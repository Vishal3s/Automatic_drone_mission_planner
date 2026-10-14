"""
RAMR novelty Phase 4 tests -- Basic Telemetry-Based RAMR.
Run with: pytest tests/test_phase_replanner.py -v

Everything here is pure Python/deterministic math (haversine distance,
threshold comparisons) -- no LLM, no MAVSDK, no PX4 SITL. Coordinates in
these tests use small (~1km-scale) offsets so the numbers are easy to
reason about by hand.
"""

from __future__ import annotations

from app.mavlink.drone_state import DroneState
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager
from app.replanning.replanner import AdaptiveMissionReplanner, RAMRStatus
from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent

HOME = (1.0000, 2.0000)


def _five_waypoint_mission() -> Mission:
    """WP1..WP4 (+ TAKEOFF at #1, RTL at #6) spaced ~1km apart from HOME, matching the spec's example."""
    return Mission(
        mission_name="5-waypoint survey",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0000, longitude=2.0000, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.0090, longitude=2.0000, altitude=30),
            MissionItem(sequence=4, command=MissionCommand.WAYPOINT, latitude=1.0180, longitude=2.0000, altitude=30),
            MissionItem(sequence=5, command=MissionCommand.WAYPOINT, latitude=1.0270, longitude=2.0000, altitude=30),
            MissionItem(sequence=6, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


def _running_manager_at(current_sequence: int, total: int = 6) -> MissionStateManager:
    manager = MissionStateManager()
    manager.start(_five_waypoint_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=current_sequence, total=total)
    return manager


def _battery_event(mission_state, drone_state, severity=EventSeverity.HIGH, **metadata) -> ReplanningEvent:
    return ReplanningEvent(
        event_type=EventType.BATTERY_DEVIATION,
        severity=severity,
        source=EventSource.TELEMETRY,
        mission_id=mission_state.mission_id,
        drone_state=drone_state,
        mission_state=mission_state,
        metadata=metadata,
    )


# ---------------------------------------------------------------------
# Trigger 1: Battery deviation -- the spec's own worked example
# ---------------------------------------------------------------------


def test_battery_low_trims_to_single_waypoint_then_rth():
    """The spec's own example: drone at WP3(seq 3), battery insufficient for all -> WP4 -> RTH."""
    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.22)

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(
        _battery_event(manager.snapshot(), drone_state), manager.snapshot(), drone_state,
        home_latitude_deg=HOME[0], home_longitude_deg=HOME[1],
    )

    assert result.status == RAMRStatus.MODIFIED
    items = result.modified_mission.mission_items
    assert [i.command for i in items] == [MissionCommand.WAYPOINT, MissionCommand.RETURN_TO_LAUNCH]
    assert items[0].sequence == 1 and items[1].sequence == 2
    assert items[0].latitude == 1.0180  # the "WP4" waypoint
    assert result.validation.valid is True


def test_battery_sufficient_keeps_full_remaining_route_then_rth():
    """The spec's other example: battery sufficient for everything -> WP4 -> WP5 -> RTH."""
    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.60)

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(
        _battery_event(manager.snapshot(), drone_state), manager.snapshot(), drone_state,
        home_latitude_deg=HOME[0], home_longitude_deg=HOME[1],
    )

    assert result.status == RAMRStatus.MODIFIED
    commands = [i.command for i in result.modified_mission.mission_items]
    assert commands == [MissionCommand.WAYPOINT, MissionCommand.WAYPOINT, MissionCommand.RETURN_TO_LAUNCH]


def test_battery_already_below_reserve_commands_immediate_rth():
    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.10)

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(
        _battery_event(manager.snapshot(), drone_state), manager.snapshot(), drone_state,
        home_latitude_deg=HOME[0], home_longitude_deg=HOME[1],
    )

    assert result.status == RAMRStatus.MODIFIED
    commands = [i.command for i in result.modified_mission.mission_items]
    assert commands == [MissionCommand.RETURN_TO_LAUNCH]


