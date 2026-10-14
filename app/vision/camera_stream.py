"""
RAMR/CV novelty Phase 11 -- Camera Integration.

    OBJECTIVE: Add a camera stream to the drone simulation.
    CREATE: CameraStreamManager.
    CAMERA DATA SHOULD INCLUDE: Image/frame, timestamp, current drone
    position, current waypoint, mission ID.
    CREATE: InspectionFrame (frame, mission_id, waypoint_id,
    drone_position, timestamp).
    ARCHITECTURE: PX4 / Gazebo Camera -> CameraStreamManager ->
    InspectionQualityAnalyzer.
    DO NOT CONNECT CAMERA EVENTS TO RAMR YET. First verify that the
    camera pipeline works.
    SUCCESS CRITERIA: The backend can receive and process camera frames
    associated with mission waypoints.

Design notes:

- `InspectionFrame` matches your OUTPUT example field-for-field:
  `frame`, `mission_id`, `waypoint_id`, `drone_position`, `timestamp`.
  "Reuse existing data structures" (standing project rule): `drone_position`
  is the SAME `DronePosition` dataclass `app.replanning.context_builder`
  already defined (Phase 5) for exactly this purpose -- a focused
  lat/lon/altitude snapshot -- not a new, parallel position type.
  `mission_id` and `waypoint_id` are read from the SAME `MissionState`
  (Phase 1) every other module already reads mission/waypoint identity
  from; `waypoint_id` is that mission's `current_waypoint.sequence` (its
  1-based position in the schema, the only "id" a `MissionItem` has --
  see `app/mission/schema.py`), falling back to `current_sequence`/`None`
  if no current waypoint is tracked yet (e.g. before the first
  `record_progress()` call).
- `frame` is deliberately typed `Any`, not `bytes` or `numpy.ndarray`.
  Nothing earlier in this project has ever taken a dependency on an image
  library (no `opencv-python`/`numpy` in `requirements.txt`), and your
  spec's own ARCHITECTURE line draws the raw source as "PX4 / Gazebo
  Camera" -- a Gazebo camera TOPIC, not a Python object this project
  controls the shape of. Forcing a `numpy.ndarray` type here would be
  guessing at a dependency and a wire format neither installed nor
  specified. `CameraStreamManager` therefore accepts whatever object the
  caller's frame source already produces (raw bytes, a numpy array, a
  PIL Image, a Gazebo message object, ...) and only ever tags/routes it
  -- it never decodes, inspects, or otherwise assumes anything about its
  contents. `_frame_size_bytes()` below is a best-effort size estimate
  for logging/`as_dict()` ONLY, wrapped so an unrecognized frame type
  never breaks tagging.
- `CameraStreamManager.capture_frame()` is the core, fully synchronous,
  fully unit-testable operation your SUCCESS CRITERIA actually asks for:
  given one already-pulled raw frame plus the current `MissionState`/
  `DroneState` (Phase 1/2, read exactly the way `ReplanningContextBuilder`
  already reads them -- see that module), produce a correctly-tagged
  `InspectionFrame` and hand it to every subscriber. `.stream()` is a
  thin async wrapper around it for a CONTINUOUS feed -- it pulls one raw
  frame at a time from an `AsyncIterator` (the shape a real Gazebo-topic
  subscriber would have) and re-reads LIVE `MissionState`/`DroneState` for
  EVERY frame (never a snapshot taken once at the start of the stream),
  the same "always read live state" principle Phase 2's
  `DroneStateManager` established as this project's shared source of
  truth.
- Subscriber dispatch (`subscribe()`/`_notify()`) deliberately mirrors
  `app.trigger.bus.EventBus` (Phase 3) exactly: a list of plain synchronous
  callables, one exception in one subscriber logged and swallowed so it
  never blocks the others. Not reimplemented differently -- this project
  already has one working "fan out to N independent subscribers, don't
  let one break the rest" pattern, so this module reuses its shape rather
  than inventing a second one.
- `InspectionQualityAnalyzer` is the ARCHITECTURE diagram's downstream
  box, but per "DO NOT CONNECT CAMERA EVENTS TO RAMR YET. First verify
  that the camera pipeline works," it is intentionally a MINIMAL
  receiver here -- it records every `InspectionFrame` it's given
  (queryable per-waypoint, for the SUCCESS CRITERIA's "processed ...
  associated with mission waypoints"), logs it, and does NOTHING else.
  It does not detect blur/exposure/coverage gaps, and it never publishes
  to `app.trigger.bus.EventBus`/`TriggerEngine`. This is the same
  "define the shape now, implement the behavior later" pattern Phase 3
  already used for the `IMAGE_BLUR_DETECTED`/`BAD_EXPOSURE_DETECTED`/
  `COVERAGE_GAP_DETECTED` event types that still sit unused in
  `app/trigger/events.py` -- wiring THIS module's output into THOSE
  event types (and therefore into RAMR) is explicitly out of scope until
  a later phase asks for it.

Known limitations (stated plainly, not glossed over):

- This sandbox has no PX4 SITL / Gazebo instance (see the RAMR/CV audit,
  `docs/RAMR_CV_Audit.md`, item 9/G, and every earlier phase's own
  "what was not verified" section) -- there is no real Gazebo camera
  topic this code has ever pulled a frame from. `_demo_cli()` below uses
  a small SYNTHETIC frame source (plain bytes standing in for image
  data) specifically so you can see the tagging/dispatch pipeline run
  end to end without needing Gazebo; it is not, and does not claim to
  be, a real camera integration. Wiring an actual Gazebo camera plugin/
  topic subscriber into the `frame_source: AsyncIterator[Any]` shape
  `CameraStreamManager.stream()` expects is genuinely new work for your
  machine, same as PX4 SITL setup itself was for Phase 7/8.
- No image-processing dependency (`opencv-python`, `numpy`, `Pillow`, ...)
  was added to `requirements.txt` by THIS phase -- Phase 12 (below) is
  the one that actually needed one, and added it then, not speculatively
  here.

==================================================
RAMR/CV novelty Phase 12 -- Basic Computer Vision Quality Analysis.
==================================================

    OBJECTIVE: Implement simple and reliable image quality checks.
    CREATE: InspectionQualityAnalyzer (EXTENDS the Phase 11 class of the
    same name below -- see "reuse, don't rewrite" note).
    START WITH: 1. Blur detection  2. Exposure detection.
    BLUR: use a lightweight method such as OpenCV Laplacian Variance.
    OUTPUT: blur_score and is_blurry.
    EXPOSURE: detect underexposed and overexposed images.
    OUTPUT: exposure_score and exposure_status.
    CREATE: InspectionQualityResult (mission_id, waypoint_id, blur_score,
    exposure_score, quality_status, timestamp).
    SUCCESS CRITERIA: correctly classifies Good / Blurry / Too dark / Too
    bright images.
    TEST USING: normal images, artificially blurred images, darkened
    images, overexposed images.

Design notes:

- "CREATE: InspectionQualityAnalyzer" -- this phase EXTENDS the
  `InspectionQualityAnalyzer` class Phase 11 already created (as the
  ARCHITECTURE diagram's downstream box), rather than creating a second,
  competing class of the same name. Per this project's standing rule
  ("don't rewrite completed phases unless necessary"), every Phase 11
  method (`receive_frame()`, `received_count`, `received_frames()`,
  `frames_for_waypoint()`, `frames_for_mission()`) is kept, unchanged in
  behavior -- Phase 11's own tests (`tests/test_phase_camera_stream.py`)
  still pass unmodified against this extended class. `receive_frame()`
  now ALSO runs the new quality analysis on every frame it receives
  (see below) -- this is the natural, intended integration point (Phase
  11's own docstring already named this class "the ARCHITECTURE
  diagram's downstream box" precisely so Phase 12 could fill it in) --
  but nothing about that wiring touches `app.trigger`/RAMR, matching
  Phase 11's "DO NOT CONNECT CAMERA EVENTS TO RAMR YET" instruction,
  which this phase's own spec does not lift.
- BLUR: `detect_blur()` computes the variance of the Laplacian of the
  frame (converted to grayscale first) via `cv2.Laplacian(gray,
  cv2.CV_64F).var()` -- exactly the "lightweight method such as OpenCV
  Laplacian Variance" your spec names, and a well-known, commonly cited
  technique (see e.g. PyImageSearch's "Blur detection with OpenCV",
  which uses this identical formula). `is_blurry` is `blur_score <
  settings.blur_variance_threshold` (default 100.0 -- see `app/config.py`
  for why this default and its explicit "needs re-tuning per
  camera/scene" caveat, taken directly from that same commonly-cited
  source rather than presented as a universal constant).
- EXPOSURE: `detect_exposure()` computes the MEAN grayscale pixel
  intensity (0-255 scale) as `exposure_score`, and classifies
  `exposure_status` as `UNDEREXPOSED` (mean below
  `settings.underexposed_mean_threshold`, default 60.0), `OVEREXPOSED`
  (mean above `settings.overexposed_mean_threshold`, default 200.0), or
  `NORMAL` otherwise. A simple, honestly-documented heuristic (see
  `app/config.py`) -- not histogram-clipping-based or otherwise more
  sophisticated, matching your own "simple and reliable" framing for
  this phase, not a claim that it's the most robust possible method.
- `InspectionQualityResult` contains EXACTLY your OUTPUT field list --
  `mission_id`, `waypoint_id`, `blur_score`, `exposure_score`,
  `quality_status`, `timestamp` -- no more. `is_blurry` and
  `exposure_status` are real outputs too (your spec names them
  explicitly, just not as `InspectionQualityResult` fields), so they are
  still available from `detect_blur()`/`detect_exposure()`'s own return
  values (`BlurDetectionResult`/`ExposureDetectionResult`) -- Phase 12
  simply doesn't duplicate them onto the final result object your spec's
  own CREATE list didn't ask for.
- `quality_status` (`QualityStatus`: GOOD / BLURRY / TOO_DARK /
  TOO_BRIGHT -- your SUCCESS CRITERIA's exact 4 categories) is derived by
  combining the two checks with a documented, deterministic priority
  when more than one issue is present: `BLURRY` is reported before a
  simultaneous exposure problem, because your OUTPUT section lists blur
  before exposure and a badly out-of-focus frame is the more fundamental
  defect for an inspection photo (a correctly-exposed but unusable blur
  is still unusable) -- documented here as a reasonable, stated
  convention, not something the spec dictates explicitly.
- `frame` decoding: `_to_grayscale()` accepts either an already-decoded
  `numpy.ndarray` (2D grayscale or 3D color, converted via
  `cv2.cvtColor`) or raw ENCODED image bytes (JPEG/PNG/etc., decoded via
  `cv2.imdecode`) -- the two shapes a real frame source plausibly
  produces (see Phase 11's own reasoning for why `InspectionFrame.frame`
  stays typed `Any`). Anything else -- including Phase 11's own
  placeholder byte strings like `b"synthetic-frame-0"`, which are not a
  real encoded image -- raises `FrameDecodeError` rather than guessing a
  score from garbage input. `receive_frame()` catches exactly this
  error, logs it, and stores the frame with `result=None` rather than
  losing the frame or crashing the whole pipeline -- Phase 11's demo and
  tests (which use non-image placeholder bytes) therefore keep working
  unchanged; only a genuinely decodable frame gets a real
  `InspectionQualityResult`.
- New dependency: `opencv-python-headless` and `numpy` are added to
  `requirements.txt` by THIS phase (Phase 11 deliberately did not add
  either -- see that phase's own "Known limitations"). `-headless`
  specifically, not plain `opencv-python`, since this backend never
  displays a GUI window and the headless build avoids an unnecessary Qt/
  GUI dependency on a server/CI environment -- the same class of choice
  this project already made for `mavsdk`/`chromadb` (server-appropriate
  packages, not desktop ones).

Known limitations (Phase 12, stated plainly):

- The three thresholds (`blur_variance_threshold`,
  `underexposed_mean_threshold`, `overexposed_mean_threshold`) are
  documented heuristic defaults (see `app/config.py`), not validated
  against a real inspection-camera dataset or your actual Gazebo/PX4
  camera's sensor characteristics -- expect to retune them once real
  frames are available (same "documented assumption, overridable in
  .env" pattern this project already uses for
  `mission_default_speed_ms`/`max_safe_speed_ms`/battery-model
  constants).
- Exposure detection here is a single global-mean heuristic. It will
  misclassify a frame that is well-exposed overall but has a small
  blown-out or crushed region (e.g. sky in one corner) -- a
  histogram/percentile-based method would be more robust but is
  explicitly out of scope for "simple and reliable... start with."
- `InspectionQualityAnalyzer` still does not publish anything to
  `app.trigger.bus.EventBus`/`TriggerEngine` -- `IMAGE_BLUR_DETECTED`/
  `BAD_EXPOSURE_DETECTED` remain unused event types (Phase 3), exactly
  as Phase 11 left them; wiring quality results into RAMR is still a
  later phase's scope, since neither this phase's nor Phase 11's spec
  asked for it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Callable, Iterable

from app.config import settings
from app.mavlink.drone_state import DroneState
from app.mission.state import MissionState, MissionStateManager
from app.replanning.context_builder import DronePosition

log = logging.getLogger(__name__)

FrameConsumer = Callable[["InspectionFrame"], None]


def _frame_size_bytes(frame: Any) -> int | None:
    """Best-effort size estimate for logging/as_dict() only -- never raises, never assumes a type."""
    try:
        if isinstance(frame, (bytes, bytearray, memoryview)):
            return len(frame)
        nbytes = getattr(frame, "nbytes", None)  # numpy.ndarray and similar
        if isinstance(nbytes, int):
            return nbytes
    except Exception:  # noqa: BLE001 -- purely best-effort, must never break tagging
        pass
    return None


@dataclass
class InspectionFrame:
    """The Phase 11 OUTPUT, verbatim: frame, mission_id, waypoint_id, drone_position, timestamp."""

    frame: Any
    mission_id: str
    waypoint_id: int | None
    drone_position: DronePosition
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        """Flat, JSON-serializable summary -- deliberately excludes the raw `frame` payload itself (arbitrary size/type; not JSON-safe in general)."""
        return {
            "mission_id": self.mission_id,
            "waypoint_id": self.waypoint_id,
            "drone_position": self.drone_position.as_dict(),
            "timestamp": self.timestamp.isoformat(),
            "frame_type": type(self.frame).__name__,
            "frame_size_bytes": _frame_size_bytes(self.frame),
        }


async def iter_frames(frames: Iterable[Any]) -> AsyncIterator[Any]:
    """
    Wraps a plain (synchronous) iterable of raw frames into the
    `AsyncIterator[Any]` shape `CameraStreamManager.stream()` expects --
    a real Gazebo-topic subscriber would naturally BE an async iterator
    (or be wrapped into one); this helper is for tests/`_demo_cli()`/any
    caller that already has frames in hand (e.g. read from disk) rather
    than a live stream.
    """
    for frame in frames:
        yield frame


class CameraStreamManager:
    """
    Usage (one already-pulled frame):
        manager = CameraStreamManager()
        manager.subscribe(analyzer.receive_frame)
        inspection_frame = manager.capture_frame(raw_frame, mission_state, drone_state)

    Usage (a continuous stream):
        produced = await manager.stream(
            frame_source,  # an AsyncIterator[Any] -- see iter_frames() for wrapping a plain list
            mission_state_manager=mission_state_manager,
            drone_state_provider=lambda: drone_state_manager.snapshot(),
        )
    """

    def __init__(self) -> None:
        self._subscribers: list[FrameConsumer] = []
        self._frame_count = 0

    def subscribe(self, consumer: FrameConsumer) -> None:
        self._subscribers.append(consumer)

    @property
    def frame_count(self) -> int:
        """Total frames captured (and dispatched) so far, across every call to capture_frame()/stream()."""
        return self._frame_count

    def capture_frame(
        self,
        raw_frame: Any,
        mission_state: MissionState,
        drone_state: DroneState,
    ) -> InspectionFrame:
        """
        Tags ONE already-pulled raw frame with the CURRENT mission ID,
        current waypoint, drone position, and a fresh timestamp, then
        notifies every subscriber (e.g. `InspectionQualityAnalyzer
        .receive_frame`). Never raises for a subscriber's own error --
        see this module's docstring (same resilience as `EventBus`).
        """
        current_waypoint = mission_state.current_waypoint
        waypoint_id = current_waypoint.sequence if current_waypoint is not None else (
            mission_state.current_sequence or None
        )
        position = DronePosition(
            latitude_deg=drone_state.latitude_deg,
            longitude_deg=drone_state.longitude_deg,
            altitude_m=drone_state.relative_altitude_m,
        )
        inspection_frame = InspectionFrame(
            frame=raw_frame,
            mission_id=mission_state.mission_id,
            waypoint_id=waypoint_id,
            drone_position=position,
        )
        self._frame_count += 1
        log.info(
            "Captured frame #%d for mission %s, waypoint %s, position=%s",
            self._frame_count, inspection_frame.mission_id, inspection_frame.waypoint_id,
            position.as_dict(),
        )
        self._notify(inspection_frame)
        return inspection_frame

    def _notify(self, frame: InspectionFrame) -> None:
        for consumer in self._subscribers:
            try:
                consumer(frame)
            except Exception:
                log.exception(
                    "Subscriber %r raised while handling frame for mission %s waypoint %s -- "
                    "other subscribers still ran.",
                    consumer, frame.mission_id, frame.waypoint_id,
                )

    async def stream(
        self,
        frame_source: AsyncIterator[Any],
        *,
        mission_state_manager: MissionStateManager,
        drone_state_provider: Callable[[], DroneState],
        max_frames: int | None = None,
    ) -> list[InspectionFrame]:
        """
        Continuously pulls raw frames from `frame_source` and tags +
        dispatches each one via `capture_frame()`, re-reading LIVE
        `MissionState`/`DroneState` for EVERY frame (see this module's
        docstring). Returns every `InspectionFrame` produced, mainly for
        tests/`_demo_cli()`. Stops after `max_frames` frames if given;
        otherwise runs until `frame_source` is exhausted.
        """
        produced: list[InspectionFrame] = []
        async for raw_frame in frame_source:
            mission_state = mission_state_manager.snapshot()
            drone_state = drone_state_provider()
            produced.append(self.capture_frame(raw_frame, mission_state, drone_state))
            if max_frames is not None and len(produced) >= max_frames:
                break
        return produced


class FrameDecodeError(Exception):
    """
    Raised when a raw frame cannot be decoded into a real image for
    quality analysis (unsupported type, or bytes that don't decode as a
    recognized image format). Deliberately an exception, not a
    documented-`None` result -- see this module's Phase 12 docstring for
    why this is treated as a genuine error rather than a normal outcome.
    """


class ExposureStatus(str, Enum):
    UNDEREXPOSED = "UNDEREXPOSED"
    NORMAL = "NORMAL"
    OVEREXPOSED = "OVEREXPOSED"


class QualityStatus(str, Enum):
    """Your SUCCESS CRITERIA's exact 4 categories."""

    GOOD = "GOOD"
    BLURRY = "BLURRY"
    TOO_DARK = "TOO_DARK"
    TOO_BRIGHT = "TOO_BRIGHT"


