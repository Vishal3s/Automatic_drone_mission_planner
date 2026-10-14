"""
RAMR novelty Phase 9 tests -- Mid-Flight Mission Update Manager.
Run with: pytest tests/test_phase_mission_update_manager.py -v

Same fake-MAVSDK-at-sys.modules pattern `tests/test_phase7_8_px4_client.py`
already established (see that file's own docstring) -- extended here with
`pause_mission()`/`clear_mission()` fakes, since this module needs them
and Phase 7/8's fakes didn't. No real `mavsdk` package, network, or PX4
SITL instance is needed to run these.

IMPORTANT -- what these tests DO and DO NOT prove: they prove this
module calls the documented MAVSDK API in the right ORDER with the right
DATA (pause before clear/upload, never a TAKEOFF item, no
set_current_mission_item() call, resume via start_mission(), the
concurrency guard, the restore-on-failure path). They do NOT prove real
PX4 SITL actually behaves this way when flying -- see
`app/mavlink/mission_update_manager.py`'s own docstring ("WHAT WAS NOT
VERIFIED") for why that gap is real and how to close it on your machine.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.mavlink.mission_update_manager import MissionUpdateManager
from app.mission.converter import ConvertedMission
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager


# ---------------------------------------------------------------------
# The spec's own TEST SCENARIO mission.
# ---------------------------------------------------------------------


def _original_mission() -> Mission:
    return Mission(
        mission_name="WP1-WP5 survey",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.000, longitude=2.000, altitude=30),  # WP1
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.010, longitude=2.000, altitude=30),  # WP2
            MissionItem(sequence=4, command=MissionCommand.WAYPOINT, latitude=1.020, longitude=2.000, altitude=30),  # WP3
            MissionItem(sequence=5, command=MissionCommand.WAYPOINT, latitude=1.030, longitude=2.000, altitude=30),  # WP4
            MissionItem(sequence=6, command=MissionCommand.WAYPOINT, latitude=1.040, longitude=2.000, altitude=30),  # WP5
            MissionItem(sequence=7, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


def _manager_at_wp3() -> MissionStateManager:
    """Mission running, drone currently AT WP3 (sequence 4 of 7)."""
    manager = MissionStateManager()
    manager.start(_original_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=4, total=7)  # completed items #1-3 (TAKEOFF, WP1, WP2); current = WP3
    return manager


def _updated_mission() -> Mission:
    """CURRENT POSITION -> NEW WAYPOINT -> WP4 -> WP5 -> RTH, per the spec's own diagram."""
    return Mission(
        mission_name="WP1-WP5 survey (adapted)",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1.021, longitude=2.001, altitude=30),  # NEW WAYPOINT
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.030, longitude=2.000, altitude=30),  # WP4
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.040, longitude=2.000, altitude=30),  # WP5
            MissionItem(sequence=4, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------


class _FakeMissionError(Exception):
    pass


class _FakeMissionProgress:
    def __init__(self, current: int, total: int):
        self.current = current
        self.total = total


class _FakeMissionPlugin:
    def __init__(
        self,
        *,
        pause_error: Exception | None = None,
        clear_error: Exception | None = None,
        upload_error: Exception | None = None,
        upload_fail_times: int = 1,
        start_error: Exception | None = None,
        post_update_progress: tuple[int, int] | None = (1, 4),
    ):
        self.pause_error = pause_error
        self.clear_error = clear_error
        self.upload_error = upload_error
        # `upload_error` (when set) raises on the first `upload_fail_times`
        # calls to upload_mission() and succeeds after that -- lets tests
        # distinguish "the real update's upload failed, but a later
        # restore attempt's upload succeeds" from "uploading is broken
        # persistently, so restore fails too".
        self.upload_fail_times = upload_fail_times
        self._upload_call_count = 0
        self.start_error = start_error
        self.post_update_progress = post_update_progress

        self.call_log: list[str] = []
        self.uploaded_plans: list[object] = []
        self.pause_called = False
        self.clear_called = False
        self.start_called_count = 0
        self.rtl_flag_set_to: bool | None = None
        self.current_mission_item_calls: list[int] = []

    async def pause_mission(self):
        self.call_log.append("pause")
        self.pause_called = True
        if self.pause_error:
            raise self.pause_error

    async def clear_mission(self):
        self.call_log.append("clear")
        self.clear_called = True
        if self.clear_error:
            raise self.clear_error

    async def upload_mission(self, plan):
        self.call_log.append("upload")
        self._upload_call_count += 1
        if self.upload_error and self._upload_call_count <= self.upload_fail_times:
            raise self.upload_error
        self.uploaded_plans.append(plan)

    async def set_return_to_launch_after_mission(self, enable: bool):
        self.call_log.append("set_rtl")
        self.rtl_flag_set_to = enable

    async def start_mission(self):
        self.call_log.append("start")
        self.start_called_count += 1
        if self.start_error:
            raise self.start_error

    async def set_current_mission_item(self, index: int):
        self.call_log.append("set_current_item")
        self.current_mission_item_calls.append(index)

    async def mission_progress(self):
        if self.post_update_progress is not None:
            yield _FakeMissionProgress(*self.post_update_progress)
        else:
            await asyncio.sleep(1000)
            yield _FakeMissionProgress(0, 0)  # pragma: no cover -- unreachable in tests


class _FakeSystem:
    def __init__(self, *, mission: _FakeMissionPlugin | None = None):
        self.mission = mission or _FakeMissionPlugin()


@pytest.fixture()
def install_fake_mavsdk(monkeypatch):
    def install():
        fake_mission_module = types.ModuleType("mavsdk.mission")
        fake_mission_module.MissionError = _FakeMissionError

        fake_mavsdk_package = types.ModuleType("mavsdk")
        fake_mavsdk_package.mission = fake_mission_module

        monkeypatch.setitem(sys.modules, "mavsdk", fake_mavsdk_package)
        monkeypatch.setitem(sys.modules, "mavsdk.mission", fake_mission_module)
        monkeypatch.delitem(sys.modules, "app.mavlink.mission_update_manager", raising=False)

        import app.mavlink.mission_update_manager as mission_update_manager_module

        monkeypatch.setattr(
            mission_update_manager_module,
            "to_mavsdk_mission_plan",
            lambda mission, **kwargs: ConvertedMission(
                plan=types.SimpleNamespace(mission_items=list(mission.mission_items)),
                return_to_launch_after_mission=True,
            ),
        )
        return mission_update_manager_module

    return install


# ---------------------------------------------------------------------
# Happy path -- the spec's own TEST SCENARIO
# ---------------------------------------------------------------------


def test_full_update_succeeds_and_follows_the_documented_call_order(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin(post_update_progress=(1, 4))
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone,
            mission_state_manager=state_manager,
            approved_mission=_updated_mission(),
            home_latitude_deg=1.000,
            home_longitude_deg=2.000,
            return_to_launch_after_mission=True,
            pause_settle_s=0,
            progress_verify_timeout_s=1.0,
        )
    )

    assert result.accepted is True
    assert result.stage == "complete"
    assert result.error is None

    # Responsibility 6: pause BEFORE clear/upload.
    assert fake_mission.call_log.index("pause") < fake_mission.call_log.index("clear")
    assert fake_mission.call_log.index("clear") < fake_mission.call_log.index("upload")
    assert fake_mission.call_log.index("upload") < fake_mission.call_log.index("start")

    # Responsibility 7: the uploaded plan is the UPDATED mission's items, not the original's.
    assert len(fake_mission.uploaded_plans) == 1
    uploaded_items = fake_mission.uploaded_plans[0].mission_items
    assert [i.sequence for i in uploaded_items] == [1, 2, 3, 4]
    assert uploaded_items[0].latitude == 1.021  # the "NEW WAYPOINT", not WP1's 1.010

    # Responsibility 8: never a TAKEOFF item, and set_current_mission_item() is never called
    # (per this module's documented research decision).
    assert all(i.command != MissionCommand.TAKEOFF for i in uploaded_items)
    assert fake_mission.current_mission_item_calls == []

    # Responsibility 9: resumed via start_mission(), exactly once.
    assert fake_mission.start_called_count == 1

    # Responsibility 10: progress was verified against the NEW plan's item count (4 items).
    assert result.verified_progress == (1, 4)

    # MissionState (Phase 1) was actually updated.
    final_state = state_manager.snapshot()
    assert final_state.mission_status.value == "RUNNING"
    assert final_state.adaptation_count == 1
    assert final_state.mission_version == 2
    assert final_state.active_mission is not state_manager.get_original_mission()


