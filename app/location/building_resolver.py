"""
Real-world coordinate & building resolution improvement -- Step 2 of
hierarchical resolution ("Resolve the requested building... AB1 -> AB1
building within VIT Chennai campus").

    IMPORTANT: Do NOT assume that a normal geocoder can always resolve
    internal campus building codes... Resolution priority:
      1. OpenStreetMap building/location data
      2. Existing project RAG/building knowledge
      3. Existing authoritative campus/building dataset
      4. Existing project map/building information
    If the exact building cannot be reliably resolved: return
    LOCATION_NOT_RESOLVED. NEVER fabricate coordinates. NEVER guess
    coordinates. NEVER silently use the campus center as the building
    location.

`BuildingResolver.resolve()` implements exactly that priority list, and
returns `None` (never a guessed/campus-centroid substitute) if nothing
in it succeeds -- `LocationResolver` is what turns a `None` here into
`LOCATION_NOT_RESOLVED`.
"""

from __future__ import annotations

import logging
import re

from app.config import settings

from app.location.building_names import building_name_variants, mentions_building
from app.location.geocoding_service import GeocodingCandidate, GeocodingError
from app.location.geofence import haversine_km, point_in_bbox

log = logging.getLogger(__name__)

# A resolved coordinate embedded in project knowledge-base text, e.g.
# "AB1: latitude=12.9692, longitude=80.1499" -- the interface priority
# #2 ("Existing project RAG/building knowledge") reads. Deliberately
# permissive about spacing/casing/separators (lat/lon vs latitude/
# longitude, "=" or ":") since it's reading documents a human wrote, not
# a machine-generated format. See this module's docstring: no document
# in this project's current knowledge base (`data/documents/`) actually
# contains building coordinates yet, so this path is real, tested
# (against a fake retriever), and currently unexercised by any real
# data -- exactly the same "defined now, populated later" situation
# Phase 3's CV event types were in before Phase 13 finally used them.
_COORD_PATTERN = re.compile(
    r"lat(?:itude)?\s*[:=]\s*(-?\d+(?:\.\d+)?)\D+lon(?:gitude)?\s*[:=]\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


class AmbiguousBuildingError(GeocodingError):
    """Phase 15 -- several DIFFERENT places inside the campus match the building name; refusing to pick one."""

    def __init__(self, building: str, candidates: list[GeocodingCandidate]):
        self.building = building
        self.candidates = candidates
        super().__init__(f"{building!r} matches {len(candidates)} different places: {self.names()}")

    def names(self) -> str:
        return "; ".join(f"{c.short_name} ({c.latitude:.5f}, {c.longitude:.5f})" for c in self.candidates)


def _building_kind(variant: str) -> str | None:
    """
    The building's generic kind, without its number: "Academic Block 1" ->
    "Academic Block", "Hostel Block 3" -> "Hostel Block". None for short codes
    ("AB1" -- too vague to search on) and names without a number.
    """
    m = re.match(r"^\s*(.*?[A-Za-z].*?)[\s-]*\d+[A-Za-z]?\s*$", variant)
    if not m:
        return None
    kind = m.group(1).strip(" -")
    return kind if len(kind.replace(" ", "")) >= 5 and " " in kind else None


def _distinct_places(candidates: list[GeocodingCandidate]) -> list[GeocodingCandidate]:
    """
    Collapses results that are the same real place (same place ID, or
    within `building_ambiguity_distance_m` of each other -- e.g. a building
    and its own entrance). Keeps provider order, so [0] is the best match.
    """
    distinct: list[GeocodingCandidate] = []
    for c in candidates:
        same = False
        for d in distinct:
            if c.place_id and d.place_id and c.place_id == d.place_id:
                same = True
            elif haversine_km(c.latitude, c.longitude, d.latitude, d.longitude) * 1000.0 <= settings.building_ambiguity_distance_m:
                same = True
            if same:
                break
        if not same:
            distinct.append(c)
    return distinct


def _coordinate_after_name(text: str, variants: list[str]):
    """
    The coordinate that belongs to THIS building: the nearest
    "lat... lon..." pair following one of its names (within a short span,
    with no other "lat" in between). A document listing several buildings,
    e.g. "AB2: lat=.., lon=.. AB1: lat=.., lon=..", must give AB1 its own
    pair -- never simply the first coordinate in the chunk.
    """
    for variant in variants:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(variant)}(?![A-Za-z0-9])(?:(?!lat).){{0,40}}?" + _COORD_PATTERN.pattern,
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(text)
        if match:
            return match
    return None


def _rag_lookup(building_name: str, campus_name: str | None, retrieve_fn) -> tuple[float, float, str] | None:
    """Priority #2/#3/#4: search this project's own RAG knowledge base for an explicit coordinate near a mention of `building_name`."""
    # Remaining Phase A: search with every name variant ("AB1" AND
    # "Academic Block 1"), and accept a chunk that mentions ANY of them
    # as a whole name -- a document almost never uses the short code.
    variants = building_name_variants(building_name)
    query = f"{' / '.join(variants)} building coordinates location" + (f" {campus_name}" if campus_name else "")
    try:
        chunks = retrieve_fn(query, k=3)
    except Exception as exc:  # noqa: BLE001 -- RAG being unavailable (e.g. no index built) must not crash resolution
        log.warning("RAG building-knowledge lookup failed for %r: %s", building_name, exc)
        return None

    for chunk in chunks:
        if not mentions_building(chunk.text, variants):
            continue
        match = _coordinate_after_name(chunk.text, variants)
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return lat, lon, chunk.source
    return None


