"""
Real-world coordinate & building resolution improvement -- the new
integration point ("Pass the resolved location into the EXISTING
mission-planning pipeline").

    New flow:
    User Request -> NLP -> Location Resolver -> Resolved Coordinates/
    Building Geometry -> Existing RAG -> ... -> Existing AI Mission
    Planner -> Existing Waypoint Generator -> Existing Safety Validation
    -> MAVSDK -> PX4

This module is the ONLY new orchestration code this improvement adds --
everything it calls is either brand new (`app.mission.intent_parser`,
`app.location.*`) or a completely UNMODIFIED existing component
(`app.mission.generator.generate_validated_mission()`, which itself
still calls the unmodified RAG retriever, LLM client, and Phase 5 rule
validator). `app.mission.generator.py` itself was touched only to add
one optional, default-`None` keyword argument (`location_context`) --
see that module's docstring.

    generate_mission_from_natural_language("Inspect AB1 of VIT Chennai.")

runs, in order:
  1. `parse_mission_intent()` (new) -- mission_type/institution/campus/
     building/altitude extraction, via the SAME LLM architecture Phase 3
     already uses.
  2. If no real-world location was mentioned at all: skip straight to
     step 4 with the request completely unchanged -- this is the
     guarantee that every mission this project could already generate
     before this improvement existed still works EXACTLY as before.
  3. If a location WAS mentioned: `LocationResolver.resolve()` (new).
     - `AMBIGUOUS` or `LOCATION_NOT_RESOLVED` -> STOP here. No mission is
       generated. This is not a bug/gap -- it's spec section 12's
       explicit requirement ("Do not generate a mission when the
       requested building cannot be reliably resolved").
     - `RESOLVED` -> continue.
  4. `generate_validated_mission()` (UNMODIFIED existing Phase 3-5
     pipeline; `RAG -> LLM -> schema validation -> rule validation`),
     with the resolved location (if any) passed in as additional
     `location_context`.
  5. If a location was resolved AND the mission was rule-valid: run the
     new `validate_mission_against_location()` check (spec section 10)
     as one MORE, ADDITIONAL safety gate -- never a replacement for
     Phase 5's `validate_mission()`, which already ran, unmodified, in
     step 4.

Nothing here ever talks to MAVSDK/PX4 directly (same standing project
rule every other phase has followed) -- the returned
`MissionGenerationResult.mission` is handed to the SAME downstream
components (`app.mission.converter`, `app.mavlink.px4_client`/
`mission_update_manager`) every other mission this project has ever
generated already goes through, completely unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from app.config import settings
from app.location.building_knowledge import BuildingFacts, lookup_building_facts, render_building_facts
from app.location.geofence import LocationValidationResult
from app.location.inspection_geometry import InspectionRing, compute_inspection_ring, footprint_extent, render_inspection_ring
from app.location.location_safety import safety_constraints_for_location, validate_location_mission_safety
from app.location.location_resolver import LocationResolver
from app.location.models import LocationConfidence, LocationResolutionResult, LocationResolutionStatus, ResolvedLocation
from app.mission.generator import MAX_VALIDATION_ATTEMPTS, MissionGenerationResult, generate_validated_mission
from app.mission.validator import validate_mission
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.inspection_camera import orient_camera
from app.mission.intent_parser import MissionIntent, ScanMode, parse_mission_intent
from app.mission.structural_scan import StructuralScanError, plan_structural_scan
from app.mission.road_scan import RoadScanError, RoadWay, plan_road_scan, road_name_from_address
from app.location.geocoding_service import GeocodingError
from app.location.road_network import fetch_named_roads

log = logging.getLogger(__name__)


class NaturalLanguageMissionStatus(str, Enum):
    """What `generate_mission_from_natural_language()`'s result always carries as its top-level outcome."""

    GENERATED = "GENERATED"                    # a location was resolved (or none was needed) and a mission was generated
    LOCATION_AMBIGUOUS = "LOCATION_AMBIGUOUS"    # spec CASE 2 -- STOPPED, no mission generated
    LOCATION_NOT_RESOLVED = "LOCATION_NOT_RESOLVED"  # spec CASE 1/3 -- STOPPED, no mission generated
    LOCATION_SAFETY_REJECTED = "LOCATION_SAFETY_REJECTED"  # mission generated but failed the new location-specific check
    LOCATION_LOW_CONFIDENCE = "LOCATION_LOW_CONFIDENCE"  # remaining Phase A -- resolved, but below settings.location_min_confidence -- STOPPED
    SCAN_NOT_POSSIBLE = "SCAN_NOT_POSSIBLE"  # structural scan asked for, but no single building / outline to scan -- STOPPED