@dataclass
class BlurDetectionResult:
    """BLUR's OUTPUT, verbatim: blur_score and is_blurry."""

    blur_score: float
    is_blurry: bool


@dataclass
class ExposureDetectionResult:
    """EXPOSURE's OUTPUT, verbatim: exposure_score and exposure_status."""

    exposure_score: float
    exposure_status: ExposureStatus


@dataclass
class InspectionQualityResult:
    """The Phase 12 OUTPUT, verbatim: mission_id, waypoint_id, blur_score, exposure_score, quality_status, timestamp."""

    mission_id: str
    waypoint_id: int | None
    blur_score: float
    exposure_score: float
    quality_status: QualityStatus
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        return {
            "mission_id": self.mission_id,
            "waypoint_id": self.waypoint_id,
            "blur_score": self.blur_score,
            "exposure_score": self.exposure_score,
            "quality_status": self.quality_status.value,
            "timestamp": self.timestamp.isoformat(),
        }


def _lazy_import_cv_deps():
    """
    Lazy import -- same reasoning as every other optional/heavy
    dependency in this project (`mavsdk`, `ollama`, `groq`): importing
    this module for its types shouldn't require `opencv-python-headless`/
    `numpy` to be installed, only actually calling the analysis methods
    should.
    """
    import cv2
    import numpy as np

    return cv2, np