def test_battery_deviation_missing_drone_position_is_unsupported():
    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(battery_remaining_percent=0.5)  # no lat/lon

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(
        _battery_event(manager.snapshot(), drone_state), manager.snapshot(), drone_state,
        home_latitude_deg=HOME[0], home_longitude_deg=HOME[1],
    )

    assert result.status == RAMRStatus.UNSUPPORTED
    assert result.modified_mission is None


def test_battery_deviation_missing_battery_reading_is_unsupported():
    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(latitude_deg=1.009, longitude_deg=2.0)  # no battery

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(
        _battery_event(manager.snapshot(), drone_state), manager.snapshot(), drone_state,
        home_latitude_deg=HOME[0], home_longitude_deg=HOME[1],
    )

    assert result.status == RAMRStatus.UNSUPPORTED


def test_battery_deviation_missing_home_position_is_unsupported():
    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(latitude_deg=1.009, longitude_deg=2.0, battery_remaining_percent=0.5)

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(
        _battery_event(manager.snapshot(), drone_state), manager.snapshot(), drone_state,
        home_latitude_deg=None, home_longitude_deg=None,
    )

    assert result.status == RAMRStatus.UNSUPPORTED


def test_custom_thresholds_change_the_outcome():
    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.22)

    # A much higher reserve requirement should trim harder (immediate RTH)
    # than the default settings did for the same battery reading.
    strict_replanner = AdaptiveMissionReplanner(min_battery_reserve_percent=0.20)
    result = strict_replanner.replan(
        _battery_event(manager.snapshot(), drone_state), manager.snapshot(), drone_state,
        home_latitude_deg=HOME[0], home_longitude_deg=HOME[1],
    )
    commands = [i.command for i in result.modified_mission.mission_items]
    assert commands == [MissionCommand.RETURN_TO_LAUNCH]


# ---------------------------------------------------------------------
# Trigger 2: Missed waypoint
# ---------------------------------------------------------------------


def test_missed_waypoint_is_dropped_and_rest_of_plan_kept():
    manager = _running_manager_at(current_sequence=3)
    event = ReplanningEvent(
        event_type=EventType.MISSED_WAYPOINT,
        severity=EventSeverity.MEDIUM,
        source=EventSource.TELEMETRY,
        mission_id=manager.snapshot().mission_id,
        metadata={"missed_sequence": 4},
    )

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(event, manager.snapshot(), DroneState())

    assert result.status == RAMRStatus.MODIFIED
    remaining_lats = [i.latitude for i in result.modified_mission.mission_items if i.latitude is not None]
    assert 1.0180 not in remaining_lats  # the missed "WP3" (seq 4) was dropped
    assert 1.0270 in remaining_lats      # "WP4" (seq 5) is still there
    assert result.modified_mission.mission_items[-1].command == MissionCommand.RETURN_TO_LAUNCH
    assert result.validation.valid is True


def test_missed_waypoint_without_metadata_is_unsupported():
    manager = _running_manager_at(current_sequence=3)
    event = ReplanningEvent(
        event_type=EventType.MISSED_WAYPOINT, severity=EventSeverity.MEDIUM, source=EventSource.TELEMETRY,
    )

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(event, manager.snapshot(), DroneState())

    assert result.status == RAMRStatus.UNSUPPORTED


def test_missed_waypoint_that_is_the_last_one_still_ends_in_rth():
    manager = _running_manager_at(current_sequence=5)  # only WP4 (seq 5) and RTL (seq 6) ahead
    event = ReplanningEvent(
        event_type=EventType.MISSED_WAYPOINT,
        severity=EventSeverity.MEDIUM,
        source=EventSource.TELEMETRY,
        metadata={"missed_sequence": 5},
    )

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(event, manager.snapshot(), DroneState())

    assert result.status == RAMRStatus.MODIFIED
    commands = [i.command for i in result.modified_mission.mission_items]
    assert commands == [MissionCommand.RETURN_TO_LAUNCH]


