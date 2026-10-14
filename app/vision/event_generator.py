"""
RAMR/CV novelty Phase 13 -- CV Event Generation.

    OBJECTIVE: Convert CV quality failures into standard replanning
    events.
    ARCHITECTURE: Camera Frame -> CV Analyzer -> Inspection Quality
    Result -> CV Event Generator -> ReplanningEvent -> TriggerEngine.
    EVENTS: IMAGE_BLUR_DETECTED, BAD_EXPOSURE_DETECTED,
    INSPECTION_QUALITY_FAILURE.
    Example: ReplanningEvent(event_type=IMAGE_BLUR_DETECTED,
    source=COMPUTER_VISION, severity=MEDIUM, waypoint_id=WP8,
    metadata={blur_score, camera_information, image_quality_information}).
    SUCCESS CRITERIA: A bad image produces a valid event that reaches
    the TriggerEngine.

Design notes:

- `CVEventGenerator` implements exactly the ARCHITECTURE diagram's last
  two boxes: `InspectionQualityResult -> CV Event Generator ->
  ReplanningEvent`. It is a pure, standalone converter -- it does NOT
  itself hold a reference to an `EventBus`/`TriggerEngine` and does not
  publish anything; that decoupling is the SAME pattern this project
  already established in Phase 3 (`TriggerEngine`/`EventBus` are built
  precisely so any publisher -- telemetry, an operator UI, and now CV --
  can hand them a `ReplanningEvent` without the publisher needing to
  know who's listening). Reusing that existing decoupled design, rather
  than having `CVEventGenerator` reach into `app.trigger.bus`/`engine`
  itself, is what makes "a bad image produces a valid event that reaches
  the TriggerEngine" (your SUCCESS CRITERIA) provable as a straight-line
  integration test: generate the event, then hand it to
  `TriggerEngine.handle_event()` (or `EventBus.publish()`), exactly the
  way `app/trigger/engine.py`'s own `_demo_cli()` already does for a
  telemetry event.
- Mapping from `QualityStatus` (Phase 12) to `EventType`:
    * `GOOD` -> no event (`generate_event()` returns `None`) -- a good
      image is not a replanning trigger.
    * `BLURRY` -> `IMAGE_BLUR_DETECTED`.
    * `TOO_DARK` / `TOO_BRIGHT` -> `BAD_EXPOSURE_DETECTED` (both are
      exposure problems; your EVENTS list has one exposure event type,
      not two, so both exposure failure modes map to it -- the specific
      direction is still visible in `metadata["image_quality_information"]
      ["quality_status"]` and `metadata["exposure_score"]`).
    * anything else not recognized above -> `INSPECTION_QUALITY_FAILURE`
      -- the generic fallback your EVENTS list's third entry is for.
      Nothing in this project can currently produce a `QualityStatus`
      outside the 4 values Phase 12 defined, so this branch is
      unreachable today; it exists so a FUTURE quality check (e.g. a
      real coverage-gap detector) can start emitting
      `INSPECTION_QUALITY_FAILURE` without `CVEventGenerator` needing a
      code change, matching this project's own "define the shape now"
      pattern (same reasoning Phase 3 used for defining
      `IMAGE_BLUR_DETECTED` etc. before any CV code existed to publish
      them).
  Your own SUCCESS CRITERIA says "A bad image produces A valid event"
  (singular) -- so `generate_event()` returns at most ONE event per
  `InspectionQualityResult`, not one-per-check, matching that wording
  rather than emitting two events (e.g. both IMAGE_BLUR_DETECTED and
  INSPECTION_QUALITY_FAILURE) for the same bad frame.
- `waypoint_id`: this phase adds `waypoint_id: int | None = None` to
  `ReplanningEvent` itself (`app/trigger/events.py`) -- an additive,
  default-`None` field, so every existing caller that constructs a
  `ReplanningEvent` keeps working unchanged (verified: the full test
  suite still passes). "WP8" in your example is this project's existing
  `MissionItem.sequence`/`InspectionFrame.waypoint_id`/
  `InspectionQualityResult.waypoint_id` convention (an int), reused
  as-is rather than inventing a new string waypoint-ID scheme.
- `metadata` contains exactly the keys your example names --
  `blur_score`, `camera_information`, `image_quality_information` --
  plus `exposure_score` alongside `blur_score` (both scores are always
  available on `InspectionQualityResult` regardless of which one
  triggered the event, and including both makes an exposure-triggered
  event's metadata just as self-describing as a blur-triggered one's,
  without contradicting your example, which lists `blur_score` as ONE of
  several metadata entries, not the only one). `image_quality_information`
  is the full `InspectionQualityResult.as_dict()` -- the complete
  underlying quality detail, so nothing about the frame's quality result
  is lost even though only 2 of the 3 event types make it to
  `event_type` itself. `camera_information` is this module's own
  reasonable, documented shape (your spec names the KEY but not its
  contents) -- capture timestamp and `waypoint_id` always, plus the
  `InspectionFrame`'s `drone_position` when the caller supplies the
  originating `InspectionFrame` (optional -- see `generate_event()`'s
  `inspection_frame` parameter; the ARCHITECTURE diagram's primary input
  is `InspectionQualityResult` alone, so this is an enrichment, not a
  requirement).
- `severity`: `MEDIUM` for every CV-generated event by default, matching
  your own example and `TriggerEngine`'s existing (Phase 3)
  `DEFAULT_ACTIVATION_THRESHOLDS` for these exact event types (also
  MEDIUM) -- so a CV event this generator produces activates replanning
  under the Trigger Engine's OUT-OF-THE-BOX thresholds with no
  additional configuration needed. Overridable per `CVEventGenerator`
  instance, same pattern as every other configurable default in this
  project.
- `source`: always `EventSource.COMPUTER_VISION` -- reused unchanged
  (Phase 3 already defined this exact value for exactly this purpose).

Known limitations (stated plainly):

- `camera_information`'s shape is this module's own choice, not
  specified by your spec beyond its name -- see above.
- Severity is currently a single fixed value per event type (not scaled
  by how bad the blur/exposure score is, e.g. barely-underexposed vs.
  pitch black) -- a reasonable simplification for "convert CV failures
  into standard replanning events," not a claim that severity grading is
  out of scope forever.
- This phase does NOT wire `CVEventGenerator`'s output automatically
  into `app.replanning.orchestrator.RAMROrchestrator` (Phase 10) -- it
  only gets the event to the `TriggerEngine`/`EventBus`, exactly as far
  as this phase's own ARCHITECTURE diagram and SUCCESS CRITERIA go.
  `InspectionQualityAnalyzer`'s new optional `on_event` hook (see
  `app/vision/camera_stream.py`) can be pointed at
  `orchestrator.handle_event` (or anything else) by a future phase/the
  caller's own wiring, without this module changing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent
from app.vision.camera_stream import InspectionFrame, QualityStatus

if TYPE_CHECKING:
    from app.vision.camera_stream import InspectionQualityResult

log = logging.getLogger(__name__)

# QualityStatus values that map to a SPECIFIC event type. Anything not
# in this table (there is currently nothing -- see this module's
# docstring) falls back to INSPECTION_QUALITY_FAILURE.
_QUALITY_STATUS_TO_EVENT_TYPE: dict[QualityStatus, EventType] = {
    QualityStatus.BLURRY: EventType.IMAGE_BLUR_DETECTED,
    QualityStatus.TOO_DARK: EventType.BAD_EXPOSURE_DETECTED,
    QualityStatus.TOO_BRIGHT: EventType.BAD_EXPOSURE_DETECTED,
}


class CVEventGenerator:
    """
    Usage:
        generator = CVEventGenerator()
        event = generator.generate_event(quality_result)  # None if quality_status is GOOD
        if event is not None:
            trigger_engine.handle_event(event)  # or bus.publish(event)
    """

    def __init__(self, *, severity: EventSeverity = EventSeverity.MEDIUM) -> None:
        self.severity = severity

    def generate_event(
        self,
        result: "InspectionQualityResult",
        *,
        inspection_frame: InspectionFrame | None = None,
    ) -> ReplanningEvent | None:
        """
        Converts one `InspectionQualityResult` (Phase 12's output) into
        at most one `ReplanningEvent`. Returns `None` for `GOOD` --
        nothing to report, no event generated. `inspection_frame` is
        optional and, when given, enriches `metadata["camera_information"]`
        with the drone's position at capture time -- see this module's
        docstring.
        """
        if result.quality_status == QualityStatus.GOOD:
            return None

        event_type = _QUALITY_STATUS_TO_EVENT_TYPE.get(result.quality_status, EventType.INSPECTION_QUALITY_FAILURE)

        camera_information: dict = {
            "timestamp": result.timestamp.isoformat(),
            "waypoint_id": result.waypoint_id,
        }
        if inspection_frame is not None:
            camera_information["drone_position"] = inspection_frame.drone_position.as_dict()

        event = ReplanningEvent(
            event_type=event_type,
            severity=self.severity,
            source=EventSource.COMPUTER_VISION,
            mission_id=result.mission_id,
            waypoint_id=result.waypoint_id,
            metadata={
                "blur_score": result.blur_score,
                "exposure_score": result.exposure_score,
                "camera_information": camera_information,
                "image_quality_information": result.as_dict(),
            },
        )
        log.info(
            "CVEventGenerator produced %s (severity=%s) for mission %s, waypoint %s -- quality_status=%s",
            event_type.value, self.severity.value, result.mission_id, result.waypoint_id,
            getattr(result.quality_status, "value", result.quality_status),
        )
        return event


def _demo_cli() -> None:
    """
    Manual test entry point matching your spec's own SUCCESS CRITERIA:
    a bad image produces a valid event that reaches the TriggerEngine.
        python -m app.vision.event_generator
    No PX4/Gazebo/network needed -- everything here is synthetic/offline
    (reuses the same synthetic-image approach as Phase 12's `_demo_cli()`).
    """
    import json

    from app.mission.schema import Mission, MissionCommand, MissionItem
    from app.mission.state import MissionStateManager
    from app.trigger.engine import TriggerEngine
    from app.vision.camera_stream import CameraStreamManager, InspectionQualityAnalyzer, _make_demo_images

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
    manager.record_progress(current=3, total=4)  # drone at "WP8"-equivalent, item #3

    from app.mavlink.drone_state import DroneState

    drone_state = DroneState(latitude_deg=1.010, longitude_deg=2.000, relative_altitude_m=30.0)

    camera_manager = CameraStreamManager()
    analyzer = InspectionQualityAnalyzer()
    camera_manager.subscribe(analyzer.receive_frame)

    blurred_frame = camera_manager.capture_frame(_make_demo_images()["blurred"], manager.snapshot(), drone_state)

    quality_result = analyzer.results()[-1]
    print("InspectionQualityResult:")
    print(json.dumps(quality_result.as_dict(), indent=2))

    generator = CVEventGenerator()
    event = generator.generate_event(quality_result, inspection_frame=blurred_frame)
    print("\nGenerated ReplanningEvent:")
    print(json.dumps(event.as_dict(), indent=2))

    trigger_engine = TriggerEngine()
    decision = trigger_engine.handle_event(event)
    print(f"\nTriggerEngine decision: should_replan={decision.should_replan}")
    print(f"reason: {decision.reason}")
    if decision.request:
        print("\nAdaptiveReplanningRequest produced -- the event reached the TriggerEngine and activated replanning:")
        print(json.dumps(decision.request.as_dict(), indent=2))


if __name__ == "__main__":
    _demo_cli()
