"""
RAMR/CV novelty Phase 14 tests -- CV-Triggered Adaptive Replanning.
Run with: pytest tests/test_phase_cv_triggered_replanning.py -v

Two layers, matching the spec's own FLOW and SUCCESS CRITERIA:

1. Unit tests against `AdaptiveMissionReplanner._handle_cv_quality_event()`
   directly (app/replanning/replanner.py) -- exercise each of the 7
   questions the spec lists, with hand-checked haversine-distance math
   (same style as tests/test_phase_replanner.py's battery tests).
2. One full-chain test through `RAMROrchestrator` (Phase 10, unchanged --
   it was never CV-specific) using a REAL `CVEventGenerator`-produced
   event from a REAL blurry synthetic image run through the REAL Phase
   11/12 camera pipeline, with a fake MAVSDK System standing in for PX4
   -- proving the spec's SUCCESS CRITERIA end to end: "A CV quality
   failure can cause safe mission adaptation."

No PX4/Gazebo/network needed -- everything here is synthetic/offline,
same as every earlier phase's tests.
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
from app.replanning.orchestrator import OrchestrationStage, RAMROrchestrator, ReplanSource
from app.replanning.replanner import AdaptiveMissionReplanner, RAMRStatus
from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent

HOME = (1.0000, 2.0000)


def _five_waypoint_mission() -> Mission:
    """Same shape as test_phase_replanner.py's helper -- WP1..WP4 (+TAKEOFF/RTL) ~1km apart from HOME."""
    return Mission(
        mission_name="5-waypoint inspection survey",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0000, longitude=2.0000, altitude=30),  # WP1
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.0090, longitude=2.0000, altitude=30),  # WP2 -- drone is here
            MissionItem(sequence=4, command=MissionCommand.WAYPOINT, latitude=1.0180, longitude=2.0000, altitude=30),  # WP3
            MissionItem(sequence=5, command=MissionCommand.WAYPOINT, latitude=1.0270, longitude=2.0000, altitude=30),  # WP4
            MissionItem(sequence=6, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


def _running_manager_at(current_sequence: int = 3, total: int = 6) -> MissionStateManager:
    manager = MissionStateManager()
    manager.start(_five_waypoint_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=current_sequence, total=total)
    return manager


def _cv_event(
    *,
    event_type: EventType = EventType.IMAGE_BLUR_DETECTED,
    severity: EventSeverity = EventSeverity.HIGH,
    waypoint_id: int | None = 2,
    mission_id: str | None = None,
) -> ReplanningEvent:
    """A hand-built CV event -- mirrors what app.vision.event_generator.CVEventGenerator actually produces."""
    return ReplanningEvent(
        event_type=event_type,
        severity=severity,
        source=EventSource.COMPUTER_VISION,
        mission_id=mission_id,
        waypoint_id=waypoint_id,
        metadata={"blur_score": 12.5},
    )


# ---------------------------------------------------------------------
# Question-by-question unit tests against the deterministic handler
# ---------------------------------------------------------------------


def test_no_waypoint_id_is_unsupported():
    """Question 1 precondition: can't decide anything to revisit without a waypoint_id."""
    manager = _running_manager_at()
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.30)
    event = _cv_event(waypoint_id=None)

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=HOME[0], home_longitude_deg=HOME[1]
    )
    assert result.status == RAMRStatus.UNSUPPORTED


def test_unknown_waypoint_id_is_unsupported():
    manager = _running_manager_at()
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.30)
    event = _cv_event(waypoint_id=99)

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=HOME[0], home_longitude_deg=HOME[1]
    )
    assert result.status == RAMRStatus.UNSUPPORTED


def test_missing_drone_position_is_unsupported():
    manager = _running_manager_at()
    drone_state = DroneState(battery_remaining_percent=0.30)  # no lat/lon
    event = _cv_event()

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=HOME[0], home_longitude_deg=HOME[1]
    )
    assert result.status == RAMRStatus.UNSUPPORTED


def test_missing_home_position_is_unsupported():
    manager = _running_manager_at()
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.30)
    event = _cv_event()

    result = AdaptiveMissionReplanner().replan(event, manager.snapshot(), drone_state)
    assert result.status == RAMRStatus.UNSUPPORTED