# ---------------------------------------------------------------------
# Trigger 3: Mission deviation
# ---------------------------------------------------------------------


def test_high_severity_mission_deviation_forces_immediate_rth():
    manager = _running_manager_at(current_sequence=3)
    event = ReplanningEvent(
        event_type=EventType.MISSION_DEVIATION, severity=EventSeverity.HIGH, source=EventSource.TELEMETRY,
    )

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(event, manager.snapshot(), DroneState())

    assert result.status == RAMRStatus.MODIFIED
    commands = [i.command for i in result.modified_mission.mission_items]
    assert commands == [MissionCommand.RETURN_TO_LAUNCH]


def test_critical_severity_mission_deviation_also_forces_rth():
    manager = _running_manager_at(current_sequence=3)
    event = ReplanningEvent(
        event_type=EventType.MISSION_DEVIATION, severity=EventSeverity.CRITICAL, source=EventSource.SAFETY_MONITOR,
    )

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(event, manager.snapshot(), DroneState())

    assert result.status == RAMRStatus.MODIFIED


def test_low_medium_severity_mission_deviation_makes_no_change():
    manager = _running_manager_at(current_sequence=3)
    for severity in (EventSeverity.LOW, EventSeverity.MEDIUM):
        event = ReplanningEvent(
            event_type=EventType.MISSION_DEVIATION, severity=severity, source=EventSource.TELEMETRY,
        )
        result = AdaptiveMissionReplanner().replan(event, manager.snapshot(), DroneState())
        assert result.status == RAMRStatus.UNCHANGED
        assert result.modified_mission is None


# ---------------------------------------------------------------------
# Unhandled event types (out of scope for this phase)
# ---------------------------------------------------------------------


def test_unhandled_event_type_returns_unhandled_status_without_raising():
    manager = _running_manager_at(current_sequence=3)
    event = ReplanningEvent(
        event_type=EventType.SAFETY_EVENT, severity=EventSeverity.CRITICAL, source=EventSource.SAFETY_MONITOR,
    )

    result = AdaptiveMissionReplanner().replan(event, manager.snapshot(), DroneState())

    assert result.status == RAMRStatus.UNHANDLED_EVENT_TYPE
    assert result.modified_mission is None


def test_coverage_gap_detected_is_unhandled_in_this_phase():
    """
    COVERAGE_GAP_DETECTED still has no deterministic handler -- no
    coverage-gap detector exists anywhere in this project (see
    app/trigger/engine.py's docstring). This is the one CV event type
    that is still genuinely out of scope after RAMR/CV novelty Phase 14
    (see test_phase_cv_triggered_replanning.py for the other three,
    which Phase 14 now handles).
    """
    manager = _running_manager_at(current_sequence=3)
    event = ReplanningEvent(
        event_type=EventType.COVERAGE_GAP_DETECTED, severity=EventSeverity.HIGH, source=EventSource.COMPUTER_VISION
    )
    result = AdaptiveMissionReplanner().replan(event, manager.snapshot(), DroneState())
    assert result.status == RAMRStatus.UNHANDLED_EVENT_TYPE


# ---------------------------------------------------------------------
# Full chain -- matches the spec's SUCCESS CRITERIA:
# Telemetry event -> RAMR -> Modified remaining mission
# ---------------------------------------------------------------------


def test_full_chain_telemetry_event_to_modified_remaining_mission():
    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.22)
    event = _battery_event(manager.snapshot(), drone_state, metadata={"battery_percent": 0.22})

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=HOME[0], home_longitude_deg=HOME[1],
    )

    assert result.status == RAMRStatus.MODIFIED
    assert result.modified_mission is not None
    assert result.validation is not None and result.validation.valid is True
    # The modified mission is a genuinely different, safe remaining route.
    assert result.modified_mission.mission_items[-1].command == MissionCommand.RETURN_TO_LAUNCH