# ---------------------------------------------------------------------
# SUCCESS CRITERIA -- does not restart from WP1, no duplicated completed
# waypoints, safely continues from current position.
# ---------------------------------------------------------------------


def test_success_criteria_no_wp1_no_duplicated_completed_waypoints(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin(post_update_progress=(1, 4))
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )

    uploaded_items = fake_mission.uploaded_plans[0].mission_items
    uploaded_coords = {(i.latitude, i.longitude) for i in uploaded_items if i.latitude is not None}
    # WP1 (1.010) and WP2 (1.020, the "current" waypoint at time of trigger) must not reappear.
    assert (1.010, 2.000) not in uploaded_coords
    # Original TAKEOFF/home coordinate must not appear either -- would mean "restart from WP1".
    assert (1.000, 2.000) not in uploaded_coords


# ---------------------------------------------------------------------
# Responsibility 2: prevent multiple simultaneous replanning requests
# ---------------------------------------------------------------------


def test_concurrent_update_is_rejected_not_queued(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()

    async def _run() -> tuple:
        # A mission plugin whose pause() blocks "forever" so the first
        # apply_update() call is still in flight when the second one starts.
        class _SlowPausePlugin(_FakeMissionPlugin):
            async def pause_mission(self):
                self.call_log.append("pause")
                await asyncio.sleep(10)

        fake_mission = _SlowPausePlugin()
        drone = _FakeSystem(mission=fake_mission)
        state_manager = _manager_at_wp3()
        manager = mum_module.MissionUpdateManager()

        first_task = asyncio.create_task(
            manager.apply_update(
                drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
                home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
                pause_settle_s=0, progress_verify_timeout_s=1.0,
            )
        )
        await asyncio.sleep(0.05)  # let the first call acquire the lock and reach pause_mission()

        second_result = await manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
        first_task.cancel()
        return second_result

    second_result = asyncio.run(_run())
    assert second_result.accepted is False
    assert second_result.stage == "guard"


def test_state_manager_guard_rejects_update_when_not_running(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin()
    drone = _FakeSystem(mission=fake_mission)

    state_manager = MissionStateManager()
    state_manager.start(_original_mission())  # status = CREATED, never reached RUNNING

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.accepted is False
    assert result.stage == "guard"
    assert fake_mission.pause_called is False  # never touched the drone


# ---------------------------------------------------------------------
# Responsibility 8 -- defensive TAKEOFF rejection
# ---------------------------------------------------------------------


def test_approved_mission_starting_with_takeoff_is_rejected_before_touching_the_drone(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin()
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    bad_mission = Mission(
        mission_name="bad",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=bad_mission,
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.accepted is False
    assert result.stage == "precondition"
    assert fake_mission.pause_called is False

    # MissionState rolled back to RUNNING, not left stuck in UPDATING/REPLANNING.
    assert state_manager.snapshot().mission_status.value == "RUNNING"


# ---------------------------------------------------------------------
# Failure + restore paths
# ---------------------------------------------------------------------


def test_pause_failure_rolls_back_without_touching_mission_state_on_the_fc(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin(pause_error=_FakeMissionError("pause refused"))
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.accepted is False
    assert result.stage == "pause"
    assert result.flight_controller_mission_restored is None  # clear() never ran -- nothing to restore
    assert fake_mission.clear_called is False
    assert state_manager.snapshot().mission_status.value == "RUNNING"


def test_upload_failure_after_clear_attempts_restore_and_reports_success(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin(upload_error=_FakeMissionError("rejected: bad item"), upload_fail_times=1)
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.accepted is False
    assert result.stage == "upload"
    # First upload attempt failed; restore path retries upload+start with the ORIGINAL remaining mission.
    assert result.flight_controller_mission_restored is True
    assert fake_mission.start_called_count == 1  # only the restore's start_mission() succeeded
    assert state_manager.snapshot().mission_status.value == "RUNNING"


def test_upload_failure_and_restore_also_failing_leaves_vehicle_paused(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    # upload_fail_times=999 -- every upload_mission() call raises, including the restore's.
    fake_mission = _FakeMissionPlugin(upload_error=_FakeMissionError("rejected"), upload_fail_times=999)
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.accepted is False
    assert result.flight_controller_mission_restored is False
    # Vehicle was paused and never successfully resumed on any mission -- the safe worst case.
    assert fake_mission.start_called_count == 0


def test_start_failure_after_upload_attempts_restore(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin(start_error=_FakeMissionError("start refused"))
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.accepted is False
    assert result.stage == "resume"
    # Both the real upload and the restore's upload happened (2 uploads total).
    assert len(fake_mission.uploaded_plans) == 2
    # start_mission() fails every time in this fake, so the restore's own resume attempt
    # fails too -- vehicle ends up paused, which is this module's safe worst case.
    assert result.flight_controller_mission_restored is False


def test_verification_timeout_is_reported_without_restore_attempt(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin(post_update_progress=None)  # mission_progress() never yields in time
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=0.05,
        )
    )
    assert result.accepted is False
    assert result.stage == "verify"
    # The new mission is genuinely active on the FC -- no restore is attempted (nothing to restore TO).
    assert result.flight_controller_mission_restored is None
    assert fake_mission.start_called_count == 1  # only ever started the NEW mission, not re-started an old one


def test_verification_wrong_total_is_reported_as_unverified(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    # Reports a total of 99 -- clearly not the 4-item updated plan -- simulating stale/mismatched telemetry.
    fake_mission = _FakeMissionPlugin(post_update_progress=(1, 99))
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.accepted is False
    assert result.stage == "verify"
    assert result.verified_progress == (1, 99)


# ---------------------------------------------------------------------
# is_update_in_progress() helper
# ---------------------------------------------------------------------


def test_is_update_in_progress_reflects_mission_state():
    state_manager = _manager_at_wp3()
    assert MissionUpdateManager.is_update_in_progress(state_manager) is False

    state_manager.start_replanning()
    assert MissionUpdateManager.is_update_in_progress(state_manager) is True

    state_manager.begin_update()
    assert MissionUpdateManager.is_update_in_progress(state_manager) is True


# ---------------------------------------------------------------------
# RAMR novelty Phase 10 integration-testing fix: the RTL flag is derived
# from the mission's own content (matching the original px4_client.py
# precedent) unless a caller explicitly overrides it, and the restore
# path sets it too.
# ---------------------------------------------------------------------


def test_rtl_flag_defaults_to_the_converted_missions_own_value(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin(post_update_progress=(1, 4))
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000,
            # return_to_launch_after_mission intentionally omitted -- must default to
            # converted.return_to_launch_after_mission (the fake's to_mavsdk_mission_plan
            # stub always returns True), not silently default to some other value.
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.accepted is True
    assert fake_mission.rtl_flag_set_to is True


def test_rtl_flag_override_is_honored_when_explicitly_given(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin(post_update_progress=(1, 4))
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000,
            return_to_launch_after_mission=False,  # explicit override of the fake's converted-value (True)
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.accepted is True
    assert fake_mission.rtl_flag_set_to is False


def test_restore_path_also_sets_the_rtl_flag(install_fake_mavsdk):
    mum_module = install_fake_mavsdk()
    fake_mission = _FakeMissionPlugin(upload_error=_FakeMissionError("rejected"), upload_fail_times=1)
    drone = _FakeSystem(mission=fake_mission)
    state_manager = _manager_at_wp3()

    manager = mum_module.MissionUpdateManager()
    result = asyncio.run(
        manager.apply_update(
            drone=drone, mission_state_manager=state_manager, approved_mission=_updated_mission(),
            home_latitude_deg=1.000, home_longitude_deg=2.000, return_to_launch_after_mission=True,
            pause_settle_s=0, progress_verify_timeout_s=1.0,
        )
    )
    assert result.flight_controller_mission_restored is True
    # set_return_to_launch_after_mission was called at least once during the restore too
    # (not just skipped) -- the fake records only the LAST value set, which is what matters.
    assert "set_rtl" in fake_mission.call_log
    assert fake_mission.rtl_flag_set_to is True
