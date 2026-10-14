"""
RAMR/CV novelty Phase 11 tests -- Camera Integration.
Run with: pytest tests/test_phase_camera_stream.py -v

No PX4/Gazebo/network needed -- `CameraStreamManager` never touches
MAVSDK or a real camera; frame sources here are synthetic (plain bytes),
matching `_demo_cli()`'s own approach (see that module's "Known
limitations").
"""

from __future__ import annotations

import asyncio

import pytest

from app.mavlink.drone_state import DroneState
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager
from app.vision.camera_stream import (
    CameraStreamManager,
    InspectionFrame,
    InspectionQualityAnalyzer,
    iter_frames,
)


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


def _manager_at(current: int, total: int = 4) -> MissionStateManager:
    manager = MissionStateManager()
    manager.start(_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=current, total=total)
    return manager


def _drone_state(*, lat=1.000, lon=2.000, alt=30.0) -> DroneState:
    return DroneState(latitude_deg=lat, longitude_deg=lon, relative_altitude_m=alt)


# ---------------------------------------------------------------------
# InspectionFrame
# ---------------------------------------------------------------------


def test_as_dict_is_json_serializable_shape_and_excludes_raw_frame():
    manager = _manager_at(current=2)
    drone_state = _drone_state()
    csm = CameraStreamManager()

    frame = csm.capture_frame(b"raw-bytes", manager.snapshot(), drone_state)
    d = frame.as_dict()

    assert d["mission_id"] == manager.snapshot().mission_id
    assert d["waypoint_id"] == 2
    assert d["drone_position"] == {"latitude_deg": 1.000, "longitude_deg": 2.000, "altitude_m": 30.0}
    assert "timestamp" in d
    assert d["frame_type"] == "bytes"
    assert d["frame_size_bytes"] == len(b"raw-bytes")
    assert "frame" not in d  # raw payload deliberately excluded


def test_frame_size_bytes_handles_numpy_like_object_without_numpy_dependency():
    class _FakeNdarray:
        nbytes = 12345

    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    frame = csm.capture_frame(_FakeNdarray(), manager.snapshot(), _drone_state())
    assert frame.as_dict()["frame_size_bytes"] == 12345


def test_frame_size_bytes_never_raises_for_an_unrecognized_type():
    class _Weird:
        pass

    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    frame = csm.capture_frame(_Weird(), manager.snapshot(), _drone_state())
    assert frame.as_dict()["frame_size_bytes"] is None


# ---------------------------------------------------------------------
# CameraStreamManager.capture_frame() -- tagging correctness (SUCCESS CRITERIA)
# ---------------------------------------------------------------------


def test_capture_frame_tags_current_mission_and_waypoint():
    manager = _manager_at(current=3)  # drone at WP2 (item #3)
    drone_state = _drone_state(lat=1.010, lon=2.000)
    csm = CameraStreamManager()

    frame = csm.capture_frame(b"frame-a", manager.snapshot(), drone_state)

    assert isinstance(frame, InspectionFrame)
    assert frame.mission_id == manager.snapshot().mission_id
    assert frame.waypoint_id == 3
    assert frame.drone_position.latitude_deg == 1.010
    assert frame.frame == b"frame-a"