_CONFIDENCE_RANK = {
    LocationConfidence.UNRESOLVED: 0,
    LocationConfidence.LOW: 1,
    LocationConfidence.MEDIUM: 2,
    LocationConfidence.HIGH: 3,
}


def meets_min_confidence(confidence: LocationConfidence, minimum: str | None = None) -> bool:
    """Remaining Phase A -- True if `confidence` is at least `minimum` (default: settings.location_min_confidence)."""
    required = LocationConfidence((minimum or settings.location_min_confidence).upper())
    return _CONFIDENCE_RANK[confidence] >= _CONFIDENCE_RANK[required]


def _render_location_context(location: ResolvedLocation) -> str:
    """
    Renders a `ResolvedLocation` into the short text block
    `generate_validated_mission(location_context=...)` appends to the RAG
    context (see `app/mission/generator.py`). Deliberately plain text,
    matching this project's existing prompt-building style (see
    `app/llm/prompts.py`'s `build_replanning_user_prompt()`) rather than
    raw JSON, since the LLM handles prose more reliably than a nested
    structure it has to parse itself.
    """
    lines = [
        f"Mission type / location: institution={location.institution or '(n/a)'}, "
        f"campus={location.campus or '(n/a)'}, building={location.building or '(n/a)'}",
        f"Latitude: {location.latitude}",
        f"Longitude: {location.longitude}",
        f"Resolution type: {location.resolution_type.value} (source: {location.source.value}, confidence: {location.confidence.value})",
    ]
    if location.geometry_available:
        lines.append(f"Building footprint: {len(location.geometry)}-point polygon available (fly around its perimeter, not just to its center point).")
    else:
        lines.append("Building footprint: not available -- treat the coordinates above as a single point.")
    return "\n".join(lines)


