"""
Real-world coordinate & building resolution improvement -- data models.

    Create a standard object/model for the resolved location... IMPORTANT:
    the example coordinates are placeholders. DO NOT insert hard-coded
    coordinates.

Nothing in this file computes or invents a coordinate -- these are pure
data containers, exactly like every other `@dataclass`/Pydantic model in
this project (`ReplanningEvent`, `MissionState`, `InspectionQualityResult`,
...). `LocationResolver` (`location_resolver.py`) is the only thing that
fills one in, and only from a real geocoding response or a real,
project-supplied knowledge-base entry -- never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ResolutionType(str, Enum):
    """
    Whether a `ResolvedLocation` is an exact point (no footprint data
    available) or a real building footprint/polygon. See this project's
    spec: "Do NOT treat a campus centroid as an exact building
    coordinate" -- `resolution_type` is what lets a caller tell the
    difference between "this IS the building" (BUILDING, or POINT when
    only a building's own point-location was found) and "this is only
    the surrounding campus" (see `LocationResolver` -- a campus-only
    resolution is always POINT and is never substituted for a requested
    building that could not itself be resolved).
    """

    POINT = "POINT"
    BUILDING = "BUILDING"


class LocationSource(str, Enum):
    """Where a `ResolvedLocation`'s coordinates ultimately came from."""

    OPENSTREETMAP = "OPENSTREETMAP"      # a live Nominatim search result
    RAG_KNOWLEDGE = "RAG_KNOWLEDGE"      # this project's own RAG knowledge base (app.rag.retriever)
    CACHE = "CACHE"                       # a previously-verified result served from LocationCache
    GOOGLE_PLACES = "GOOGLE_PLACES"      # Phase 15 -- Google Places API (New) Text Search (+ Geocoding v4 outline)
    USER_VERIFIED = "USER_VERIFIED"      # Phase 15.4 -- the user clicked the building once on the mission map


class GeometryType(str, Enum):
    """
    Phase 15 -- the exact shape of the geometry behind a ResolvedLocation.
    Kept ALONGSIDE `ResolutionType` (POINT/BUILDING, unchanged for
    backward compatibility) rather than replacing it. Never "promoted":
    a point is never turned into a polygon here or anywhere else.
    """

    POINT = "POINT"
    POLYGON = "POLYGON"
    MULTIPOLYGON = "MULTIPOLYGON"