def _to_grayscale(frame: Any):
    """See this module's Phase 12 docstring for the two shapes handled and why anything else raises FrameDecodeError."""
    cv2, np = _lazy_import_cv_deps()

    if isinstance(frame, np.ndarray):
        if frame.ndim == 2:
            return frame
        if frame.ndim == 3:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        raise FrameDecodeError(f"Unsupported numpy array shape for image quality analysis: {frame.shape}")

    if isinstance(frame, (bytes, bytearray, memoryview)):
        buffer = np.frombuffer(bytes(frame), dtype=np.uint8)
        decoded = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE) if buffer.size else None
        if decoded is None:
            raise FrameDecodeError(
                "Frame bytes could not be decoded as a recognized image format (e.g. JPEG/PNG) -- "
                "cannot compute blur/exposure scores from non-image data."
            )
        return decoded

    raise FrameDecodeError(f"Don't know how to decode a frame of type {type(frame).__name__} for image quality analysis.")


@dataclass
class _ReceivedFrameRecord:
    frame: InspectionFrame
    result: InspectionQualityResult | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class InspectionQualityAnalyzer:
    """
    The ARCHITECTURE diagram's downstream box. Phase 11 built this as a
    minimal receiver; Phase 12 (see this module's docstring) extends it
    with real blur/exposure quality analysis, run automatically on every
    frame `receive_frame()` gets -- still without publishing anything to
    RAMR/the Trigger Engine.

    Usage:
        analyzer = InspectionQualityAnalyzer()
        camera_manager.subscribe(analyzer.receive_frame)
        ...
        result = analyzer.results_for_waypoint(3)[-1]
        if result.quality_status != QualityStatus.GOOD:
            ...  # a future phase decides what, if anything, to do about it
    """

    def __init__(
        self,
        *,
        blur_variance_threshold: float | None = None,
        underexposed_mean_threshold: float | None = None,
        overexposed_mean_threshold: float | None = None,
    ) -> None:
        self._received: list[_ReceivedFrameRecord] = []
        self.blur_variance_threshold = (
            blur_variance_threshold if blur_variance_threshold is not None else settings.blur_variance_threshold
        )
        self.underexposed_mean_threshold = (
            underexposed_mean_threshold
            if underexposed_mean_threshold is not None
            else settings.underexposed_mean_threshold
        )
        self.overexposed_mean_threshold = (
            overexposed_mean_threshold
            if overexposed_mean_threshold is not None
            else settings.overexposed_mean_threshold
        )

    # -----------------------------------------------------------------
    # Blur detection
    # -----------------------------------------------------------------

    def detect_blur(self, frame: Any) -> BlurDetectionResult:
        """
        OpenCV Laplacian Variance -- see this module's Phase 12 docstring
        for the citation. Lower variance = smoother/less detail = more
        likely blurry.
        """
        cv2, _np = _lazy_import_cv_deps()
        gray = _to_grayscale(frame)
        blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        return BlurDetectionResult(blur_score=blur_score, is_blurry=blur_score < self.blur_variance_threshold)

    # -----------------------------------------------------------------
    # Exposure detection
    # -----------------------------------------------------------------

    def detect_exposure(self, frame: Any) -> ExposureDetectionResult:
        """Mean grayscale pixel intensity (0-255) classified against the configured under/over-exposure thresholds."""
        gray = _to_grayscale(frame)
        exposure_score = float(gray.mean())
        if exposure_score < self.underexposed_mean_threshold:
            status = ExposureStatus.UNDEREXPOSED
        elif exposure_score > self.overexposed_mean_threshold:
            status = ExposureStatus.OVEREXPOSED
        else:
            status = ExposureStatus.NORMAL
        return ExposureDetectionResult(exposure_score=exposure_score, exposure_status=status)

    # -----------------------------------------------------------------
    # Combined analysis -> InspectionQualityResult
    # -----------------------------------------------------------------

    def analyze(self, frame: InspectionFrame) -> InspectionQualityResult:
        """
        Runs both checks against `frame.frame` and combines them into an
        `InspectionQualityResult` tagged with the SAME mission_id/
        waypoint_id/timestamp as the `InspectionFrame` it came from.
        Raises `FrameDecodeError` if `frame.frame` isn't decodable as an
        image -- see this module's docstring for why `receive_frame()`
        (not this method) is what catches that.
        """
        blur = self.detect_blur(frame.frame)
        exposure = self.detect_exposure(frame.frame)

        if blur.is_blurry:
            quality_status = QualityStatus.BLURRY
        elif exposure.exposure_status == ExposureStatus.UNDEREXPOSED:
            quality_status = QualityStatus.TOO_DARK
        elif exposure.exposure_status == ExposureStatus.OVEREXPOSED:
            quality_status = QualityStatus.TOO_BRIGHT
        else:
            quality_status = QualityStatus.GOOD

        return InspectionQualityResult(
            mission_id=frame.mission_id,
            waypoint_id=frame.waypoint_id,
            blur_score=blur.blur_score,
            exposure_score=exposure.exposure_score,
            quality_status=quality_status,
            timestamp=frame.timestamp,
        )

    # -----------------------------------------------------------------
    # Receiving frames (Phase 11, extended)
    # -----------------------------------------------------------------

    def receive_frame(self, frame: InspectionFrame) -> None:
        log.info(
            "InspectionQualityAnalyzer received frame for mission %s, waypoint %s, at %s",
            frame.mission_id, frame.waypoint_id, frame.timestamp.isoformat(),
        )
        try:
            result = self.analyze(frame)
            log.info(
                "Quality analysis for mission %s, waypoint %s: %s (blur_score=%.2f, exposure_score=%.2f)",
                frame.mission_id, frame.waypoint_id, result.quality_status.value,
                result.blur_score, result.exposure_score,
            )
        except FrameDecodeError as exc:
            log.warning(
                "Could not analyze frame for mission %s, waypoint %s (%s) -- frame is still recorded, "
                "just without a quality result.",
                frame.mission_id, frame.waypoint_id, exc,
            )
            result = None
        self._received.append(_ReceivedFrameRecord(frame=frame, result=result))

    @property
    def received_count(self) -> int:
        return len(self._received)

    def received_frames(self) -> list[InspectionFrame]:
        """Every frame received so far, oldest first."""
        return [record.frame for record in self._received]

    def frames_for_waypoint(self, waypoint_id: int) -> list[InspectionFrame]:
        """Every frame received so far tagged with this waypoint_id, oldest first."""
        return [record.frame for record in self._received if record.frame.waypoint_id == waypoint_id]

    def frames_for_mission(self, mission_id: str) -> list[InspectionFrame]:
        """Every frame received so far tagged with this mission_id, oldest first."""
        return [record.frame for record in self._received if record.frame.mission_id == mission_id]

    def results(self) -> list[InspectionQualityResult]:
        """Every InspectionQualityResult successfully produced so far, oldest first (skips any FrameDecodeError frames)."""
        return [record.result for record in self._received if record.result is not None]

    def results_for_waypoint(self, waypoint_id: int) -> list[InspectionQualityResult]:
        """Every InspectionQualityResult tagged with this waypoint_id, oldest first."""
        return [
            record.result for record in self._received
            if record.result is not None and record.result.waypoint_id == waypoint_id
        ]

    def results_for_mission(self, mission_id: str) -> list[InspectionQualityResult]:
        """Every InspectionQualityResult tagged with this mission_id, oldest first."""
        return [
            record.result for record in self._received
            if record.result is not None and record.result.mission_id == mission_id
        ]


