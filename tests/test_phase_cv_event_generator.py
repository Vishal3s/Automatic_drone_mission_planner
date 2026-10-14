"""
RAMR/CV novelty Phase 13 tests -- CV Event Generation.
Run with: pytest tests/test_phase_cv_event_generator.py -v

No PX4/Gazebo/network needed -- reuses Phase 12's synthetic-image
approach (numpy + OpenCV) to produce real InspectionQualityResults, then
exercises CVEventGenerator and the TriggerEngine against them.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.mavlink.drone_state import DroneState
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager
from app.trigger.bus import EventBus
from app.trigger.engine import TriggerEngine
from app.trigger.events import EventSeverity, EventSource, EventType
from app.vision.camera_stream import CameraStreamManager, InspectionQualityAnalyzer, InspectionQualityResult, QualityStatus
from app.vision.event_generator import CVEventGenerator


# ---------------------------------------------------------------------
# Synthetic images -- same approach as Phase 12
# ---------------------------------------------------------------------


def _normal_image() -> np.ndarray:
    rng = np.random.default_rng(seed=11)
    return rng.integers(90, 165, size=(200, 200), dtype=np.uint8)


def _blurred_image() -> np.ndarray:
    return cv2.GaussianBlur(_normal_image(), (25, 25), sigmaX=10)


def _darkened_image() -> np.ndarray:
    return (_normal_image().astype(np.float64) * 0.2).astype(np.uint8)


def _overexposed_image() -> np.ndarray:
    return np.clip(_normal_image().astype(np.float64) * 0.3 + 200, 0, 255).astype(np.uint8)


def _mission() -> Mission:
    return Mission(
        mission_name="Inspection survey",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.000, longitude=2.000, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.010, longitude=2.000, altitude=30),
            MissionItem(sequence=4, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


def _manager_at(current: int = 2) -> MissionStateManager:
    manager = MissionStateManager()
    manager.start(_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=current, total=4)
    return manager


def _drone_state() -> DroneState:
    return DroneState(latitude_deg=1.010, longitude_deg=2.000, relative_altitude_m=30.0)


def _result_for(image, *, waypoint_current: int = 3) -> tuple[InspectionQualityResult, "InspectionFrame"]:
    """Runs a real image through the real Phase 12 pipeline to get a genuine InspectionQualityResult."""
    manager = _manager_at(current=waypoint_current)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)
    frame = csm.capture_frame(image, manager.snapshot(), _drone_state())
    return analyzer.results()[-1], frame


# ---------------------------------------------------------------------
# generate_event() -- mapping and shape
# ---------------------------------------------------------------------


def test_good_image_produces_no_event():
    result, _frame = _result_for(_normal_image())
    assert result.quality_status == QualityStatus.GOOD
    event = CVEventGenerator().generate_event(result)
    assert event is None


def test_blurry_image_produces_image_blur_detected():
    result, _frame = _result_for(_blurred_image())
    assert result.quality_status == QualityStatus.BLURRY
    event = CVEventGenerator().generate_event(result)
    assert event is not None
    assert event.event_type == EventType.IMAGE_BLUR_DETECTED
    assert event.source == EventSource.COMPUTER_VISION
    assert event.severity == EventSeverity.MEDIUM


@pytest.mark.parametrize("image_factory", [_darkened_image, _overexposed_image])
def test_exposure_problems_produce_bad_exposure_detected(image_factory):
    result, _frame = _result_for(image_factory())
    assert result.quality_status in (QualityStatus.TOO_DARK, QualityStatus.TOO_BRIGHT)
    event = CVEventGenerator().generate_event(result)
    assert event is not None
    assert event.event_type == EventType.BAD_EXPOSURE_DETECTED


def test_event_carries_mission_id_and_waypoint_id():
    result, _frame = _result_for(_blurred_image(), waypoint_current=3)
    event = CVEventGenerator().generate_event(result)
    assert event.mission_id == result.mission_id
    assert event.waypoint_id == 3


def test_severity_is_configurable():
    result, _frame = _result_for(_blurred_image())
    generator = CVEventGenerator(severity=EventSeverity.HIGH)
    event = generator.generate_event(result)
    assert event.severity == EventSeverity.HIGH


def test_metadata_contains_the_spec_named_keys():
    result, _frame = _result_for(_blurred_image())
    event = CVEventGenerator().generate_event(result)
    assert "blur_score" in event.metadata
    assert "camera_information" in event.metadata
    assert "image_quality_information" in event.metadata
    assert event.metadata["blur_score"] == result.blur_score
    assert event.metadata["image_quality_information"] == result.as_dict()


def test_metadata_also_carries_exposure_score_for_a_blur_event():
    result, _frame = _result_for(_blurred_image())
    event = CVEventGenerator().generate_event(result)
    assert event.metadata["exposure_score"] == result.exposure_score


def test_camera_information_is_enriched_when_inspection_frame_is_supplied():
    result, frame = _result_for(_blurred_image())
    event = CVEventGenerator().generate_event(result, inspection_frame=frame)
    assert event.metadata["camera_information"]["drone_position"] == frame.drone_position.as_dict()


def test_camera_information_omits_drone_position_when_no_frame_supplied():
    result, _frame = _result_for(_blurred_image())
    event = CVEventGenerator().generate_event(result)  # no inspection_frame
    assert "drone_position" not in event.metadata["camera_information"]
    assert event.metadata["camera_information"]["waypoint_id"] == result.waypoint_id


def test_unmapped_quality_status_falls_back_to_inspection_quality_failure():
    """Defensive fallback path -- exercised directly since no real check produces an unmapped status today."""
    import datetime as _dt

    class _FakeResult:
        mission_id = "fake-mission"
        waypoint_id = 5
        blur_score = 500.0
        exposure_score = 128.0
        quality_status = "SOMETHING_ELSE"  # not GOOD/BLURRY/TOO_DARK/TOO_BRIGHT
        timestamp = _dt.datetime.now(_dt.timezone.utc)

        def as_dict(self):
            return {"mission_id": self.mission_id, "quality_status": "SOMETHING_ELSE"}

    event = CVEventGenerator().generate_event(_FakeResult())
    assert event.event_type == EventType.INSPECTION_QUALITY_FAILURE


def test_generate_event_returns_at_most_one_event_per_result():
    result, _frame = _result_for(_blurred_image())
    event = CVEventGenerator().generate_event(result)
    # generate_event() itself is single-valued by construction (returns
    # ReplanningEvent | None) -- this assertion documents/locks that contract.
    assert event is None or isinstance(event.event_type, EventType)


# ---------------------------------------------------------------------
# SUCCESS CRITERIA: a bad image produces a valid event that reaches the TriggerEngine
# ---------------------------------------------------------------------


def test_blurry_image_event_reaches_and_activates_the_trigger_engine():
    result, frame = _result_for(_blurred_image())
    event = CVEventGenerator().generate_event(result, inspection_frame=frame)

    trigger_engine = TriggerEngine()
    decision = trigger_engine.handle_event(event)

    assert decision.should_replan is True
    assert decision.request is not None
    assert decision.request.event.event_type == EventType.IMAGE_BLUR_DETECTED
    assert decision.request.event.source == EventSource.COMPUTER_VISION


def test_exposure_problem_event_reaches_and_activates_the_trigger_engine():
    result, frame = _result_for(_darkened_image())
    event = CVEventGenerator().generate_event(result, inspection_frame=frame)

    trigger_engine = TriggerEngine()
    decision = trigger_engine.handle_event(event)

    assert decision.should_replan is True
    assert decision.request.event.event_type == EventType.BAD_EXPOSURE_DETECTED


def test_full_pipeline_via_event_bus_reaches_trigger_engine():
    """Camera Frame -> CV Analyzer -> Inspection Quality Result -> CV Event Generator -> ReplanningEvent -> TriggerEngine, via EventBus (the same decoupled publish path Phase 3 built for telemetry)."""
    manager = _manager_at(current=3)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)

    frame = csm.capture_frame(_blurred_image(), manager.snapshot(), _drone_state())
    result = analyzer.results()[-1]

    event = CVEventGenerator().generate_event(result, inspection_frame=frame)
    assert event is not None

    bus = EventBus()
    trigger_engine = TriggerEngine()
    bus.subscribe(trigger_engine.handle_event)
    bus.publish(event)

    decisions = trigger_engine.decision_log()
    assert len(decisions) == 1
    assert decisions[0].should_replan is True
    assert decisions[0].event.event_type == EventType.IMAGE_BLUR_DETECTED


def test_good_image_produces_no_event_and_therefore_nothing_reaches_trigger_engine():
    result, frame = _result_for(_normal_image())
    event = CVEventGenerator().generate_event(result, inspection_frame=frame)
    assert event is None  # nothing to hand to the TriggerEngine at all
