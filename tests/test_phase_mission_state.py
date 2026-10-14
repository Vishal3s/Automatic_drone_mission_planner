"""
RAMR novelty Phase 1 tests -- Mission State Management.
Run with: pytest tests/test_phase_mission_state.py -v

Rewritten to match the actual Phase 1 spec (mission_id, original vs.
active mission, replanning_status, adaptation_count, mission_version,
the 10-state MissionStatus enum, and the required getter API) rather
than the earlier, differently-shaped first version of this file.

`app/mission/state.py` is pure in-memory bookkeeping (no MAVSDK, no I/O,
no asyncio), so these tests need nothing beyond the existing `Mission`/
`MissionItem` schema -- no fakes, no PX4 SITL, no LLM.
"""

from __future__ import annotations

import pytest

from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import (
    MissionStateError,
    MissionStateManager,
    MissionStatus,
    ReplanningStatus,
)


def _mission(name: str = "State test mission") -> Mission:
    return Mission(
        mission_name=name,
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=2.0, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=3.0, longitude=4.0, altitude=30),
            MissionItem(sequence=4, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


def _running_manager() -> MissionStateManager:
    """Helper: a manager already walked through CREATED -> VALIDATED -> UPLOADED -> RUNNING."""
    manager = MissionStateManager()
    manager.start(_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    return manager


# ---------------------------------------------------------------------
# start() / basic fields
# ---------------------------------------------------------------------


def test_start_initializes_all_required_fields():
    manager = MissionStateManager()
    state = manager.start(_mission())

    assert state.mission_id  # auto-generated, non-empty
    assert state.original_mission.mission_name == "State test mission"
    assert state.active_mission is state.original_mission
    assert state.mission_status == MissionStatus.CREATED
    assert state.replanning_status == ReplanningStatus.NONE
    assert state.adaptation_count == 0
    assert state.mission_version == 1
    assert state.current_waypoint is None
    assert state.completed_waypoints == []
    assert len(state.remaining_waypoints) == 4


def test_start_accepts_an_explicit_mission_id():
    manager = MissionStateManager()
    state = manager.start(_mission(), mission_id="mission-abc-123")
    assert state.mission_id == "mission-abc-123"


def test_two_manager_instances_get_different_auto_ids():
    m1 = MissionStateManager()
    m2 = MissionStateManager()
    s1 = m1.start(_mission())
    s2 = m2.start(_mission())
    assert s1.mission_id != s2.mission_id


# ---------------------------------------------------------------------
# Normal lifecycle transitions
# ---------------------------------------------------------------------


def test_normal_lifecycle_created_to_running():
    manager = MissionStateManager()
    manager.start(_mission())

    assert manager.mark_validated().mission_status == MissionStatus.VALIDATED
    assert manager.mark_uploaded().mission_status == MissionStatus.UPLOADED
    assert manager.mark_running().mission_status == MissionStatus.RUNNING


def test_invalid_transition_raises():
    manager = MissionStateManager()
    manager.start(_mission())
    # Cannot jump straight from CREATED to UPLOADED, skipping VALIDATED.
    with pytest.raises(MissionStateError):
        manager.mark_uploaded()


def test_running_to_completed():
    manager = _running_manager()
    state = manager.complete()
    assert state.mission_status == MissionStatus.COMPLETED
    assert state.remaining_waypoints == []
    assert manager.is_terminal is True


def test_running_to_returning_home_to_completed():
    manager = _running_manager()
    assert manager.mark_returning_home().mission_status == MissionStatus.RETURNING_HOME
    assert manager.complete().mission_status == MissionStatus.COMPLETED


def test_fail_allowed_from_any_non_terminal_status():
    manager = MissionStateManager()
    manager.start(_mission())
    state = manager.fail("upload rejected: no landing defined")
    assert state.mission_status == MissionStatus.FAILED
    assert state.last_error == "upload rejected: no landing defined"
    assert manager.is_terminal is True


def test_fail_from_terminal_status_raises():
    manager = _running_manager()
    manager.complete()
    with pytest.raises(MissionStateError):
        manager.fail("too late")


def test_abort_records_optional_reason():
    manager = _running_manager()
    state = manager.abort("operator cancelled")
    assert state.mission_status == MissionStatus.ABORTED
    assert state.last_error == "operator cancelled"


# ---------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------


def test_record_progress_requires_running_or_returning_home():
    manager = MissionStateManager()
    manager.start(_mission())
    with pytest.raises(MissionStateError):
        manager.record_progress(current=2, total=4)


def test_record_progress_splits_completed_remaining_and_sets_current_waypoint():
    manager = _running_manager()
    state = manager.record_progress(current=3, total=4)

    assert state.current_sequence == 3
    assert [item.sequence for item in state.completed_waypoints] == [1, 2]
    assert [item.sequence for item in state.remaining_waypoints] == [3, 4]
    assert state.current_waypoint.sequence == 3
    assert state.last_progress == (3, 4)


def test_getters_match_snapshot_after_progress():
    manager = _running_manager()
    manager.record_progress(current=2, total=4)

    assert manager.get_current_waypoint().sequence == 2
    assert [i.sequence for i in manager.get_completed_waypoints()] == [1]
    assert [i.sequence for i in manager.get_remaining_waypoints()] == [2, 3, 4]
    assert manager.get_active_mission().mission_name == "State test mission"
    assert manager.get_original_mission().mission_name == "State test mission"


def test_record_progress_allowed_while_returning_home():
    manager = _running_manager()
    manager.mark_returning_home()
    state = manager.record_progress(current=4, total=4)
    assert state.current_sequence == 4


def test_record_progress_after_terminal_status_raises():
    manager = _running_manager()
    manager.complete()
    with pytest.raises(MissionStateError):
        manager.record_progress(current=4, total=4)


# ---------------------------------------------------------------------
# Replanning loop
# ---------------------------------------------------------------------


def test_full_replanning_loop_updates_active_mission_version_and_adaptation_count():
    manager = _running_manager()
    manager.record_progress(current=3, total=4)  # drone is partway through

    assert manager.start_replanning().mission_status == MissionStatus.REPLANNING
    assert manager.snapshot().replanning_status == ReplanningStatus.IN_PROGRESS

    assert manager.begin_update().mission_status == MissionStatus.UPDATING

    corrective_mission = Mission(
        mission_name="Corrective route",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=9.0, longitude=9.0, altitude=25),
            MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )
    state = manager.apply_replan(corrective_mission)

    assert state.mission_status == MissionStatus.RUNNING
    assert state.replanning_status == ReplanningStatus.APPLIED
    assert state.active_mission is corrective_mission
    assert state.mission_version == 2
    assert state.adaptation_count == 1
    # Original mission is untouched.
    assert manager.get_original_mission().mission_name == "State test mission"
    # Progress tracking reset against the new active mission.
    assert state.current_sequence == 0
    assert state.current_waypoint is None
    assert len(state.remaining_waypoints) == 2


def test_replan_can_fail_and_rolls_back_to_running_on_old_active_mission():
    manager = _running_manager()
    original_active = manager.get_active_mission()

    manager.start_replanning()
    manager.begin_update()
    state = manager.fail_replan("revised route failed safety revalidation")

    assert state.mission_status == MissionStatus.RUNNING
    assert state.replanning_status == ReplanningStatus.FAILED
    assert state.last_error == "revised route failed safety revalidation"
    # Nothing was actually applied.
    assert state.active_mission is original_active
    assert state.mission_version == 1
    assert state.adaptation_count == 0


def test_apply_replan_requires_updating_status():
    manager = _running_manager()
    with pytest.raises(MissionStateError):
        manager.apply_replan(_mission("too early"))


def test_multiple_replans_accumulate_version_and_adaptation_count():
    manager = _running_manager()

    manager.start_replanning()
    manager.begin_update()
    manager.apply_replan(_mission("replan 1"))

    manager.start_replanning()
    manager.begin_update()
    state = manager.apply_replan(_mission("replan 2"))

    assert state.mission_version == 3
    assert state.adaptation_count == 2
    assert state.active_mission.mission_name == "replan 2"


# ---------------------------------------------------------------------
# remaining_route() helper (forward-looking, not part of the spec)
# ---------------------------------------------------------------------


def test_remaining_route_renumbers_sequence_from_one():
    manager = _running_manager()
    manager.record_progress(current=3, total=4)

    remaining = manager.remaining_route()

    assert [item.sequence for item in remaining.mission_items] == [1, 2]
    assert remaining.mission_items[0].command == MissionCommand.WAYPOINT
    assert remaining.mission_items[1].command == MissionCommand.RETURN_TO_LAUNCH


# ---------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------


def test_snapshot_before_start_raises():
    manager = MissionStateManager()
    with pytest.raises(MissionStateError):
        manager.snapshot()


def test_as_dict_is_json_serializable_shape():
    import json

    manager = _running_manager()
    manager.record_progress(current=2, total=4)

    payload = json.dumps(manager.snapshot().as_dict())
    assert '"mission_status": "RUNNING"' in payload
    assert '"adaptation_count": 0' in payload