class LocationConfidence(str, Enum):
    """
    See this project's spec section 7 ("Location Confidence") for the
    factors that feed this. `UNRESOLVED` is included for the enum's
    completeness (the spec explicitly lists it as a possible value) but
    a `ResolvedLocation` object is only ever constructed once *something*
    has resolved -- a fully-unresolved outcome is represented by
    `LocationResolutionResult.status == LOCATION_NOT_RESOLVED` with
    `location=None`, not by a `ResolvedLocation` carrying this value.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNRESOLVED = "UNRESOLVED"


class LocationResolutionStatus(str, Enum):
    """What `LocationResolver.resolve()` always returns as its top-level outcome."""

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"                    # spec CASE 2 -- e.g. a building with no institution/campus given
    LOCATION_NOT_RESOLVED = "LOCATION_NOT_RESOLVED"  # spec CASE 1/3 -- never fabricated, never guessed


@dataclass
class MatchDetails:
    """
    The individual factors `LocationResolver` checks when computing
    `ResolvedLocation.confidence` -- kept as their own structured object
    (rather than folded into a single confidence label) so a caller/log/
    test can see exactly which factors were satisfied, matching this
    project's spec's own worked example:
        Institution Match       [x]
        Campus Match            [x]
        Building Match          [x]
        Coordinates Valid       [x]
        Campus Relationship     [x]
        Confidence              HIGH
    """

    institution_match: bool = False
    campus_match: bool = False
    building_match: bool = False
    coordinates_valid: bool = False
    campus_relationship: bool = False  # the building falls inside the campus's own bounding box
    geometry_available: bool = False
    source_reliable: bool = False

    def as_dict(self) -> dict:
        return {
            "institution_match": self.institution_match,
            "campus_match": self.campus_match,
            "building_match": self.building_match,
            "coordinates_valid": self.coordinates_valid,
            "campus_relationship": self.campus_relationship,
            "geometry_available": self.geometry_available,
            "source_reliable": self.source_reliable,
        }


@dataclass
class ResolvedLocation:
    """
    A verified real-world location -- the thing `LocationResolver`
    produces and the thing the rest of the pipeline (RAG context, the
    LLM mission planner, the new location-specific safety check) is
    allowed to treat as ground truth. Field names follow the spec's own
    conceptual example field-for-field, adapted to this project's
    dataclass/snake_case convention.
    """

    institution: str | None
    campus: str | None
    building: str | None
    latitude: float
    longitude: float
    resolution_type: ResolutionType
    source: LocationSource
    confidence: LocationConfidence
    geometry_available: bool
    # Polygon footprint as a list of (latitude, longitude) points, when
    # `geometry_available` is True (e.g. a building outline from OSM's
    # `polygon_geojson=1`). None otherwise -- never fabricated.
    geometry: list[tuple[float, float]] | None = None
    # (south, north, west, east) -- Nominatim's own `boundingbox` shape,
    # kept for the campus-relationship check and for scoping a
    # subsequent building search (see building_resolver.py).
    bounding_box: tuple[float, float, float, float] | None = None
    raw_source_name: str | None = None  # e.g. Nominatim's own display_name, for traceability/debugging
    match_details: MatchDetails = field(default_factory=MatchDetails)
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Phase 15 additions -- all optional, so every existing construction is unchanged.
    place_id: str | None = None             # Google place ID (cacheable indefinitely per Google's terms)
    formatted_address: str | None = None
    # Every outer ring when the footprint is a MultiPolygon; `geometry` above
    # stays the largest ring so existing checks keep working unchanged.
    geometry_parts: list[list[tuple[float, float]]] | None = None
    # Phase 15.1: where the outline came from when it differs from `source`
    # (e.g. a Google point whose boundary is the OSM footprint containing it).
    geometry_source: str | None = None
    geometry_note: str | None = None        # how the outline was recognized (name tag / contains point / snapped)
    # The outline's own record: {"id", "source", "confidence", "height_m", "height_source"} -- Microsoft ML
    # footprints carry a detection confidence and an average-height estimate; OSM ones may carry a height tag.
    geometry_meta: dict | None = None

    @property
    def geometry_type(self) -> "GeometryType":
        if not self.geometry_available or not self.geometry:
            return GeometryType.POINT
        if self.geometry_parts and len(self.geometry_parts) > 1:
            return GeometryType.MULTIPOLYGON
        return GeometryType.POLYGON

    @property
    def footprint_rings(self) -> list[list[tuple[float, float]]]:
        """Every real footprint ring (empty for a point-only location)."""
        if not self.geometry_available or not self.geometry:
            return []
        return [list(r) for r in self.geometry_parts] if self.geometry_parts else [list(self.geometry)]

    def as_dict(self) -> dict:
        return {
            "place_id": self.place_id,
            "formatted_address": self.formatted_address,
            "geometry_type": self.geometry_type.value,
            "geometry_parts": self.geometry_parts,
            "geometry_source": self.geometry_source,
            "geometry_note": self.geometry_note,
            "geometry_meta": self.geometry_meta,
            "verified": True,  # a ResolvedLocation only exists once a real source verified it (see LocationResolver)
            "institution": self.institution,
            "campus": self.campus,
            "building": self.building,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "resolution_type": self.resolution_type.value,
            "source": self.source.value,
            "confidence": self.confidence.value,
            "geometry_available": self.geometry_available,
            "geometry": self.geometry,
            "bounding_box": self.bounding_box,
            "raw_source_name": self.raw_source_name,
            "match_details": self.match_details.as_dict(),
            "resolved_at": self.resolved_at.isoformat(),
        }


@dataclass
class LocationResolutionResult:
    """What `LocationResolver.resolve()` always returns."""

    status: LocationResolutionStatus
    location: ResolvedLocation | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    # Free-form diagnostic detail (e.g. which query variants were tried,
    # what OSM/cache/RAG returned) -- for logging/debugging, never
    # required by a caller for correctness.
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.status == LocationResolutionStatus.RESOLVED and self.location is not None

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "location": self.location.as_dict() if self.location else None,
            "message": self.message,
            "warnings": self.warnings,
            "details": self.details,
        }
