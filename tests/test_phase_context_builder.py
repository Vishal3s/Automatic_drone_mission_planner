"""
RAMR novelty Phase 5 tests -- Replanning Context Builder.
Run with: pytest tests/test_phase_context_builder.py -v

Pure Python -- no LLM/MAVSDK/PX4 SITL needed.
"""

from __future__ import annotations

import dataclasses

from app.mavlink.drone_state import DroneState
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager
from app.replanning.context_builder import (
    BatteryStatus,
    DronePosition,
    ReplanningContext,
    ReplanningContextBuilder,
    SafetyConstraints,
)
from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent


def _mission() -> Mission:
    return Mission(
        mission_name="Context test mission",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=2.0, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.01, longitude=2.0, altitude=30),
            MissionItem(sequence=4, command=MissionCommand.WAYPOINT, latitude=1.02, longitude=2.0, altitude=30),
            MissionItem(sequence=5, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


def _running_manager_at(current_sequence: int) -> MissionStateManager:
    manager = MissionStateManager()
    manager.start(_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=current_sequence, total=5)
    return manager


def _event(**kwargs) -> ReplanningEvent:
    defaults = dict(event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.HIGH, source=EventSource.TELEMETRY)
    defaults.update(kwargs)
    return ReplanningEvent(**defaults)


# ---------------------------------------------------------------------
# Core build() behavior
# ---------------------------------------------------------------------


def test_build_returns_current_completed_remaining_from_mission_state():
    manager = _running_manager_at(3)
    drone_state = DroneState(latitude_deg=1.01, longitude_deg=2.0, relative_altitude_m=30.0)

    context = ReplanningContextBuilder().build(_event(), manager.snapshot(), drone_state)

    assert context.current_waypoint is manager.snapshot().current_waypoint
    assert context.completed_waypoints == manager.snapshot().completed_waypoints
    assert context.remaining_waypoints == manager.snapshot().remaining_waypoints
    assert context.mission_id == manager.snapshot().mission_id


def test_build_captures_current_drone_position():
    manager = _running_manager_at(3)
    drone_state = DroneState(latitude_deg=1.01, longitude_deg=2.0, relative_altitude_m=30.0)

    context = ReplanningContextBuilder().build(_event(), manager.snapshot(), drone_state)

    assert context.current_drone_position == DronePosition(latitude_deg=1.01, longitude_deg=2.0, altitude_m=30.0)


def test_build_trigger_reason_includes_type_severity_source_and_metadata():
    manager = _running_manager_at(3)
    event = _event(metadata={"battery_percent": 0.12})

    context = ReplanningContextBuilder().build(event, manager.snapshot(), DroneState())

    assert "BATTERY_DEVIATION" in context.trigger_reason
    assert "HIGH" in context.trigger_reason
    assert "TELEMETRY" in context.trigger_reason
    assert "battery_percent" in context.trigger_reason
    assert context.trigger_event is event


# ---------------------------------------------------------------------
# BatteryStatus
# ---------------------------------------------------------------------


def test_battery_status_sufficient_true_above_reserve():
    manager = _running_manager_at(3)
    drone_state = DroneState(battery_remaining_percent=0.5)

    context = ReplanningContextBuilder().build(_event(), manager.snapshot(), drone_state)

    assert context.battery_status.remaining_percent == 0.5
    assert context.battery_status.sufficient is True


def test_battery_status_sufficient_false_below_reserve():
    manager = _running_manager_at(3)
    drone_state = DroneState(battery_remaining_percent=0.05)

    context = ReplanningContextBuilder().build(_event(), manager.snapshot(), drone_state)

    assert context.battery_status.sufficient is False


def test_battery_status_sufficient_none_when_unknown():
    manager = _running_manager_at(3)
    drone_state = DroneState()  # no battery reading at all

    context = ReplanningContextBuilder().build(_event(), manager.snapshot(), drone_state)

    assert context.battery_status.remaining_percent is None
    assert context.battery_status.sufficient is None


# ---------------------------------------------------------------------
# SafetyConstraints
# ---------------------------------------------------------------------


def test_safety_constraints_default_from_settings():
    from app.config import settings

    manager = _running_manager_at(3)
    context = ReplanningContextBuilder().build(_event(), manager.snapshot(), DroneState())

    assert context.safety_constraints.min_altitude_m == settings.min_altitude_m
    assert context.safety_constraints.max_altitude_m == settings.max_altitude_m
    assert context.safety_constraints.min_battery_reserve_percent == settings.min_battery_reserve_percent
    assert context.safety_constraints.max_waypoints == settings.max_waypoints
    assert context.safety_constraints.geofence is None


def test_safety_constraints_can_be_overridden():
    manager = _running_manager_at(3)
    custom = SafetyConstraints(
        min_altitude_m=5, max_altitude_m=80, min_battery_reserve_percent=0.30, max_waypoints=10
    )
    drone_state = DroneState(battery_remaining_percent=0.25)  # above default reserve, below this custom one

    context = ReplanningContextBuilder().build(
        _event(), manager.snapshot(), drone_state, safety_constraints=custom
    )

    assert context.safety_constraints is custom
    assert context.battery_status.minimum_reserve_percent == 0.30
    assert context.battery_status.sufficient is False  # 0.25 < 0.30


# ---------------------------------------------------------------------
# mission_priority / relevant_history
# ---------------------------------------------------------------------


def test_mission_priority_defaults_to_normal_and_is_overridable():
    manager = _running_manager_at(3)

    default_context = ReplanningContextBuilder().build(_event(), manager.snapshot(), DroneState())
    assert default_context.mission_priority == "NORMAL"

    high_context = ReplanningContextBuilder().build(
        _event(), manager.snapshot(), DroneState(), mission_priority="HIGH"
    )
    assert high_context.mission_priority == "HIGH"


def test_relevant_history_defaults_to_empty_list():
    manager = _running_manager_at(3)
    context = ReplanningContextBuilder().build(_event(), manager.snapshot(), DroneState())
    assert context.relevant_history == []


def test_relevant_history_filters_by_mission_id():
    manager = _running_manager_at(3)
    mission_id = manager.snapshot().mission_id
    history = [
        {"mission_id": mission_id, "note": "relevant"},
        {"mission_id": "some-other-mission", "note": "irrelevant"},
        {"note": "general lessons learned, no mission_id"},
    ]

    context = ReplanningContextBuilder().build(
        _event(), manager.snapshot(), DroneState(), mission_history=history
    )

    notes = [h["note"] for h in context.relevant_history]
    assert "relevant" in notes
    assert "irrelevant" not in notes
    assert "general lessons learned, no mission_id" in notes


# ---------------------------------------------------------------------
# "Do not rebuild the entire original mission context unnecessarily"
# ---------------------------------------------------------------------


def test_context_does_not_carry_the_full_original_or_active_mission():
    field_names = {f.name for f in dataclasses.fields(ReplanningContext)}
    assert "original_mission" not in field_names
    assert "active_mission" not in field_names


def test_as_dict_is_json_serializable_shape():
    import json

    manager = _running_manager_at(3)
    drone_state = DroneState(latitude_deg=1.01, longitude_deg=2.0, battery_remaining_percent=0.4)
    context = ReplanningContextBuilder().build(_event(), manager.snapshot(), drone_state)

    payload = json.dumps(context.as_dict())
    assert '"mission_priority": "NORMAL"' in payload
    assert '"sufficient": true' in payload


# ---------------------------------------------------------------------
# Full chain -- matches the spec's SUCCESS CRITERIA
# ---------------------------------------------------------------------


def test_full_chain_trigger_event_produces_complete_accurate_snapshot():
    manager = _running_manager_at(3)
    drone_state = DroneState(
        latitude_deg=1.01, longitude_deg=2.0, relative_altitude_m=30.0,
        battery_remaining_percent=0.12, flight_mode="MISSION", is_armed=True,
    )
    event = _event(
        mission_id=manager.snapshot().mission_id,
        drone_state=drone_state,
        mission_state=manager.snapshot(),
        metadata={"battery_percent": 0.12, "min_reserve_percent": 0.15},
    )

    context = ReplanningContextBuilder().build(event, manager.snapshot(), drone_state)

    assert context.mission_id == manager.snapshot().mission_id
    assert context.current_waypoint.sequence == 3
    assert [i.sequence for i in context.completed_waypoints] == [1, 2]
    assert [i.sequence for i in context.remaining_waypoints] == [3, 4, 5]
    assert context.battery_status.sufficient is False
    assert "BATTERY_DEVIATION" in context.trigger_reason
    assert context.safety_constraints is not None
    assert context.mission_priority == "NORMAL"
    assert context.relevant_history == []
