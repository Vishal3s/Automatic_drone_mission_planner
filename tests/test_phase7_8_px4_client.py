"""
Phase 7/8 tests -- run with:  pytest tests/test_phase7_8_px4_client.py -v

`app/mavlink/px4_client.py` lazily imports `mavsdk`/`mavsdk.action`/
`mavsdk.mission` (same pattern as every other MAVSDK/provider import in
this project), so these tests fake all three at the `sys.modules` level
-- a `FakeSystem` stands in for `mavsdk.System`, with fake `core`,
`telemetry`, `mission`, and `action` sub-objects that are async
generators/coroutines shaped exactly like the real MAVSDK plugin API
(verified against the real signatures while writing `px4_client.py` --
see that file's docstring for the sources). No real `mavsdk` package,
network, or PX4 SITL instance is needed to run these -- they run
anywhere, fast, and prove the orchestration logic in
`run_mission_on_px4()` (every stage, every failure path, the
accepted/rejected result shape).

No `pytest-asyncio` dependency either -- each test is a plain sync
function that drives its async body with `asyncio.run(...)`, since that
plugin isn't guaranteed to be installed everywhere these tests run.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.mission.converter import ConvertedMission
from app.mission.schema import Mission, MissionCommand, MissionItem


def _mission() -> Mission:
    return Mission(
        mission_name="Phase 7/8 test mission",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=2.0, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


# ---------------------------------------------------------------------
# Fakes -- shaped like the real MAVSDK API (see px4_client.py's
# docstring for exactly which methods/fields were verified where).
# ---------------------------------------------------------------------


class _FakeActionError(Exception):
    pass


class _FakeMissionError(Exception):
    pass


class _FakeConnectionState:
    def __init__(self, is_connected: bool):
        self.is_connected = is_connected


class _FakeHealth:
    def __init__(self, ok: bool):
        self.is_global_position_ok = ok
        self.is_home_position_ok = ok


class _FakePosition:
    def __init__(self, latitude_deg: float, longitude_deg: float):
        self.latitude_deg = latitude_deg
        self.longitude_deg = longitude_deg


class _FakeMissionProgress:
    def __init__(self, current: int, total: int):
        self.current = current
        self.total = total


class _FakeCore:
    def __init__(self, *, connects: bool):
        self._connects = connects

    async def connection_state(self):
        if self._connects:
            yield _FakeConnectionState(True)
        else:
            # Never resolves is_connected=True -- exercises the real
            # asyncio.wait_for timeout path in _wait_for_connection().
            await asyncio.sleep(1000)
            yield _FakeConnectionState(False)  # pragma: no cover -- unreachable in tests


class _FakeTelemetry:
    def __init__(self, *, preflight_ready: bool, home: tuple[float, float]):
        self._preflight_ready = preflight_ready
        self._home = home

    async def health(self):
        if self._preflight_ready:
            yield _FakeHealth(True)
        else:
            await asyncio.sleep(1000)
            yield _FakeHealth(False)  # pragma: no cover -- unreachable in tests

    async def home(self):
        yield _FakePosition(*self._home)


class _FakeMissionPlugin:
    def __init__(
        self,
        *,
        upload_error: Exception | None = None,
        start_error: Exception | None = None,
        progress_sequence: list[tuple[int, int]] | None = None,
        finish_after_last_progress: bool = True,
    ):
        self.upload_error = upload_error
        self.start_error = start_error
        self.progress_sequence = progress_sequence or [(1, 1)]
        self.finish_after_last_progress = finish_after_last_progress
        self.uploaded_plan = None
        self.rtl_flag_set_to: bool | None = None
        self._current_index = -1

    async def upload_mission(self, plan):
        if self.upload_error:
            raise self.upload_error
        self.uploaded_plan = plan

    async def set_return_to_launch_after_mission(self, enable: bool):
        self.rtl_flag_set_to = enable

    async def start_mission(self):
        if self.start_error:
            raise self.start_error

    async def mission_progress(self):
        for index, (current, total) in enumerate(self.progress_sequence):
            self._current_index = index
            yield _FakeMissionProgress(current, total)
        if not self.finish_after_last_progress:
            # Simulate a stalled mission -- is_mission_finished() never
            # returns True, exercising the monitor timeout path.
            await asyncio.sleep(1000)

    async def is_mission_finished(self) -> bool:
        return self.finish_after_last_progress and self._current_index == len(self.progress_sequence) - 1


class _FakeActionPlugin:
    def __init__(self, *, arm_error: Exception | None = None):
        self.arm_error = arm_error
        self.armed = False

    async def arm(self):
        if self.arm_error:
            raise self.arm_error
        self.armed = True


class _FakeSystem:
    def __init__(
        self,
        *,
        connects: bool = True,
        preflight_ready: bool = True,
        home: tuple[float, float] = (1.0, 2.0),
        upload_error: Exception | None = None,
        arm_error: Exception | None = None,
        start_error: Exception | None = None,
        progress_sequence: list[tuple[int, int]] | None = None,
        finish_after_last_progress: bool = True,
    ):
        self.core = _FakeCore(connects=connects)
        self.telemetry = _FakeTelemetry(preflight_ready=preflight_ready, home=home)
        self.mission = _FakeMissionPlugin(
            upload_error=upload_error,
            start_error=start_error,
            progress_sequence=progress_sequence,
            finish_after_last_progress=finish_after_last_progress,
        )
        self.action = _FakeActionPlugin(arm_error=arm_error)

    async def connect(self, system_address: str | None = None):
        return None


@pytest.fixture()
def install_fake_mavsdk(monkeypatch):
    """
    Returns a function `install(fake_system)` that wires everything up:
    injects fake `mavsdk`/`mavsdk.action`/`mavsdk.mission` modules,
    re-imports `app.mavlink.px4_client` against them, and stubs its
    `to_mavsdk_mission_plan` so these tests exercise ONLY the
    orchestration in px4_client.py -- Phase 6's converter already has
    its own dedicated tests in test_phase6_converter.py.
    """

    def install(fake_system: _FakeSystem):
        fake_action_module = types.ModuleType("mavsdk.action")
        fake_action_module.ActionError = _FakeActionError

        fake_mission_module = types.ModuleType("mavsdk.mission")
        fake_mission_module.MissionError = _FakeMissionError

        fake_mavsdk_package = types.ModuleType("mavsdk")
        fake_mavsdk_package.System = lambda: fake_system
        fake_mavsdk_package.action = fake_action_module
        fake_mavsdk_package.mission = fake_mission_module

        monkeypatch.setitem(sys.modules, "mavsdk", fake_mavsdk_package)
        monkeypatch.setitem(sys.modules, "mavsdk.action", fake_action_module)
        monkeypatch.setitem(sys.modules, "mavsdk.mission", fake_mission_module)
        monkeypatch.delitem(sys.modules, "app.mavlink.px4_client", raising=False)

        import app.mavlink.px4_client as px4_client_module

        monkeypatch.setattr(
            px4_client_module,
            "to_mavsdk_mission_plan",
            lambda mission, **kwargs: ConvertedMission(plan="FAKE_PLAN", return_to_launch_after_mission=True),
        )
        return px4_client_module

    return install


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


def test_successful_mission_is_accepted_with_full_progress_log(install_fake_mavsdk):
    fake_system = _FakeSystem(progress_sequence=[(1, 2), (2, 2)])
    px4_client = install_fake_mavsdk(fake_system)

    result = asyncio.run(
        px4_client.run_mission_on_px4(_mission(), connect_timeout_s=1.0, mission_timeout_s=1.0)
    )

    assert result.accepted is True
    assert result.stage == "complete"
    assert result.error is None
    assert result.progress_log == [(1, 2), (2, 2)]
    assert fake_system.action.armed is True
    assert fake_system.mission.uploaded_plan == "FAKE_PLAN"
    assert fake_system.mission.rtl_flag_set_to is True


# ---------------------------------------------------------------------
# Failure paths -- one per stage
# ---------------------------------------------------------------------


def test_connection_timeout_is_reported_as_rejected_at_connect_stage(install_fake_mavsdk):
    fake_system = _FakeSystem(connects=False)
    px4_client = install_fake_mavsdk(fake_system)

    result = asyncio.run(
        px4_client.run_mission_on_px4(_mission(), connect_timeout_s=0.05, mission_timeout_s=1.0)
    )

    assert result.accepted is False
    assert result.stage == "connect"
    assert "Timed out" in result.error


def test_preflight_not_ready_is_reported_as_rejected_at_preflight_stage(install_fake_mavsdk):
    fake_system = _FakeSystem(preflight_ready=False)
    px4_client = install_fake_mavsdk(fake_system)

    result = asyncio.run(
        px4_client.run_mission_on_px4(_mission(), connect_timeout_s=0.05, mission_timeout_s=1.0)
    )

    assert result.accepted is False
    assert result.stage == "preflight"


def test_upload_rejection_is_reported_at_upload_stage(install_fake_mavsdk):
    fake_system = _FakeSystem(upload_error=_FakeMissionError("no landing defined"))
    px4_client = install_fake_mavsdk(fake_system)

    result = asyncio.run(
        px4_client.run_mission_on_px4(_mission(), connect_timeout_s=1.0, mission_timeout_s=1.0)
    )

    assert result.accepted is False
    assert result.stage == "upload"
    assert "no landing defined" in result.error
    # We still know what RTL behavior *would* have been set.
    assert result.return_to_launch_after_mission is True


def test_arm_rejection_is_reported_at_arm_stage(install_fake_mavsdk):
    fake_system = _FakeSystem(arm_error=_FakeActionError("pre-arm check failed"))
    px4_client = install_fake_mavsdk(fake_system)

    result = asyncio.run(
        px4_client.run_mission_on_px4(_mission(), connect_timeout_s=1.0, mission_timeout_s=1.0)
    )

    assert result.accepted is False
    assert result.stage == "arm"
    assert "pre-arm check failed" in result.error
    # Upload should have already succeeded before arm was attempted.
    assert fake_system.mission.uploaded_plan == "FAKE_PLAN"


def test_start_rejection_is_reported_at_start_stage(install_fake_mavsdk):
    fake_system = _FakeSystem(start_error=_FakeMissionError("mission not uploaded"))
    px4_client = install_fake_mavsdk(fake_system)

    result = asyncio.run(
        px4_client.run_mission_on_px4(_mission(), connect_timeout_s=1.0, mission_timeout_s=1.0)
    )

    assert result.accepted is False
    assert result.stage == "start"
    assert fake_system.action.armed is True  # armed before start was attempted


def test_mission_that_never_finishes_times_out_at_monitor_stage(install_fake_mavsdk):
    fake_system = _FakeSystem(progress_sequence=[(1, 3)], finish_after_last_progress=False)
    px4_client = install_fake_mavsdk(fake_system)

    result = asyncio.run(
        px4_client.run_mission_on_px4(_mission(), connect_timeout_s=1.0, mission_timeout_s=0.05)
    )

    assert result.accepted is False
    assert result.stage == "monitor"
    assert "did not finish" in result.error
    assert result.progress_log == [(1, 3)]


# ---------------------------------------------------------------------
# RAMR novelty Phase 1 (Mission State Management) integration --
# additive `on_progress` hook only. Every test above passes with this
# hook untouched, proving it's backward compatible; this test proves the
# hook actually fires and can drive a MissionStateManager end to end.
# ---------------------------------------------------------------------


def test_on_progress_hook_drives_mission_state_manager(install_fake_mavsdk):
    from app.mission.state import MissionStateManager, MissionStatus

    fake_system = _FakeSystem(progress_sequence=[(1, 3), (2, 3), (3, 3)])
    px4_client = install_fake_mavsdk(fake_system)

    manager = MissionStateManager()
    manager.start(_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()

    result = asyncio.run(
        px4_client.run_mission_on_px4(
            _mission(),
            connect_timeout_s=1.0,
            mission_timeout_s=1.0,
            on_progress=manager.record_progress,
        )
    )
    manager.complete()

    assert result.accepted is True
    state = manager.snapshot()
    assert state.mission_status == MissionStatus.COMPLETED
    # Every progress event the fake mission plugin emitted reached the
    # state manager via the new hook.
    assert state.last_progress == (3, 3)