def _make_demo_images():
    """
    Builds the 4 synthetic test images your spec's own TEST USING list
    asks for (normal, artificially blurred, darkened, overexposed),
    encoded as real JPEG bytes -- NOT a real Gazebo camera frame (see
    this module's "Known limitations"), but a real, decodable image, so
    `detect_blur()`/`detect_exposure()` run their actual OpenCV logic
    here, not just the FrameDecodeError fallback path.
    """
    cv2, np = _lazy_import_cv_deps()

    rng = np.random.default_rng(seed=42)
    # A normal, well-exposed image needs real texture/edges for the
    # Laplacian-variance blur measure to be meaningful -- a flat/blank
    # image is "blurry" by that measure regardless of focus. Random noise
    # at mid-gray stands in for a textured inspection subject (a wall,
    # foliage, etc.) without needing a real photo.
    normal = rng.integers(90, 165, size=(200, 200), dtype=np.uint8)

    blurred = cv2.GaussianBlur(normal, (25, 25), sigmaX=10)
    darkened = (normal.astype(np.float64) * 0.25).astype(np.uint8)
    # Shift bright AND compress the texture's amplitude -- not just add a
    # flat offset -- so the result is overexposed without clipping so
    # many pixels to flat 255 that it also (unrealistically) destroys the
    # Laplacian-variance blur measure.
    overexposed = np.clip(normal.astype(np.float64) * 0.3 + 200, 0, 255).astype(np.uint8)

    def _encode(gray_array) -> bytes:
        ok, buf = cv2.imencode(".jpg", gray_array)
        assert ok
        return buf.tobytes()

    return {
        "normal": _encode(normal),
        "blurred": _encode(blurred),
        "darkened": _encode(darkened),
        "overexposed": _encode(overexposed),
    }