@dataclass
class NaturalLanguageMissionResult:
    """What `generate_mission_from_natural_language()` always returns."""

    status: NaturalLanguageMissionStatus
    intent: MissionIntent
    location_result: LocationResolutionResult | None  # None only when the request had no location to resolve at all
    mission_result: MissionGenerationResult | None      # None whenever mission generation was never reached/attempted
    location_validation: LocationValidationResult | None = None
    message: str = ""
    # Remaining phases B-D (all optional, None when no building was resolved):
    building_facts: BuildingFacts | None = None          # Phase B -- what project documents say (height + citation)
    inspection_ring: InspectionRing | None = None         # Phase C -- perimeter reference points given to the planner
    safety_constraints: object | None = None              # Phase D -- SafetyConstraints to pass to RAMROrchestrator.handle_event()
    camera_aims: list = field(default_factory=list)       # inspection_camera.CameraAim per oriented waypoint

    @property
    def accepted(self) -> bool:
        """True only when a mission was generated, passed Phase 5's rule validation, AND (if applicable) the new location-specific check."""
        if self.status != NaturalLanguageMissionStatus.GENERATED:
            return False
        if self.mission_result is None or not self.mission_result.accepted:
            return False
        if self.location_validation is not None and not self.location_validation.valid:
            return False
        return True

    def inspection_summary(self) -> dict | None:
        """
        What the drone side needs, in one place: the building (its point and
        its DETECTED outline polygon, with where the outline came from), the
        ring, and the final waypoints with camera orientation -- the same
        Mission that is uploaded to PX4 (app/mission/converter.py).
        """
        location = self.location_result.location if (self.location_result and self.location_result.location) else None
        if location is None:
            return None
        rings = location.footprint_rings
        meta = getattr(location, "geometry_meta", None) or {}
        if rings:
            pts = [p for r in rings for p in r]
            centre = [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]
        else:
            centre = [location.latitude, location.longitude]
        mission = self.mission_result.mission if self.mission_result else None
        return {
            "building": {
                "name": location.building or location.campus,
                "geocoded_point": [location.latitude, location.longitude],
                "centre": [round(centre[0], 7), round(centre[1], 7)],
                "outline_detected": bool(rings),
                "outline_source": location.geometry_source,
                "outline_id": meta.get("id"),
                "outline_confidence": meta.get("confidence"),
                "outline_height_m": meta.get("height_m"),
                "outline_height_source": meta.get("height_source"),
                "outline_note": location.geometry_note,
                "polygon": [[[round(a, 7), round(b, 7)] for a, b in r] for r in rings],
            },
            "ring": {
                "shape": self.inspection_ring.shape, "standoff_m": self.inspection_ring.standoff_m,
                "spacing_m": round(self.inspection_ring.spacing_m, 1), "points": len(self.inspection_ring.points),
                "blocked_points": self.inspection_ring.blocked,
            } if self.inspection_ring else None,
            "waypoints": [
                {
                    "sequence": i.sequence, "command": i.command.value,
                    "latitude": i.latitude, "longitude": i.longitude, "altitude_m": i.altitude,
                    "yaw_deg": i.yaw_deg, "gimbal_pitch_deg": i.gimbal_pitch_deg, "take_photo": i.take_photo,
                }
                for i in mission.mission_items
            ] if mission else [],
            "accepted": self.accepted,
        }

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "intent": self.intent.model_dump(),
            "location_result": self.location_result.as_dict() if self.location_result else None,
            "mission": self.mission_result.mission.model_dump() if self.mission_result else None,
            "mission_validation": self.mission_result.validation.as_dict() if self.mission_result else None,
            "location_validation": self.location_validation.as_dict() if self.location_validation else None,
            "building_facts": self.building_facts.as_dict() if self.building_facts else None,
            "inspection_ring": self.inspection_ring.as_dict() if self.inspection_ring else None,
            "ramr_safety_constraints": self.safety_constraints.as_dict() if self.safety_constraints else None,
            "inspection": self.inspection_summary(),
            "message": self.message,
            "accepted": self.accepted,
        }


def _road_anchor(resolver, user_request: str, intent) -> LocationResolutionResult:
    """
    The point a road scan starts from. "... near <landmark>" -> that landmark
    (within the same area); otherwise the road's own mapped point. The road
    is then taken from the map as the named road nearest that point.
    """
    from app.mission.road_scan import road_name_from_address

    road_name = road_name_from_address(intent.address)
    area = intent.address[len(road_name):].lstrip(", ").strip()
    match = re.search(r"\bnear\s+(.+?)\s*\.?$", user_request or "", re.IGNORECASE)
    landmark = match.group(1).strip(" .") if match else None
    if landmark:
        query = f"{landmark}, {area}" if area else landmark
        anchored = resolver.resolve_road_anchor(query)
        if anchored.status == LocationResolutionStatus.RESOLVED:
            return anchored
        fallback = resolver.resolve_road_anchor(intent.address)
        if fallback.status == LocationResolutionStatus.RESOLVED and fallback.location is not None:
            fallback.warnings.append(f"Landmark '{landmark}' was not found on the map -- the road itself was used as the start point.")
        return fallback
    return resolver.resolve_road_anchor(intent.address)