def test_capture_frame_falls_back_to_current_sequence_when_no_current_waypoint():
    manager = MissionStateManager()
    manager.start(_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    # No record_progress() called yet -- current_waypoint is None, current_sequence is 0.
    csm = CameraStreamManager()
    frame = csm.capture_frame(b"x", manager.snapshot(), _drone_state())
    assert frame.waypoint_id is None


def test_frame_count_increments_across_calls():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    csm.capture_frame(b"1", manager.snapshot(), _drone_state())
    csm.capture_frame(b"2", manager.snapshot(), _drone_state())
    assert csm.frame_count == 2


# ---------------------------------------------------------------------
# Subscriber dispatch -- mirrors EventBus's own resilience contract
# ---------------------------------------------------------------------


def test_subscribers_all_receive_the_frame():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    received_a, received_b = [], []
    csm.subscribe(received_a.append)
    csm.subscribe(received_b.append)

    csm.capture_frame(b"x", manager.snapshot(), _drone_state())

    assert len(received_a) == 1 and len(received_b) == 1
    assert received_a[0] is received_b[0]


def test_one_subscriber_raising_does_not_block_others_or_the_caller():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()

    def _boom(frame):
        raise RuntimeError("boom")

    received = []
    csm.subscribe(_boom)
    csm.subscribe(received.append)

    csm.capture_frame(b"x", manager.snapshot(), _drone_state())  # must not raise

    assert len(received) == 1


# ---------------------------------------------------------------------
# CameraStreamManager.stream() -- continuous feed, live state per frame
# ---------------------------------------------------------------------


def test_stream_tags_each_frame_with_live_state_not_a_stale_snapshot():
    manager = _manager_at(current=2)  # drone at WP1
    state = {"drone": _drone_state(lat=1.000, lon=2.000)}
    csm = CameraStreamManager()

    async def _frame_source():
        yield b"frame-1"
        # Simulate the drone progressing to WP2 and telemetry updating
        # BETWEEN frames -- stream() must read fresh state each time.
        manager.record_progress(current=3, total=4)
        state["drone"] = _drone_state(lat=1.010, lon=2.000)
        yield b"frame-2"

    produced = asyncio.run(
        csm.stream(
            _frame_source(),
            mission_state_manager=manager,
            drone_state_provider=lambda: state["drone"],
        )
    )

    assert len(produced) == 2
    assert produced[0].waypoint_id == 2
    assert produced[0].drone_position.latitude_deg == 1.000
    assert produced[1].waypoint_id == 3
    assert produced[1].drone_position.latitude_deg == 1.010


def test_stream_respects_max_frames():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()

    produced = asyncio.run(
        csm.stream(
            iter_frames([b"1", b"2", b"3", b"4"]),
            mission_state_manager=manager,
            drone_state_provider=lambda: _drone_state(),
            max_frames=2,
        )
    )
    assert len(produced) == 2
    assert csm.frame_count == 2


def test_stream_runs_until_source_exhausted_when_max_frames_not_set():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    produced = asyncio.run(
        csm.stream(
            iter_frames([b"1", b"2", b"3"]),
            mission_state_manager=manager,
            drone_state_provider=lambda: _drone_state(),
        )
    )
    assert len(produced) == 3


# ---------------------------------------------------------------------
# InspectionQualityAnalyzer -- minimal receiver (per "verify the pipeline works")
# ---------------------------------------------------------------------


def test_analyzer_receives_and_logs_frames_without_analyzing_content():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)

    csm.capture_frame(b"x", manager.snapshot(), _drone_state())

    assert analyzer.received_count == 1
    assert isinstance(analyzer.received_frames()[0], InspectionFrame)


def test_analyzer_frames_for_waypoint_filters_correctly():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)

    csm.capture_frame(b"wp1-a", manager.snapshot(), _drone_state())
    csm.capture_frame(b"wp1-b", manager.snapshot(), _drone_state())
    manager.record_progress(current=3, total=4)
    csm.capture_frame(b"wp2-a", manager.snapshot(), _drone_state())

    assert len(analyzer.frames_for_waypoint(2)) == 2
    assert len(analyzer.frames_for_waypoint(3)) == 1
    assert [f.frame for f in analyzer.frames_for_waypoint(2)] == [b"wp1-a", b"wp1-b"]


def test_analyzer_frames_for_mission_filters_correctly():
    manager_a = _manager_at(current=2)
    manager_b = _manager_at(current=2)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)

    csm.capture_frame(b"a", manager_a.snapshot(), _drone_state())
    csm.capture_frame(b"b", manager_b.snapshot(), _drone_state())

    assert len(analyzer.frames_for_mission(manager_a.snapshot().mission_id)) == 1
    assert len(analyzer.frames_for_mission(manager_b.snapshot().mission_id)) == 1


# ---------------------------------------------------------------------
# End-to-end: SUCCESS CRITERIA, full pipeline
# ---------------------------------------------------------------------


def test_full_pipeline_receives_and_processes_frames_associated_with_waypoints():
    manager = _manager_at(current=2)
    csm = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    csm.subscribe(analyzer.receive_frame)

    produced = asyncio.run(
        csm.stream(
            iter_frames([b"f1", b"f2", b"f3"]),
            mission_state_manager=manager,
            drone_state_provider=lambda: _drone_state(),
        )
    )

    assert len(produced) == 3
    assert analyzer.received_count == 3
    assert all(f.mission_id == manager.snapshot().mission_id for f in produced)
    assert all(f.waypoint_id == 2 for f in produced)
    assert analyzer.frames_for_waypoint(2) == produced