def _demo_cli() -> None:
    """
    Manual test entry point matching both phases' SUCCESS CRITERIA:
    Phase 11's (frames received/processed, associated with mission
    waypoints) and Phase 12's (correctly classifies Good / Blurry / Too
    dark / Too bright). Uses SYNTHETIC images -- real JPEG-encoded
    frames, but NOT a real Gazebo camera (see this module's "Known
    limitations"):
        python -m app.vision.camera_stream
    No PX4/Gazebo/network needed -- everything here is synthetic/offline.
    """
    import asyncio
    import json

    from app.mission.schema import Mission, MissionCommand, MissionItem

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    mission = Mission(
        mission_name="Demo inspection survey",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.000, longitude=2.000, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.010, longitude=2.000, altitude=30),
            MissionItem(sequence=4, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )
    manager = MissionStateManager()
    manager.start(mission)
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=2, total=4)  # drone at WP1 (item #2)

    drone_state = DroneState(latitude_deg=1.000, longitude_deg=2.000, relative_altitude_m=30.0)

    camera_manager = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    camera_manager.subscribe(analyzer.receive_frame)

    demo_images = _make_demo_images()
    labels = list(demo_images.keys())  # normal, blurred, darkened, overexposed

    async def _run():
        return await camera_manager.stream(
            iter_frames(demo_images.values()),
            mission_state_manager=manager,
            drone_state_provider=lambda: drone_state,
        )

    produced = asyncio.run(_run())

    print(f"Captured {len(produced)} frame(s).\n")
    for label, result in zip(labels, analyzer.results()):
        print(f"{label:12s} -> {json.dumps(result.as_dict(), indent=2)}")

    print(f"\nInspectionQualityAnalyzer received {analyzer.received_count} frame(s), "
          f"produced {len(analyzer.results())} quality result(s).")


if __name__ == "__main__":
    _demo_cli()
