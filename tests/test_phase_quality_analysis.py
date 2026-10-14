"""
RAMR/CV novelty Phase 12 tests -- Basic Computer Vision Quality Analysis.
Run with: pytest tests/test_phase_quality_analysis.py -v

Uses REAL synthetic images (numpy + OpenCV, JPEG-encoded), per your own
TEST USING list: normal images, artificially blurred images, darkened
images, overexposed images. Not a real Gazebo camera frame -- see
`app/vision/camera_stream.py`'s "Known limitations" -- but real,
decodable image data, so `detect_blur()`/`detect_exposure()` exercise
their actual OpenCV logic, not a mocked-out stand-in.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.mavlink.drone_state import DroneState
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager
from app.vision.camera_stream import (
    BlurDetectionResult,
    CameraStreamManager,
    ExposureDetectionResult,
    ExposureStatus,
    FrameDecodeError,
    InspectionQualityAnalyzer,
    InspectionQualityResult,
    QualityStatus,
)


# ---------------------------------------------------------------------
# Synthetic test images -- your spec's own TEST USING list
# ---------------------------------------------------------------------


def _normal_image() -> np.ndarray:
    """Textured, mid-gray, well-exposed -- a real focus measure needs edges/texture to score against."""
    rng = np.random.default_rng(seed=7)
    return rng.integers(90, 165, size=(200, 200), dtype=np.uint8)


def _blurred_image() -> np.ndarray:
    return cv2.GaussianBlur(_normal_image(), (25, 25), sigmaX=10)


def _darkened_image() -> np.ndarray:
    return (_normal_image().astype(np.float64) * 0.2).astype(np.uint8)


def _overexposed_image() -> np.ndarray:
    # Shift bright AND compress the texture's amplitude (not just add a
    # flat offset) so the result is overexposed WITHOUT clipping so many
    # pixels to a flat 255 that it also destroys the Laplacian-variance
    # blur measure -- a real overexposed-but-still-in-focus photo, not a
    # solid white square.
    normal = _normal_image().astype(np.float64)
    return np.clip(normal * 0.3 + 200, 0, 255).astype(np.uint8)


def _jpeg_bytes(gray_array: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", gray_array)
    assert ok
    return buf.tobytes()


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
    return DroneState(latitude_deg=1.000, longitude_deg=2.000, relative_altitude_m=30.0)


def _inspection_frame(csm: CameraStreamManager, raw_frame, manager: MissionStateManager):
    return csm.capture_frame(raw_frame, manager.snapshot(), _drone_state())


# ---------------------------------------------------------------------
# detect_blur()
# ---------------------------------------------------------------------


def test_detect_blur_on_normal_image_is_not_blurry():
    analyzer = InspectionQualityAnalyzer()
    result = analyzer.detect_blur(_normal_image())
    assert isinstance(result, BlurDetectionResult)
    assert result.is_blurry is False
    assert result.blur_score > analyzer.blur_variance_threshold


def test_detect_blur_on_blurred_image_is_blurry_and_scores_lower():
    analyzer = InspectionQualityAnalyzer()
    normal_score = analyzer.detect_blur(_normal_image()).blur_score
    blurred_result = analyzer.detect_blur(_blurred_image())
    assert blurred_result.is_blurry is True
    assert blurred_result.blur_score < normal_score
    assert blurred_result.blur_score < analyzer.blur_variance_threshold


def test_detect_blur_accepts_encoded_jpeg_bytes_too():
    analyzer = InspectionQualityAnalyzer()
    result = analyzer.detect_blur(_jpeg_bytes(_normal_image()))
    assert result.is_blurry is False


def test_detect_blur_threshold_is_configurable():
    analyzer = InspectionQualityAnalyzer(blur_variance_threshold=1_000_000.0)
    # With an absurdly high threshold, even a sharp image is "blurry".
    result = analyzer.detect_blur(_normal_image())
    assert result.is_blurry is True


# ---------------------------------------------------------------------
# detect_exposure()
# ---------------------------------------------------------------------


def test_detect_exposure_on_normal_image_is_normal():
    analyzer = InspectionQualityAnalyzer()
    result = analyzer.detect_exposure(_normal_image())
    assert isinstance(result, ExposureDetectionResult)
    assert result.exposure_status == ExposureStatus.NORMAL


def test_detect_exposure_on_darkened_image_is_underexposed():
    analyzer = InspectionQualityAnalyzer()
    result = analyzer.detect_exposure(_darkened_image())
    assert result.exposure_status == ExposureStatus.UNDEREXPOSED
    assert result.exposure_score < analyzer.underexposed_mean_threshold


def test_detect_exposure_on_overexposed_image_is_overexposed():
    analyzer = InspectionQualityAnalyzer()
    result = analyzer.detect_exposure(_overexposed_image())
    assert result.exposure_status == ExposureStatus.OVEREXPOSED
    assert result.exposure_score > analyzer.overexposed_mean_threshold


def test_detect_exposure_thresholds_are_configurable():
    analyzer = InspectionQualityAnalyzer(underexposed_mean_threshold=0.0, overexposed_mean_threshold=255.0)
    # With a 0..255 "normal" band, nothing is ever under/over-exposed.
    assert analyzer.detect_exposure(_darkened_image()).exposure_status == ExposureStatus.NORMAL
    assert analyzer.detect_exposure(_overexposed_image()).exposure_status == ExposureStatus.NORMAL


# ---------------------------------------------------------------------
# FrameDecodeError -- undecodable input
# ---------------------------------------------------------------------


def test_detect_blur_raises_frame_decode_error_for_non_image_bytes():
    analyzer = InspectionQualityAnalyzer()
    with pytest.raises(FrameDecodeError):
        analyzer.detect_blur(b"not-an-image")


def test_detect_exposure_raises_frame_decode_error_for_unrecognized_type():
    analyzer = InspectionQualityAnalyzer()
    with pytest.raises(FrameDecodeError):
        analyzer.detect_exposure(object())


# ---------------------------------------------------------------------
# analyze() / InspectionQualityResult -- SUCCESS CRITERIA, all 4 classes
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "image_factory,expected_status",
    [
        (_normal_image, QualityStatus.GOOD),
        (_blurred_image, QualityStatus.BLURRY),
        (_darkened_image, QualityStatus.TOO_DARK),
        (_overexposed_image, QualityStatus.TOO_BRIGHT),
    ],
)
def test_analyze_correctly_classifies_each_of_the_four_categories(image_factory, expected_status):
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()

    inspection_frame = _inspection_frame(csm, image_factory(), manager)
    result = analyzer.analyze(inspection_frame)

    assert isinstance(result, InspectionQualityResult)
    assert result.quality_status == expected_status
    assert result.mission_id == manager.snapshot().mission_id
    assert result.waypoint_id == 2
    assert result.timestamp == inspection_frame.timestamp


def test_result_as_dict_shape_matches_spec_field_list_exactly():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    frame = _inspection_frame(csm, _normal_image(), manager)

    d = analyzer.analyze(frame).as_dict()
    assert set(d.keys()) == {"mission_id", "waypoint_id", "blur_score", "exposure_score", "quality_status", "timestamp"}


def test_blurry_takes_priority_over_a_simultaneous_exposure_problem():
    """Documented, deterministic priority when an image is both blurry and mis-exposed."""
    blurred_and_dark = cv2.GaussianBlur(_darkened_image(), (25, 25), sigmaX=10)
    analyzer = InspectionQualityAnalyzer()
    blur = analyzer.detect_blur(blurred_and_dark)
    exposure = analyzer.detect_exposure(blurred_and_dark)
    assert blur.is_blurry is True
    assert exposure.exposure_status == ExposureStatus.UNDEREXPOSED  # both conditions really are present

    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    frame = _inspection_frame(csm, blurred_and_dark, manager)
    result = analyzer.analyze(frame)
    assert result.quality_status == QualityStatus.BLURRY


# ---------------------------------------------------------------------
# receive_frame() integration -- Phase 11 behavior preserved, Phase 12 added
# ---------------------------------------------------------------------


def test_receive_frame_still_records_the_frame_like_phase_11():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)

    csm.capture_frame(_normal_image(), manager.snapshot(), _drone_state())

    assert analyzer.received_count == 1
    assert len(analyzer.frames_for_waypoint(2)) == 1  # Phase 11 accessor still works


def test_receive_frame_also_produces_a_quality_result_for_a_decodable_frame():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)

    csm.capture_frame(_normal_image(), manager.snapshot(), _drone_state())

    assert len(analyzer.results()) == 1
    assert analyzer.results()[0].quality_status == QualityStatus.GOOD
    assert analyzer.results_for_waypoint(2) == analyzer.results()
    assert analyzer.results_for_mission(manager.snapshot().mission_id) == analyzer.results()


def test_receive_frame_does_not_crash_or_lose_the_frame_on_undecodable_input():
    """Backward compatibility with Phase 11's own placeholder byte-string frames."""
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)

    csm.capture_frame(b"synthetic-frame-not-a-real-image", manager.snapshot(), _drone_state())

    assert analyzer.received_count == 1          # frame still recorded
    assert analyzer.results() == []               # but no quality result produced
    assert analyzer.frames_for_waypoint(2)[0].frame == b"synthetic-frame-not-a-real-image"


def test_full_pipeline_stream_produces_correct_classifications_per_waypoint():
    manager = _manager_at(current=2)  # WP1
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)

    csm.capture_frame(_normal_image(), manager.snapshot(), _drone_state())
    manager.record_progress(current=3, total=4)  # -> WP2
    csm.capture_frame(_darkened_image(), manager.snapshot(), _drone_state())

    wp1_results = analyzer.results_for_waypoint(2)
    wp2_results = analyzer.results_for_waypoint(3)
    assert len(wp1_results) == 1 and wp1_results[0].quality_status == QualityStatus.GOOD
    assert len(wp2_results) == 1 and wp2_results[0].quality_status == QualityStatus.TOO_DARK