def _road_scan_result(
    *, intent, location_result, location, road_fetch, radius_km, home_position, battery_percent,
) -> "NaturalLanguageMissionResult":
    """Road scan: the mapped road with the requested name nearest the place, flown once along a stretch of it."""

    def not_possible(message: str) -> "NaturalLanguageMissionResult":
        return NaturalLanguageMissionResult(
            status=NaturalLanguageMissionStatus.SCAN_NOT_POSSIBLE, intent=intent,
            location_result=location_result, mission_result=None, message=message,
        )

    road_name = road_name_from_address(intent.address)
    try:
        ways = road_fetch(location.latitude, location.longitude, float(settings.road_search_radius_m))
    except GeocodingError as exc:
        return not_possible(f"Road map lookup failed ({exc}) -- road scan not planned. Try again when the map service is available.")

    try:
        plan = plan_road_scan(ways, road_name, (location.latitude, location.longitude), altitude_m=intent.altitude_m)
    except RoadScanError as exc:
        return not_possible(str(exc))

    mission_result = MissionGenerationResult(mission=plan.mission, validation=validate_mission(plan.mission), attempts=1)
    location_validation = None
    if mission_result.accepted:
        location_validation = validate_location_mission_safety(
            plan.mission, location, None, radius_km=radius_km,
            home=home_position, battery_percent=battery_percent, neighbours=None, check_obstacles=False,
        )
    accepted = mission_result.accepted and (location_validation is None or location_validation.valid)
    status = NaturalLanguageMissionStatus.GENERATED if accepted else NaturalLanguageMissionStatus.LOCATION_SAFETY_REJECTED
    message = (
        f"Road scan of {plan.road_name}: {len(plan.mission.mission_items) - 2} points over about "
        f"{plan.length_m:g} m at {plan.altitude_m:g} m ({plan.altitude_source}). "
        + " ".join(plan.warnings)
    )
    if not accepted:
        extra = location_validation.errors if location_validation else mission_result.validation.errors
        message = "Road scan failed a safety check: " + "; ".join(extra)
    if location_result is not None and location_result.warnings:
        message = " ".join(location_result.warnings) + " " + message
    return NaturalLanguageMissionResult(
        status=status, intent=intent, location_result=location_result, mission_result=mission_result,
        location_validation=location_validation, camera_aims=plan.camera_aims, message=message,
    )



# A perimeter inspection must actually fly around the building. The LLM is
# given the ring as reference points, but a small model sometimes returns a
# mission with no inspection waypoints at all (e.g. TAKEOFF -> RETURN_TO_LAUNCH),
# which is rule-valid yet useless. Such a mission is replaced with the ring
# itself, flown at the requested altitude; it is then checked like any other.
_MIN_INSPECTION_FRACTION = 0.5


def _inspection_waypoint_count(mission) -> int:
    return sum(1 for it in mission.mission_items if it.command in (MissionCommand.WAYPOINT, MissionCommand.LOITER)
               and it.latitude is not None and it.longitude is not None)


def _ring_mission(ring, altitude_m: float, name: str):
    """TAKEOFF -> one WAYPOINT per ring point (raised where a neighbour needs it) -> RETURN_TO_LAUNCH. None if a point is blocked."""
    details = list(ring.details or [])
    if not details or any(d.blocked for d in details):
        return None
    items = [MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=round(altitude_m, 1))]
    for d in details:
        alt = altitude_m if d.min_altitude_m is None else max(altitude_m, float(d.min_altitude_m))
        items.append(MissionItem(sequence=len(items) + 1, command=MissionCommand.WAYPOINT,
                                 latitude=round(d.latitude, 7), longitude=round(d.longitude, 7), altitude=round(alt, 1)))
    items.append(MissionItem(sequence=len(items) + 1, command=MissionCommand.RETURN_TO_LAUNCH))
    return Mission(mission_name=name, mission_items=items)


