"""
RAMR novelty Phase 10 tests -- RAMR Integration Testing.
Run with: pytest tests/test_phase_orchestrator.py -v

Covers the spec's own 6 TEST SCENARIOS end-to-end through
`app.replanning.orchestrator.RAMROrchestrator`, using:
  - the SAME fake-MAVSDK-at-sys.modules pattern
    `tests/test_phase_mission_update_manager.py` established (extended
    here with a fake Action plugin, since the orchestrator's fallback/
    bare-action execution needs `drone.action.return_to_launch()` /
    `.land()` / `.hold()`), and
  - the SAME fake-LLM-client pattern `tests/test_phase_reasoner.py`
    established, for the one scenario (invalid replanning proposal) that
    needs the LLM escalation path.

IMPORTANT -- what these tests DO and DO NOT prove: same caveat as Phase
9's own tests -- they prove the FULL PIPELINE (TriggerEngine ->
AdaptiveMissionReplanner -> [ReplanningContextBuilder -> RAG+LLM] ->
Safety Validator -> Battery Checker -> MissionUpdateManager -> MAVSDK)
calls every component in the right order with the right data. They do
NOT prove real PX4 SITL behaves this way in flight -- see
`app/replanning/orchestrator.py`'s own docstring and the README.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.mavlink.drone_state import DroneState
from app.mission.converter import ConvertedMission
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager
from app.replanning.adaptive_reasoner import RecommendedAction, RevisedMissionProposal
from app.replanning.orchestrator import OrchestrationStage, RAMROrchestrator, ReplanSource
from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent

HOME_LAT, HOME_LON = 1.0000, 2.0000


# ---------------------------------------------------------------------
# Shared mission / state helpers
# ---------------------------------------------------------------------


def _mission() -> Mission:
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


def _manager_at(current: int, total: int = 7) -> MissionStateManager:
    manager = MissionStateManager()
    manager.start(_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=current, total=total)
    return manager


def _drone_state(*, lat=1.020, lon=2.000, battery=0.30) -> DroneState:
    return DroneState(latitude_deg=lat, longitude_deg=lon, relative_altitude_m=30.0, battery_remaining_percent=battery)


def _event(event_type: EventType, severity: EventSeverity, mission_id: str, **metadata) -> ReplanningEvent:
    return ReplanningEvent(
        event_type=event_type, severity=severity, source=EventSource.TELEMETRY,
        mission_id=mission_id, metadata=metadata,
    )


# ---------------------------------------------------------------------
# Fake MAVSDK (mission + action plugins) -- mirrors test_phase_mission_update_manager.py
# ---------------------------------------------------------------------


class _FakeMissionError(Exception):
    pass


class _FakeMissionProgress:
    def __init__(self, current: int, total: int):
        self.current = current
        self.total = total


class _FakeMissionPlugin:
    def __init__(self, *, post_update_progress_factory=None):
        self.call_log: list[str] = []
        self.uploaded_plans: list[object] = []
        # By default, verify_progress against whatever plan was just
        # uploaded, so tests don't need to hand-compute exact counts.
        self._post_update_progress_factory = post_update_progress_factory

    async def pause_mission(self):
        self.call_log.append("pause")

    async def clear_mission(self):
        self.call_log.append("clear")

    async def upload_mission(self, plan):
        self.call_log.append("upload")
        self.uploaded_plans.append(plan)

    async def set_return_to_launch_after_mission(self, enable: bool):
        self.call_log.append(f"set_rtl({enable})")

    async def start_mission(self):
        self.call_log.append("start")

    async def set_current_mission_item(self, index: int):
        self.call_log.append("set_current_item")

    async def mission_progress(self):
        if self.uploaded_plans:
            total = len(self.uploaded_plans[-1].mission_items)
        else:
            total = 0
        yield _FakeMissionProgress(1, total)


class _FakeActionPlugin:
    def __init__(self):
        self.call_log: list[str] = []

    async def return_to_launch(self):
        self.call_log.append("return_to_launch")

    async def land(self):
        self.call_log.append("land")

    async def hold(self):
        self.call_log.append("hold")


class _FakeSystem:
    def __init__(self):
        self.mission = _FakeMissionPlugin()
        self.action = _FakeActionPlugin()


@pytest.fixture()
def install_fake_mavsdk(monkeypatch):
    """
    Deliberately does NOT delete/reimport `app.mavlink.mission_update_manager`
    (unlike `tests/test_phase_mission_update_manager.py`'s own fixture) --
    `app.replanning.orchestrator` imports `MissionUpdateManager` directly at
    module load time (at test collection), so a reload here would leave
    `RAMROrchestrator` holding a reference to the OLD module's class while
    this fixture patches a NEW module object, and the patch would never be
    seen. Patching the already-imported module's `to_mavsdk_mission_plan`
    attribute in place works for every caller, old or new, since they all
    share the one module object that's actually in `sys.modules`.

    Fetched via `sys.modules[...]` directly, NOT `import ... as mum_module`:
    when `tests/test_phase_mission_update_manager.py`'s own fixture runs
    first (it deletes and reimports this module once per test, then relies
    on monkeypatch's teardown to restore `sys.modules[...]` back to the
    original module afterwards), that restore only fixes the `sys.modules`
    DICT entry -- it does not fix the `app.mavlink` PACKAGE's
    `mission_update_manager` attribute, which Python's dotted `import x.y
    as z` statement actually resolves through. Using that statement here
    would silently hand back the LAST reloaded (and now-stale) module from
    that other test file instead of the original one `RAMROrchestrator`
    was built against -- a real gotcha, verified by direct `id()` checks
    while writing this test.
    """
    fake_mission_module = types.ModuleType("mavsdk.mission")
    fake_mission_module.MissionError = _FakeMissionError
    fake_mavsdk_package = types.ModuleType("mavsdk")
    fake_mavsdk_package.mission = fake_mission_module

    monkeypatch.setitem(sys.modules, "mavsdk", fake_mavsdk_package)
    monkeypatch.setitem(sys.modules, "mavsdk.mission", fake_mission_module)

    mum_module = sys.modules["app.mavlink.mission_update_manager"]

    monkeypatch.setattr(
        mum_module,
        "to_mavsdk_mission_plan",
        lambda mission, **kwargs: ConvertedMission(
            plan=types.SimpleNamespace(mission_items=list(mission.mission_items)),
            return_to_launch_after_mission=True,
        ),
    )
    return mum_module


@pytest.fixture(autouse=True)
def _stub_rag(monkeypatch):
    monkeypatch.setattr("app.replanning.adaptive_reasoner.retrieve", lambda *a, **k: [])
    monkeypatch.setattr("app.replanning.adaptive_reasoner.format_context", lambda *a, **k: "")


# ---------------------------------------------------------------------
# Scenario 1: Battery deviation
# ---------------------------------------------------------------------


def test_scenario_1_battery_deviation_is_applied(install_fake_mavsdk):
    manager = _manager_at(current=4)  # drone at WP3 (item #4 of 7)
    drone_state = _drone_state(lat=1.020, lon=2.000, battery=0.22)
    event = _event(EventType.BATTERY_DEVIATION, EventSeverity.HIGH, manager.snapshot().mission_id, battery_percent=0.22)
    drone = _FakeSystem()

    orchestrator = RAMROrchestrator()
    result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
        )
    )

    assert result.replan_source == ReplanSource.DETERMINISTIC
    assert result.stage == OrchestrationStage.APPLIED
    assert result.accepted
    assert drone.mission.call_log == ["pause", "clear", "upload", "set_rtl(True)", "start"]
    # Never restarts from WP1/home.
    uploaded = drone.mission.uploaded_plans[0].mission_items
    assert all((i.latitude, i.longitude) != (1.000, 2.000) for i in uploaded if i.latitude is not None)


# ---------------------------------------------------------------------
# Scenario 2: Missed waypoint
# ---------------------------------------------------------------------


def test_scenario_2_missed_waypoint_drops_it_and_applies(install_fake_mavsdk):
    manager = _manager_at(current=4)  # drone at WP3 (item #4)
    drone_state = _drone_state(lat=1.020, lon=2.000, battery=0.80)
    event = _event(
        EventType.MISSED_WAYPOINT, EventSeverity.MEDIUM, manager.snapshot().mission_id, missed_sequence=5,  # WP4
    )
    drone = _FakeSystem()

    orchestrator = RAMROrchestrator()
    result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
        )
    )

    assert result.replan_source == ReplanSource.DETERMINISTIC
    assert result.proposal.recommended_action == RecommendedAction.SKIP_WAYPOINTS
    assert result.proposal.skipped_waypoints == [5]
    assert result.stage == OrchestrationStage.APPLIED
    uploaded = drone.mission.uploaded_plans[0].mission_items
    assert all(abs(i.latitude - 1.030) > 1e-9 for i in uploaded if i.latitude is not None)  # WP4 dropped


# ---------------------------------------------------------------------
# Scenario 3: Mission deviation
# ---------------------------------------------------------------------


def test_scenario_3_high_severity_mission_deviation_commands_rth(install_fake_mavsdk):
    manager = _manager_at(current=4)
    drone_state = _drone_state(lat=1.020, lon=2.000, battery=0.80)
    event = _event(EventType.MISSION_DEVIATION, EventSeverity.CRITICAL, manager.snapshot().mission_id)
    drone = _FakeSystem()

    orchestrator = RAMROrchestrator()
    result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
        )
    )

    assert result.replan_source == ReplanSource.DETERMINISTIC
    assert result.proposal.recommended_action == RecommendedAction.RETURN_TO_LAUNCH
    assert result.stage == OrchestrationStage.APPLIED
    uploaded = drone.mission.uploaded_plans[0].mission_items
    assert len(uploaded) == 1
    assert uploaded[0].command == MissionCommand.RETURN_TO_LAUNCH


def test_low_severity_mission_deviation_is_no_change(install_fake_mavsdk):
    manager = _manager_at(current=4)
    drone_state = _drone_state(lat=1.020, lon=2.000, battery=0.80)
    event = _event(EventType.MISSION_DEVIATION, EventSeverity.MEDIUM, manager.snapshot().mission_id)
    drone = _FakeSystem()

    orchestrator = RAMROrchestrator()
    result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
        )
    )

    assert result.stage == OrchestrationStage.NO_CHANGE
    assert drone.mission.call_log == []


# ---------------------------------------------------------------------
# Scenario 4: Invalid replanning proposal (LLM escalation path)
# ---------------------------------------------------------------------


class _FakeLLMClient:
    def __init__(self, proposal: RevisedMissionProposal):
        self._proposal = proposal

    def generate_structured(self, **kwargs):
        return self._proposal


def test_scenario_4_invalid_llm_proposal_is_rejected_and_never_reaches_mavsdk(install_fake_mavsdk, monkeypatch):
    # SAFETY_EVENT is not one of AdaptiveMissionReplanner's 3 deterministic
    # triggers, so this escalates to the LLM path.
    invalid_proposal = RevisedMissionProposal(
        remaining_waypoints=[
            MissionItem(sequence=1, command=MissionCommand.WAYPOINT, altitude=30),  # missing lat/lon -- rule violation
            MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
        recommended_action=RecommendedAction.CONTINUE,
        reason="Continue to remaining waypoints.",
        confidence=0.5,
    )
    fake_client = _FakeLLMClient(invalid_proposal)
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake_client)

    manager = _manager_at(current=4)
    drone_state = _drone_state(lat=1.020, lon=2.000, battery=0.80)
    event = _event(EventType.SAFETY_EVENT, EventSeverity.HIGH, manager.snapshot().mission_id)
    drone = _FakeSystem()

    orchestrator = RAMROrchestrator()
    result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
        )
    )

    assert result.replan_source == ReplanSource.LLM
    assert not result.safety_result.approved
    assert result.stage in (OrchestrationStage.REJECTED, OrchestrationStage.FALLBACK_EXECUTED)
    assert result.stage != OrchestrationStage.APPLIED
    # MissionUpdateManager (and therefore MAVSDK's mission plugin) is never reached.
    assert drone.mission.call_log == []
    # Battery is plenty (0.80) and the original remaining route is valid, so the
    # deterministic fallback should be "continue the original mission" -- a no-op.
    assert result.fallback_action is not None
    assert result.fallback_action.value == "CONTINUE_ORIGINAL_MISSION"
    assert result.action_executed is None


# ---------------------------------------------------------------------
# Scenario 5: Multiple simultaneous triggers
# ---------------------------------------------------------------------


def test_scenario_5_concurrent_triggers_exactly_one_is_applied(install_fake_mavsdk):
    manager = _manager_at(current=4)
    drone_state = _drone_state(lat=1.020, lon=2.000, battery=0.90)
    mission_id = manager.snapshot().mission_id
    event_a = _event(EventType.BATTERY_DEVIATION, EventSeverity.HIGH, mission_id, battery_percent=0.90)
    event_b = _event(EventType.BATTERY_DEVIATION, EventSeverity.HIGH, mission_id, battery_percent=0.90)
    drone = _FakeSystem()

    orchestrator = RAMROrchestrator()  # ONE shared instance -- required for the lock to matter

    async def _run():
        return await asyncio.gather(
            orchestrator.handle_event(
                event_a, mission_state_manager=manager, drone_state=drone_state, drone=drone,
                home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
            ),
            orchestrator.handle_event(
                event_b, mission_state_manager=manager, drone_state=drone_state, drone=drone,
                home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
            ),
        )

    result_a, result_b = asyncio.run(_run())
    stages = {result_a.stage, result_b.stage}

    # Exactly one request is ever actually applied; the other is rejected
    # (never queued) by one of the two concurrency guards.
    assert OrchestrationStage.APPLIED in stages
    rejected_stages = {OrchestrationStage.UPDATE_ALREADY_IN_PROGRESS, OrchestrationStage.MISSION_NOT_RUNNING}
    other_stage = (stages - {OrchestrationStage.APPLIED}).pop() if len(stages) == 2 else OrchestrationStage.APPLIED
    assert other_stage in rejected_stages or len(stages) == 1
    assert drone.mission.call_log.count("start") == 1  # only one update was ever actually uploaded+resumed


# ---------------------------------------------------------------------
# Scenario 6: Replanning while near mission completion
# ---------------------------------------------------------------------


def test_scenario_6_near_mission_completion_skips_replanning(install_fake_mavsdk):
    manager = _manager_at(current=7, total=7)  # only the trailing RETURN_TO_LAUNCH item remains
    drone_state = _drone_state(lat=1.040, lon=2.000, battery=0.05)  # even a severe trigger...
    event = _event(EventType.BATTERY_DEVIATION, EventSeverity.HIGH, manager.snapshot().mission_id, battery_percent=0.05)
    drone = _FakeSystem()

    orchestrator = RAMROrchestrator()
    result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
        )
    )

    assert result.stage == OrchestrationStage.NEAR_MISSION_COMPLETION
    assert result.accepted
    assert drone.mission.call_log == []
    assert drone.action.call_log == []


# ---------------------------------------------------------------------
# Extra: NOT_TRIGGERED and bare-action execution paths
# ---------------------------------------------------------------------


def test_below_threshold_event_is_not_triggered(install_fake_mavsdk):
    manager = _manager_at(current=4)
    drone_state = _drone_state()
    event = _event(EventType.BATTERY_DEVIATION, EventSeverity.LOW, manager.snapshot().mission_id)
    drone = _FakeSystem()

    orchestrator = RAMROrchestrator()
    result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
        )
    )
    assert result.stage == OrchestrationStage.NOT_TRIGGERED
    assert drone.mission.call_log == []


def test_bare_return_to_launch_recommendation_executes_via_action_plugin(install_fake_mavsdk, monkeypatch):
    bare_rtl_proposal = RevisedMissionProposal(
        recommended_action=RecommendedAction.RETURN_TO_LAUNCH,
        reason="Battery critical -- return immediately.",
        confidence=0.95,
    )
    fake_client = _FakeLLMClient(bare_rtl_proposal)
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake_client)

    manager = _manager_at(current=4)
    drone_state = _drone_state(lat=1.020, lon=2.000, battery=0.80)
    event = _event(EventType.SAFETY_EVENT, EventSeverity.HIGH, manager.snapshot().mission_id)
    drone = _FakeSystem()

    orchestrator = RAMROrchestrator()
    result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
        )
    )

    assert result.safety_result.approved
    assert result.safety_result.approved_mission is None
    assert result.stage == OrchestrationStage.ACTION_EXECUTED
    assert result.action_executed is True
    assert drone.action.call_log == ["return_to_launch"]
    assert drone.mission.call_log == []  # MissionUpdateManager never touched -- there was no Mission to upload


def test_approved_action_only_reported_honestly_when_no_drone_supplied(install_fake_mavsdk, monkeypatch):
    bare_rtl_proposal = RevisedMissionProposal(
        recommended_action=RecommendedAction.RETURN_TO_LAUNCH,
        reason="Battery critical -- return immediately.",
        confidence=0.95,
    )
    fake_client = _FakeLLMClient(bare_rtl_proposal)
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake_client)

    manager = _manager_at(current=4)
    drone_state = _drone_state(lat=1.020, lon=2.000, battery=0.80)
    event = _event(EventType.SAFETY_EVENT, EventSeverity.HIGH, manager.snapshot().mission_id)

    orchestrator = RAMROrchestrator()
    result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=None,
            home_latitude_deg=HOME_LAT, home_longitude_deg=HOME_LON,
        )
    )

    assert result.stage == OrchestrationStage.APPROVED_ACTION_ONLY
    assert result.action_executed is None