def test_question_7_battery_cannot_finish_original_route_returns_home():
    """Battery so low even the already-planned remaining route is infeasible -- RTH, correction moot."""
    manager = _running_manager_at()
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.16)
    event = _cv_event(mission_id=manager.snapshot().mission_id)

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=HOME[0], home_longitude_deg=HOME[1]
    )

    assert result.status == RAMRStatus.MODIFIED
    items = result.modified_mission.mission_items
    assert [i.command for i in items] == [MissionCommand.RETURN_TO_LAUNCH]
    assert "returning home" in result.reason.lower()
    assert result.validation.valid is True


def test_question_3_battery_insufficient_for_correction_continues_unchanged():
    """Route itself is affordable, but adding the revisit detour is not -- continue without correction."""
    manager = _running_manager_at()
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.26)
    event = _cv_event(mission_id=manager.snapshot().mission_id)

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=HOME[0], home_longitude_deg=HOME[1]
    )

    assert result.status == RAMRStatus.UNCHANGED
    assert result.modified_mission is None
    assert "not sufficient for the correction" in result.reason
    assert "continuing the original mission without a revisit" in result.reason


def test_questions_4_5_high_severity_inserts_revisit_immediately():
    """Battery affords the correction; HIGH severity -> revisit right after current position."""
    manager = _running_manager_at()
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.30)
    event = _cv_event(severity=EventSeverity.HIGH, mission_id=manager.snapshot().mission_id)

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=HOME[0], home_longitude_deg=HOME[1]
    )

    assert result.status == RAMRStatus.MODIFIED
    items = result.modified_mission.mission_items
    # revisit (LOITER at WP1's coords) is FIRST, ahead of the rest of the remaining route.
    assert items[0].command == MissionCommand.LOITER
    assert (items[0].latitude, items[0].longitude) == (1.0000, 2.0000)
    assert [i.command for i in items[1:]] == [
        MissionCommand.WAYPOINT, MissionCommand.WAYPOINT, MissionCommand.RETURN_TO_LAUNCH,
    ]
    assert items[-1].command == MissionCommand.RETURN_TO_LAUNCH
    assert result.validation.valid is True
    assert "immediately" in result.reason


def test_questions_4_6_medium_severity_defers_revisit_to_end_of_route():
    """Battery affords the correction; MEDIUM severity -> revisit deferred to just before RTH."""
    manager = _running_manager_at()
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.30)
    event = _cv_event(severity=EventSeverity.MEDIUM, mission_id=manager.snapshot().mission_id)

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=HOME[0], home_longitude_deg=HOME[1]
    )

    assert result.status == RAMRStatus.MODIFIED
    items = result.modified_mission.mission_items
    assert [i.command for i in items] == [
        MissionCommand.WAYPOINT, MissionCommand.WAYPOINT, MissionCommand.LOITER, MissionCommand.RETURN_TO_LAUNCH,
    ]
    revisit = items[2]
    assert (revisit.latitude, revisit.longitude) == (1.0000, 2.0000)
    assert items[-1].command == MissionCommand.RETURN_TO_LAUNCH
    assert result.validation.valid is True
    assert "after completing the rest of the remaining route" in result.reason


@pytest.mark.parametrize(
    "event_type", [EventType.IMAGE_BLUR_DETECTED, EventType.BAD_EXPOSURE_DETECTED, EventType.INSPECTION_QUALITY_FAILURE]
)
def test_all_three_cv_event_types_are_handled_deterministically(event_type):
    """All three CV quality-failure event types from Phase 13 route through the same handler."""
    manager = _running_manager_at()
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, battery_remaining_percent=0.30)
    event = _cv_event(event_type=event_type, mission_id=manager.snapshot().mission_id)

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=HOME[0], home_longitude_deg=HOME[1]
    )
    assert result.status in (RAMRStatus.MODIFIED, RAMRStatus.UNCHANGED, RAMRStatus.UNSUPPORTED)
    assert result.status != RAMRStatus.UNHANDLED_EVENT_TYPE


# ---------------------------------------------------------------------
# Full chain: real CV pipeline -> real TriggerEngine -> real RAMROrchestrator
# -> fake MAVSDK. SUCCESS CRITERIA: "A CV quality failure can cause safe
# mission adaptation."
# ---------------------------------------------------------------------

cv2 = pytest.importorskip("cv2")


class _FakeMissionError(Exception):
    pass


class _FakeMissionProgress:
    def __init__(self, current: int, total: int):
        self.current = current
        self.total = total