class BuildingResolver:
    """
    Usage:
        resolver = BuildingResolver(geocoding_service, retrieve_fn=app.rag.retriever.retrieve)
        candidate, source_name = resolver.resolve("AB1", campus_candidate)
    """

    def __init__(self, geocoding_service, *, retrieve_fn=None) -> None:
        self._geocoding_service = geocoding_service
        self.last_seen: list[dict] = []
        # Injected so tests don't need a real Chroma index -- defaults
        # to the real Phase 2 retriever, reused unchanged.
        if retrieve_fn is None:
            from app.rag.retriever import retrieve as retrieve_fn  # noqa: PLC0415 -- lazy: avoid a hard Chroma import for callers that never hit this path
        self._retrieve_fn = retrieve_fn

    def resolve_via_openstreetmap(
        self, building_name: str, campus: GeocodingCandidate
    ) -> GeocodingCandidate | None:
        """
        Priority #1. Tries a few non-hardcoded query phrasings (just the
        building name/code, and the same name with "building" appended),
        scoped to the campus's own bounding box first (`bounded=True` --
        a real restriction), then as a soft preference only
        (`bounded=False`) if the strict search finds nothing -- OSM
        sometimes places a feature just outside a campus's own mapped
        boundary. Returns the first candidate whose own coordinates
        actually fall inside the campus bounding box (the "campus
        relationship" check named in the spec) -- never just the
        top-ranked Nominatim result regardless of location.
        """
        # Remaining Phase A: every name variant ("AB1", "AB-1",
        # "Academic Block 1", ...). Phase 15 adds a campus-context form
        # ("AB1, VIT Chennai") which Google Text Search handles well.
        variants = building_name_variants(building_name)
        campus_short = campus.short_name
        queries: list[str] = []
        for variant in variants:
            queries += [variant, f"{variant} building"]
            if campus_short:
                queries.append(f"{variant}, {campus_short}")

        # Phase 15.2 -- automatic campus-wide discovery. If the exact name
        # finds nothing, list the campus's places of the same KIND ("AB1" ->
        # "Academic Block") and keep only the one whose own name matches
        # (e.g. Google's "VIT Chennai Academic Block - 1"). Same validation
        # as every other candidate: name + inside campus + not the campus.
        discovery: list[str] = []
        for variant in variants:
            kind = _building_kind(variant)
            if kind:
                discovery += [kind, f"{kind}, {campus_short}"] if campus_short else [kind]
        queries += [q for q in dict.fromkeys(discovery) if q not in queries]
        discovery_set = set(discovery)

        # Phase 15: a provider whose bounded search is a strict restriction
        # (Google locationRestriction) gains nothing from the second,
        # unbounded pass -- results must lie inside the campus either way.
        strict = getattr(self._geocoding_service, "strict_viewbox", False)
        passes = (True,) if (campus.bounding_box is not None and strict) else (
            (True, False) if campus.bounding_box is not None else (False,)
        )
        self.last_seen = []  # Phase 15.1: every candidate the provider returned, with why it was accepted/rejected
        for bounded in passes:
            for query in queries:
                candidates = self._geocoding_service.search(
                    query, viewbox=campus.bounding_box, bounded=bounded, want_polygon=True,
                    limit=20 if query in discovery_set else 5,  # discovery lists the whole campus
                )
                accepted = []
                for c in candidates:
                    reason = self._rejection_reason(c, campus, variants)
                    self.last_seen.append({"query": query, "name": c.display_name, "latitude": c.latitude,
                                           "longitude": c.longitude, "accepted": reason is None, "reason": reason or "name + campus match"})
                    log.info("Building candidate for %r: %r -> %s", query, c.display_name, "ACCEPTED" if reason is None else f"rejected ({reason})")
                    if reason is None:
                        accepted.append(c)
                if not accepted:
                    continue
                distinct = _distinct_places(accepted)
                if len(distinct) > 1:
                    raise AmbiguousBuildingError(building_name, distinct)
                return distinct[0]
        return None

    @classmethod
    def _is_valid_building_match(cls, candidate: GeocodingCandidate, campus: GeocodingCandidate, variants: list[str]) -> bool:
        return cls._rejection_reason(candidate, campus, variants) is None

    @staticmethod
    def _rejection_reason(candidate: GeocodingCandidate, campus: GeocodingCandidate, variants: list[str]) -> str | None:
        """
        Phase 15 candidate validation -- "Google returned a result" is NOT
        "the correct building". A candidate is accepted only if ALL hold:
          1. it lies inside the resolved campus bounding box (campus relationship);
          2. its own NAME matches the requested building (any variant, whole name);
          3. it is not the campus itself (fuzzy search often returns the
             university for "AB1, VIT Chennai").
        """
        if campus.bounding_box is not None and not point_in_bbox(candidate.latitude, candidate.longitude, campus.bounding_box):
            return "outside the campus area"
        if not mentions_building(candidate.short_name, variants):
            return f"name {candidate.short_name!r} does not match {variants[0]!r}"
        if candidate.place_id and campus.place_id and candidate.place_id == campus.place_id:
            return "this is the campus itself"
        if candidate.short_name.lower() == campus.short_name.lower():
            return "this is the campus itself"
        return None

    def resolve_via_rag(self, building_name: str, campus_name: str | None) -> tuple[float, float, str] | None:
        """Priority #2/#3/#4 -- see `_rag_lookup()` above."""
        return _rag_lookup(building_name, campus_name, self._retrieve_fn)