def _ensure_inspection_ring(mission_result, ring, intent, building: str):
    """Replace a perimeter mission that skips the ring with the ring itself. Returns a note, or None if unchanged."""
    mission = mission_result.mission
    if mission is None or ring is None or not ring.details:
        return None
    if _inspection_waypoint_count(mission) >= max(3, int(len(ring.details) * _MIN_INSPECTION_FRACTION)):
        return None
    altitude = intent.altitude_m
    if altitude is None:
        takeoff = next((it.altitude for it in mission.mission_items
                        if it.command == MissionCommand.TAKEOFF and it.altitude is not None), None)
        altitude = takeoff if takeoff is not None else 30.0
    altitude = min(max(float(altitude), float(settings.min_altitude_m)), float(settings.max_altitude_m))
    fixed = _ring_mission(ring, altitude, mission.mission_name or f"Inspection of {building}")
    if fixed is None:
        return None
    validation = validate_mission(fixed)
    if not validation.valid:
        return None
    mission_result.mission = fixed
    mission_result.validation = validation
    return (f"The planner returned only {_inspection_waypoint_count(mission)} inspection waypoint(s); "
            f"the {len(ring.details)}-point ring around the outline is flown instead at {altitude:g} m.")


def generate_mission_from_natural_language(
    user_request: str,
    *,
    location_resolver: LocationResolver | None = None,
    road_fetch=None,
    rag_k: int | None = None,
    max_validation_attempts: int | None = None,
    location_geofence_radius_km: float | None = None,
    building_facts_fn=None,
    home_position: tuple[float, float] | None = None,
    battery_percent: float | None = None,
) -> NaturalLanguageMissionResult:
    """
    The new end-to-end entry point this improvement adds. See this
    module's docstring for the full step-by-step flow.

    `location_resolver` is dependency-injected (defaults to a real
    `LocationResolver()`) purely so tests never need real network/RAG --
    same pattern as every other constructor in this project.

    Raises `LLMGenerationError` (from `app.llm.llm_client`) if the LLM
    itself is unreachable at either the intent-extraction or the
    mission-generation step -- same failure mode `generate_mission()`/
    `generate_validated_mission()`/`parse_mission_intent()` already have
    individually; this function does not add a new one.

    Remaining phases: `building_facts_fn` (default: the real RAG lookup,
    `lookup_building_facts`) is injectable for tests. `home_position`
    (lat, lon) and `battery_percent` (0-1) are optional live readings --
    supply them (e.g. from PX4 once connected) to enable the initial
    battery/RTH feasibility check; without them it is reported as
    not checked, never assumed fine.
    """
    resolver = location_resolver or LocationResolver()
    facts_fn = building_facts_fn or lookup_building_facts
    radius_km = location_geofence_radius_km if location_geofence_radius_km is not None else settings.location_geofence_radius_km

    intent = parse_mission_intent(user_request)

    if intent.scan_mode == ScanMode.ROAD and not intent.address:
        return NaturalLanguageMissionResult(
            status=NaturalLanguageMissionStatus.SCAN_NOT_POSSIBLE, intent=intent,
            location_result=None, mission_result=None,
            message="A road scan needs the road's name with its place, for example 'Road scan of Karanai Main Road, Sonalur, Tamil Nadu 600130'.",
        )

    if intent.scan_mode == ScanMode.STRUCTURAL and not (intent.institution or intent.campus or intent.building or intent.address):
        return NaturalLanguageMissionResult(
            status=NaturalLanguageMissionStatus.SCAN_NOT_POSSIBLE, intent=intent,
            location_result=None, mission_result=None,
            message="A structural scan needs one named building (for example 'Structural scan of Ripon Building, Chennai').",
        )

    if not intent.location_mentioned or not (intent.institution or intent.campus or intent.building or intent.address):
        # No real-world location to resolve -- the pipeline behaves
        # EXACTLY as it did before this improvement existed.
        mission_result = generate_validated_mission(
            user_request, rag_k=rag_k,
            max_validation_attempts=max_validation_attempts if max_validation_attempts is not None else MAX_VALIDATION_ATTEMPTS,
        )
        return NaturalLanguageMissionResult(
            status=NaturalLanguageMissionStatus.GENERATED, intent=intent,
            location_result=None, mission_result=mission_result,
            message="No real-world location was referenced -- generated without location resolution.",
        )

    if intent.scan_mode == ScanMode.ROAD and intent.address:
        location_result = _road_anchor(resolver, user_request, intent)
    elif intent.address:
        # Phase 15.5: a street address / landmark -> one geocoding lookup.
        location_result = resolver.resolve_address(intent.address)
    else:
        location_result = resolver.resolve(institution=intent.institution, campus=intent.campus, building=intent.building)

    if location_result.status == LocationResolutionStatus.AMBIGUOUS:
        log.warning("Mission generation STOPPED -- ambiguous location: %s", location_result.message)
        return NaturalLanguageMissionResult(
            status=NaturalLanguageMissionStatus.LOCATION_AMBIGUOUS, intent=intent,
            location_result=location_result, mission_result=None, message=location_result.message,
        )
    if location_result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED:
        log.warning("Mission generation STOPPED -- location not resolved: %s", location_result.message)
        return NaturalLanguageMissionResult(
            status=NaturalLanguageMissionStatus.LOCATION_NOT_RESOLVED, intent=intent,
            location_result=location_result, mission_result=None, message=location_result.message,
        )

    location = location_result.location

    # Remaining Phase A -- spec section 7: "Only continue to mission
    # planning when the location is sufficiently verified." A location
    # can be RESOLVED yet only LOW confidence (e.g. an OSM building match
    # that sits outside the campus it was asked for); that must stop
    # here, same as an unresolved one, rather than reach the planner.
    if not meets_min_confidence(location.confidence):
        message = (
            f"Location resolved with only {location.confidence.value} confidence "
            f"(minimum required: {settings.location_min_confidence}) -- mission generation stopped. "
            f"Verify the institution/campus/building names, or lower LOCATION_MIN_CONFIDENCE deliberately."
        )
        log.warning("Mission generation STOPPED -- %s", message)
        return NaturalLanguageMissionResult(
            status=NaturalLanguageMissionStatus.LOCATION_LOW_CONFIDENCE, intent=intent,
            location_result=location_result, mission_result=None, message=message,
        )

    if intent.scan_mode == ScanMode.ROAD:
        return _road_scan_result(
            intent=intent, location_result=location_result, location=location, road_fetch=road_fetch or fetch_named_roads,
            radius_km=radius_km, home_position=home_position, battery_percent=battery_percent,
        )

    # Remaining Phase B + C: building facts from the existing RAG
    # (height, cited) and the perimeter reference ring -- only for a
    # specific building, not a campus-only request.
    facts: BuildingFacts | None = None
    ring: InspectionRing | None = None
    neighbours = None
    neighbour_warning: str | None = None
    context_parts = [_render_location_context(location)]
    if location.building:
        facts = facts_fn(location.building, location.campus)
        # Boundary fix: other mapped buildings around the target are obstacles.
        lookup = getattr(resolver, "neighbouring_obstacles", None)
        if lookup is not None:
            _, _, extent_m, _ = footprint_extent(location)
            search = extent_m + settings.inspection_standoff_m + settings.neighbour_search_margin_m
            neighbours, neighbour_warning = lookup(location, search)
        ring = compute_inspection_ring(location, neighbours=neighbours, start_near=home_position)
        if neighbour_warning:
            ring.notes.append(neighbour_warning)
        if facts is not None:
            context_parts.append(render_building_facts(facts))
        context_parts.append(render_inspection_ring(ring, altitude_hint_m=intent.altitude_m))
    location_context = "\n".join(context_parts)
    ramr_constraints = safety_constraints_for_location(location, radius_km=radius_km)

    structural = intent.scan_mode == ScanMode.STRUCTURAL
    camera_aims: list = []
    if structural:
        # Deterministic plan (no LLM): stacked rings up the facade, checked by Phase 5 below.
        if not location.building:
            return NaturalLanguageMissionResult(
                status=NaturalLanguageMissionStatus.SCAN_NOT_POSSIBLE, intent=intent,
                location_result=location_result, mission_result=None,
                message="A structural scan needs one named building, not a campus or area.",
            )
        try:
            plan = plan_structural_scan(
                location, facts, neighbours=neighbours, start_near=home_position,
                requested_altitude_m=intent.altitude_m, name=f"Structural scan of {location.building}",
            )
        except StructuralScanError as exc:
            log.warning("Structural scan NOT planned: %s", exc)
            return NaturalLanguageMissionResult(
                status=NaturalLanguageMissionStatus.SCAN_NOT_POSSIBLE, intent=intent,
                location_result=location_result, mission_result=None,
                building_facts=facts, inspection_ring=ring, safety_constraints=ramr_constraints,
                message=str(exc),
            )
        mission_result = MissionGenerationResult(
            mission=plan.mission, validation=validate_mission(plan.mission), attempts=1,
        )
        camera_aims = plan.camera_aims
        ring = compute_inspection_ring(location, neighbours=neighbours, start_near=home_position, n_points=plan.points_per_level)
        ring.notes.extend(plan.warnings)
    else:
        mission_result = generate_validated_mission(
            user_request, rag_k=rag_k,
            max_validation_attempts=max_validation_attempts if max_validation_attempts is not None else MAX_VALIDATION_ATTEMPTS,
            location_context=location_context,
        )

    location_validation = None
    if mission_result.accepted and location.building and not structural:
        note = _ensure_inspection_ring(mission_result, ring, intent, location.building)
        if note:
            log.warning(note)
            ring.notes.append(note)
    if mission_result.accepted and location.building and not structural:
        # Face the real outline and take a photo at each inspection waypoint (no position/altitude change).
        mission_result.mission, camera_aims = orient_camera(mission_result.mission, location, facts)
    if mission_result.accepted:
        location_validation = validate_location_mission_safety(
            mission_result.mission, location, facts, radius_km=radius_km,
            home=home_position, battery_percent=battery_percent,
            neighbours=neighbours, check_obstacles=ring is not None,
        )
        if not location_validation.valid:
            log.warning(
                "Mission generated and rule-valid, but REJECTED by location-specific safety check: %s",
                location_validation.errors,
            )
            return NaturalLanguageMissionResult(
                status=NaturalLanguageMissionStatus.LOCATION_SAFETY_REJECTED, intent=intent,
                location_result=location_result, mission_result=mission_result,
                location_validation=location_validation,
                building_facts=facts, inspection_ring=ring, safety_constraints=ramr_constraints, camera_aims=camera_aims,
                message="Generated mission failed a location-specific safety check: " + "; ".join(location_validation.errors),
            )

    return NaturalLanguageMissionResult(
        status=NaturalLanguageMissionStatus.GENERATED, intent=intent,
        location_result=location_result, mission_result=mission_result,
        location_validation=location_validation,
        building_facts=facts, inspection_ring=ring, safety_constraints=ramr_constraints, camera_aims=camera_aims,
        message=f"Location resolved ({location.confidence.value} confidence, source {location.source.value}).",
    )


def _demo_cli() -> None:
    """
    Manual test entry point:
        python -m app.mission.location_aware_generator "Inspect AB1 of VIT Chennai at 30 meters."
    Needs the configured LLM provider AND real internet access to
    Nominatim reachable -- neither has been exercised together from
    inside this sandbox (see app/location/geocoding_service.py's
    docstring). Run this on your own machine to verify end-to-end.
    """
    import json
    import sys

    from app.llm.llm_client import LLMGenerationError

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    request = " ".join(sys.argv[1:]) or "Inspect AB1 of VIT Chennai at 30 meters."
    print(f"Request: {request}\n")

    try:
        result = generate_mission_from_natural_language(request)
    except LLMGenerationError as exc:
        print(f"\nLLM generation failed: {exc}")
        raise SystemExit(1)

    print(json.dumps(result.as_dict(), indent=2, default=str))
    if not result.accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    _demo_cli()