class _FakeMissionPlugin:
    def __init__(self):
        self.call_log: list[str] = []
        self.uploaded_plans: list[object] = []

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
        total = len(self.uploaded_plans[-1].mission_items) if self.uploaded_plans else 0
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
    """Same fake-MAVSDK-at-sys.modules pattern as tests/test_phase_orchestrator.py -- see that file's
    fixture docstring for why `sys.modules[...]` is read directly rather than via `import ... as`."""
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


def _blurred_image():
    import numpy as np

    rng = np.random.default_rng(seed=11)
    normal = rng.integers(90, 165, size=(200, 200), dtype=np.uint8)
    return cv2.GaussianBlur(normal, (25, 25), sigmaX=10)


def test_cv_quality_failure_causes_safe_mission_adaptation_end_to_end(install_fake_mavsdk):
    """
    SUCCESS CRITERIA, exercised literally: a real blurry frame, run
    through the real Phase 11/12 camera pipeline, produces a real
    IMAGE_BLUR_DETECTED event (Phase 13) which -- with ZERO CV-specific
    code in RAMROrchestrator -- reaches the TriggerEngine, is picked up
    by AdaptiveMissionReplanner's new Phase 14 handler, and results in a
    validated, safety-checked, MAVSDK-applied corrective mission.
    """
    from app.trigger.engine import TriggerEngine
    from app.vision.camera_stream import CameraStreamManager, InspectionQualityAnalyzer
    from app.vision.event_generator import CVEventGenerator

    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, relative_altitude_m=30.0, battery_remaining_percent=0.30)

    # -- Camera Frame -> CV Analyzer -> Inspection Quality Result (Phase 11/12, unmodified) --
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)
    # capture the bad frame while the manager still reports current_sequence=3 (WP2, drone's own
    # position) -- for this end-to-end test, what matters is that a REAL event with a REAL
    # waypoint_id comes out the other end; that waypoint_id is item #3 here.
    frame = csm.capture_frame(_blurred_image(), manager.snapshot(), drone_state)
    result = analyzer.results()[-1]

    # -- CV Event Generator -> ReplanningEvent (Phase 13, unmodified) --
    event = CVEventGenerator().generate_event(result, inspection_frame=frame)
    assert event is not None
    assert event.event_type == EventType.IMAGE_BLUR_DETECTED
    assert event.waypoint_id == 3

    # -- TriggerEngine -> AdaptiveMissionReplanner -> ... -> MAVSDK (Phase 3/10/14, unmodified except
    # for the new Phase 14 handler inside AdaptiveMissionReplanner) --
    drone = _FakeSystem()
    orchestrator = RAMROrchestrator()
    orch_result = asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=HOME[0], home_longitude_deg=HOME[1],
        )
    )

    assert orch_result.replan_source == ReplanSource.DETERMINISTIC
    assert orch_result.stage == OrchestrationStage.APPLIED
    assert orch_result.accepted
    assert orch_result.safety_result is not None
    assert orch_result.safety_result.approved is True
    # A revisit waypoint (LOITER at WP2's own coordinates -- item #3 in this mission) was actually
    # uploaded to the (fake) flight controller.
    uploaded_items = drone.mission.uploaded_plans[0].mission_items
    assert any(i.command == MissionCommand.LOITER for i in uploaded_items)
    assert "pause" in drone.mission.call_log and "upload" in drone.mission.call_log


def test_good_image_never_reaches_the_replanner_at_all(install_fake_mavsdk):
    """The other half of the SUCCESS CRITERIA: a GOOD image causes no adaptation at all."""
    from app.vision.camera_stream import CameraStreamManager, InspectionQualityAnalyzer
    from app.vision.event_generator import CVEventGenerator

    import numpy as np

    manager = _running_manager_at(current_sequence=3)
    drone_state = DroneState(latitude_deg=1.0090, longitude_deg=2.0000, relative_altitude_m=30.0, battery_remaining_percent=0.30)

    rng = np.random.default_rng(seed=11)
    normal_image = rng.integers(90, 165, size=(200, 200), dtype=np.uint8)

    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)
    frame = csm.capture_frame(normal_image, manager.snapshot(), drone_state)
    result = analyzer.results()[-1]

    event = CVEventGenerator().generate_event(result, inspection_frame=frame)
    assert event is None  # nothing to hand to the TriggerEngine/orchestrator at all
