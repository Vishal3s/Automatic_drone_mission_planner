"""
RAMR novelty Phase 3 tests -- Event and Trigger Infrastructure.
Run with: pytest tests/test_phase_trigger.py -v

Everything here is pure Python (dataclasses, enums, a synchronous
pub/sub bus) -- no MAVSDK, no LLM, no PX4 SITL, no asyncio needed.
"""

from __future__ import annotations

from app.trigger.bus import EventBus
from app.trigger.engine import (
    AdaptiveReplanningRequest,
    DEFAULT_ACTIVATION_THRESHOLDS,
    TriggerDecision,
    TriggerEngine,
)
from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent, severity_at_least


def _event(
    event_type: EventType = EventType.BATTERY_DEVIATION,
    severity: EventSeverity = EventSeverity.HIGH,
    source: EventSource = EventSource.TELEMETRY,
    **kwargs,
) -> ReplanningEvent:
    return ReplanningEvent(event_type=event_type, severity=severity, source=source, **kwargs)


# ---------------------------------------------------------------------
# ReplanningEvent / severity ordering
# ---------------------------------------------------------------------


def test_event_auto_generates_id_and_timestamp():
    event = _event()
    assert event.event_id
    assert event.timestamp is not None


def test_two_events_get_different_auto_ids():
    e1, e2 = _event(), _event()
    assert e1.event_id != e2.event_id


def test_event_optional_fields_default_sensibly():
    event = _event()
    assert event.mission_id is None
    assert event.drone_state is None
    assert event.mission_state is None
    assert event.metadata == {}


def test_event_carries_mission_id_drone_state_mission_state_metadata():
    from app.mavlink.drone_state import DroneState
    from app.mission.schema import Mission, MissionCommand, MissionItem
    from app.mission.state import MissionStateManager

    drone_state = DroneState(latitude_deg=1.0, longitude_deg=2.0)
    manager = MissionStateManager()
    manager.start(
        Mission(
            mission_name="m",
            mission_items=[MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30)],
        )
    )
    mission_state = manager.snapshot()

    event = _event(
        mission_id=mission_state.mission_id,
        drone_state=drone_state,
        mission_state=mission_state,
        metadata={"battery_percent": 0.12},
    )

    assert event.mission_id == mission_state.mission_id
    assert event.drone_state is drone_state
    assert event.mission_state is mission_state
    assert event.metadata == {"battery_percent": 0.12}


def test_as_dict_is_json_serializable_shape():
    import json

    event = _event(metadata={"battery_percent": 0.12})
    payload = json.dumps(event.as_dict())
    assert '"event_type": "BATTERY_DEVIATION"' in payload
    assert '"severity": "HIGH"' in payload
    assert '"source": "TELEMETRY"' in payload


def test_severity_at_least_orders_correctly():
    assert severity_at_least(EventSeverity.HIGH, EventSeverity.MEDIUM) is True
    assert severity_at_least(EventSeverity.MEDIUM, EventSeverity.MEDIUM) is True
    assert severity_at_least(EventSeverity.LOW, EventSeverity.MEDIUM) is False
    assert severity_at_least(EventSeverity.CRITICAL, EventSeverity.HIGH) is True


def test_every_event_type_has_a_default_threshold():
    for event_type in EventType:
        assert event_type in DEFAULT_ACTIVATION_THRESHOLDS


# ---------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------


def test_bus_delivers_to_type_specific_subscriber():
    bus = EventBus()
    received = []
    bus.subscribe(received.append, event_type=EventType.BATTERY_DEVIATION)

    bus.publish(_event(event_type=EventType.BATTERY_DEVIATION))
    bus.publish(_event(event_type=EventType.MISSED_WAYPOINT))

    assert len(received) == 1
    assert received[0].event_type == EventType.BATTERY_DEVIATION


def test_bus_delivers_every_event_to_wildcard_subscriber():
    bus = EventBus()
    received = []
    bus.subscribe(received.append)  # event_type=None -> all events

    bus.publish(_event(event_type=EventType.BATTERY_DEVIATION))
    bus.publish(_event(event_type=EventType.MISSED_WAYPOINT))

    assert len(received) == 2


def test_bus_delivers_to_multiple_subscribers_for_same_event():
    bus = EventBus()
    received_a, received_b = [], []
    bus.subscribe(received_a.append, event_type=EventType.SAFETY_EVENT)
    bus.subscribe(received_b.append, event_type=EventType.SAFETY_EVENT)

    bus.publish(_event(event_type=EventType.SAFETY_EVENT, severity=EventSeverity.CRITICAL))

    assert len(received_a) == 1
    assert len(received_b) == 1


def test_bus_one_subscriber_raising_does_not_block_others():
    bus = EventBus()
    received = []

    def _bad_subscriber(event):
        raise RuntimeError("boom")

    bus.subscribe(_bad_subscriber)
    bus.subscribe(received.append)

    bus.publish(_event())  # must not raise

    assert len(received) == 1


def test_bus_history_records_every_published_event_in_order():
    bus = EventBus()
    e1 = _event(event_type=EventType.BATTERY_DEVIATION)
    e2 = _event(event_type=EventType.MISSED_WAYPOINT)
    bus.publish(e1)
    bus.publish(e2)

    assert bus.history() == [e1, e2]


# ---------------------------------------------------------------------
# TriggerEngine
# ---------------------------------------------------------------------


def test_below_threshold_severity_does_not_activate_replanning():
    engine = TriggerEngine()
    decision = engine.handle_event(
        _event(event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.LOW)
    )
    assert decision.should_replan is False
    assert decision.request is None


def test_at_or_above_threshold_severity_activates_replanning():
    engine = TriggerEngine()
    decision = engine.handle_event(
        _event(event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.MEDIUM)
    )
    assert decision.should_replan is True
    assert isinstance(decision.request, AdaptiveReplanningRequest)
    assert decision.request.event is decision.event


def test_unrecognized_event_type_does_not_activate_and_does_not_raise():
    engine = TriggerEngine(thresholds={})  # no thresholds configured at all
    decision = engine.handle_event(_event(event_type=EventType.COVERAGE_GAP_DETECTED))
    assert decision.should_replan is False
    assert "No activation threshold" in decision.reason


def test_operator_request_at_low_severity_still_activates():
    engine = TriggerEngine()
    decision = engine.handle_event(
        _event(event_type=EventType.OPERATOR_REQUEST, severity=EventSeverity.LOW, source=EventSource.OPERATOR)
    )
    assert decision.should_replan is True


def test_safety_event_requires_high_severity_by_default():
    engine = TriggerEngine()
    low = engine.handle_event(
        _event(event_type=EventType.SAFETY_EVENT, severity=EventSeverity.MEDIUM, source=EventSource.SAFETY_MONITOR)
    )
    high = engine.handle_event(
        _event(event_type=EventType.SAFETY_EVENT, severity=EventSeverity.HIGH, source=EventSource.SAFETY_MONITOR)
    )
    assert low.should_replan is False
    assert high.should_replan is True


def test_custom_thresholds_override_defaults():
    engine = TriggerEngine(thresholds={EventType.BATTERY_DEVIATION: EventSeverity.CRITICAL})
    decision = engine.handle_event(
        _event(event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.HIGH)
    )
    assert decision.should_replan is False  # HIGH < CRITICAL under this override


def test_on_replan_requested_callback_fires_only_when_activated():
    fired = []
    engine = TriggerEngine(on_replan_requested=fired.append)

    engine.handle_event(_event(event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.LOW))
    assert fired == []

    engine.handle_event(_event(event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.HIGH))
    assert len(fired) == 1
    assert isinstance(fired[0], AdaptiveReplanningRequest)


def test_decision_log_accumulates_every_call_in_order():
    engine = TriggerEngine()
    engine.handle_event(_event(event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.LOW))
    engine.handle_event(_event(event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.HIGH))

    log = engine.decision_log()
    assert len(log) == 2
    assert all(isinstance(d, TriggerDecision) for d in log)
    assert log[0].should_replan is False
    assert log[1].should_replan is True


# ---------------------------------------------------------------------
# Full chain -- matches the spec's own TEST section exactly:
# Event -> TriggerEngine -> Adaptive Replanning Request
# ---------------------------------------------------------------------


def test_full_chain_publish_battery_deviation_produces_replanning_request():
    bus = EventBus()
    requests: list[AdaptiveReplanningRequest] = []
    engine = TriggerEngine(on_replan_requested=requests.append)
    bus.subscribe(engine.handle_event)

    event = ReplanningEvent(
        event_type=EventType.BATTERY_DEVIATION,
        severity=EventSeverity.HIGH,
        source=EventSource.TELEMETRY,
        mission_id="mission-xyz",
        metadata={"battery_percent": 0.12, "threshold_percent": 0.20},
    )

    bus.publish(event)

    # Event reached the engine and produced exactly one decision.
    assert len(engine.decision_log()) == 1
    decision = engine.decision_log()[0]
    assert decision.event is event
    assert decision.should_replan is True

    # TriggerEngine -> Adaptive Replanning Request, delivered via the callback.
    assert len(requests) == 1
    request = requests[0]
    assert request.event is event
    assert request.event.event_type == EventType.BATTERY_DEVIATION
    assert request.event.mission_id == "mission-xyz"

    # Also recorded on the bus's own history for a future event logger.
    assert bus.history() == [event]
