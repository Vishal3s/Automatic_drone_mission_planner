"""
Real-world coordinate & building resolution improvement -- the main
orchestrator ("Create a modular component responsible for converting
natural-language locations into verified real-world locations").

    LocationResolver should receive information such as: institution,
    campus, building. And return a structured resolved location.

This ties together everything else in `app/location/` in the exact
priority/flow order the spec lays out (sections 4 and 13):

    Check verified cache
        -> if not found: OpenStreetMap / Nominatim (campus, then building)
        -> if building not found there: existing RAG / building knowledge
        -> if still not confidently resolved: LOCATION_NOT_RESOLVED

Nothing here calls Nominatim, the RAG retriever, or the cache file
directly -- `GeocodingService`/`CampusResolver`/`BuildingResolver`/
`LocationCache` do that, all dependency-injected via the constructor
(same pattern `RAMROrchestrator`/`AdaptiveMissionReplanner` already use),
so this class's own logic is pure, deterministic, and testable with
fakes -- no network, no real Chroma index needed for its own tests.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.location.building_resolver import BuildingResolver
from app.location.campus_resolver import CampusResolver
from app.config import settings
from app.location.building_footprints import (
    AmbiguousFootprintError,
    OverpassFootprintService,
    find_named_footprint,
    footprint_for_point,
)
from app.location.building_names import building_name_variants
from app.location.building_resolver import AmbiguousBuildingError
from app.location.inspection_geometry import polygon_centroid
from app.location.footprint_sources import SOURCE_LABELS, FootprintSources, as_sources, build_footprint_sources
from app.location.verified_buildings import VerifiedBuildingStore
from app.location.geocoding_service import GeocodingCandidate, GeocodingError
from app.location.providers import get_geocoding_service, provider_name, source_for
from app.location.geofence import haversine_km, point_in_bbox
from app.location.location_cache import LocationCache
from app.location.models import (
    LocationConfidence,
    LocationResolutionResult,
    LocationResolutionStatus,
    LocationSource,
    MatchDetails,
    ResolutionType,
    ResolvedLocation,
)

log = logging.getLogger(__name__)

_DEFAULT = object()  # "not passed" sentinel for footprint_service


# Results that name a whole area, not a place to inspect (Google types / OSM types).
_VAGUE_TYPES = {
    "locality", "sublocality", "sublocality_level_1", "sublocality_level_2", "neighborhood", "political",
    "administrative_area_level_1", "administrative_area_level_2", "administrative_area_level_3",
    "country", "postal_code", "colloquial_area", "route", "geocode",
    "city", "town", "village", "hamlet", "county", "state", "region", "postcode", "suburb",
    "neighbourhood", "quarter", "administrative", "district", "municipality", "province",
}


def _campus_query(institution: str | None, campus: str | None) -> str:
    """
    One search text for the institution + campus. "Vellore Institute of
    Technology" + "Chennai" must be searched together -- searching
    "Chennai" alone finds the city, not the campus.
    """
    inst, camp = (institution or "").strip(), (campus or "").strip()
    if not inst or not camp:
        return inst or camp
    a, b = inst.lower(), camp.lower()
    if a == b or b in a:
        return inst
    if a in b:
        return camp
    return f"{inst} {camp}"


def _too_vague(candidate: GeocodingCandidate) -> bool:
    return (candidate.place_type or "").lower() in _VAGUE_TYPES or (candidate.category or "").lower() == "boundary"


def _location_from_verified(record: dict, institution, campus, building) -> ResolvedLocation:
    """A ResolvedLocation from a user-verified record (see verified_buildings.py)."""
    geometry = [tuple(p) for p in record["geometry"]] if record.get("geometry") else None
    parts = [[tuple(p) for p in ring] for ring in record["geometry_parts"]] if record.get("geometry_parts") else None
    has_outline = geometry is not None
    match = MatchDetails(
        institution_match=True, campus_match=True, building_match=True, coordinates_valid=True,
        campus_relationship=True, geometry_available=has_outline, source_reliable=True,
    )
    return ResolvedLocation(
        institution=institution or record.get("institution"), campus=campus or record.get("campus"), building=building,
        latitude=float(record["latitude"]), longitude=float(record["longitude"]),
        resolution_type=ResolutionType.BUILDING if has_outline else ResolutionType.POINT,
        source=LocationSource.USER_VERIFIED,
        # A human pointed at this exact building. HIGH with a real outline
        # around the click; MEDIUM when only the clicked point is known.
        confidence=LocationConfidence.HIGH if has_outline else LocationConfidence.MEDIUM,
        geometry_available=has_outline, geometry=geometry, geometry_parts=parts,
        raw_source_name=f"User-verified on the map ({record.get('selected_at', '')})",
        match_details=match,
        geometry_source=(record.get("geometry_source") or "OPENSTREETMAP") if has_outline else None,
        geometry_note=record.get("geometry_note"),
        geometry_meta=record.get("geometry_meta"),
    )


def footprint_meta(footprint) -> dict:
    """The outline's own record, kept with the location (shown in the UI, returned by the API)."""
    def _f(key):
        try:
            return float(footprint.tags.get(key))
        except (TypeError, ValueError):
            return None

    return {
        "id": footprint.osm_id,
        "source": footprint.source,
        "confidence": _f("confidence"),
        "height_m": _f("height"),
        "height_source": footprint.tags.get("height_source") or ("OSM height tag" if footprint.tags.get("height") else None),
    }


def _below_min_confidence(footprint) -> bool:
    if footprint.source != "MICROSOFT":
        return False
    try:
        return float(footprint.tags.get("confidence")) < settings.ms_footprints_min_confidence
    except (TypeError, ValueError):
        return False  # not scored -> not filtered


def _apply_outline(location, outline) -> None:
    footprint, note = outline
    location.geometry_source = footprint.source
    location.geometry_note = note
    location.geometry_meta = footprint_meta(footprint)


def _largest_first(rings):
    """(largest ring, all rings if more than one else None) -- matches ResolvedLocation.geometry/geometry_parts."""
    from app.location.google_places_service import _ring_area  # planar area, only used for ordering

    ordered = sorted(rings, key=_ring_area, reverse=True) if rings else []
    if not ordered:
        return None, None
    return ordered[0], (ordered if len(ordered) > 1 else None)


def _valid_coordinate(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def _compute_confidence(match: MatchDetails, source: LocationSource) -> LocationConfidence:
    """
    See this project's spec section 7. Deterministic and documented,
    not a black box:
      - RAG-sourced results are capped at MEDIUM regardless of match
        count -- they were never independently geocoded/live-verified,
        only found as text in this project's own knowledge base (see
        `building_resolver.py`'s docstring).
      - Otherwise, HIGH needs a strong majority of the tracked factors
        satisfied (institution/campus/building match, coordinates
        valid, the resolved point actually sitting inside its campus's
        bounding box, and a reliable source); fewer -> MEDIUM; very few
        -> LOW.
    """
    if not match.coordinates_valid:
        return LocationConfidence.UNRESOLVED

    score = sum(
        [
            match.institution_match,
            match.campus_match,
            match.building_match,
            match.campus_relationship,
            match.source_reliable,
        ]
    )
    if source == LocationSource.RAG_KNOWLEDGE:
        return LocationConfidence.MEDIUM if score >= 2 else LocationConfidence.LOW
    if score >= 4:
        return LocationConfidence.HIGH
    if score >= 2:
        return LocationConfidence.MEDIUM
    return LocationConfidence.LOW


class LocationResolver:
    """
    Usage:
        resolver = LocationResolver()
        result = resolver.resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
        if result.resolved:
            location = result.location
        elif result.status == LocationResolutionStatus.AMBIGUOUS:
            ...ask the user for institution/campus...
        else:
            ...LOCATION_NOT_RESOLVED -- do not generate a mission...
    """

    def __init__(
        self,
        *,
        geocoding_service=None,
        campus_resolver: CampusResolver | None = None,
        building_resolver: BuildingResolver | None = None,
        cache: LocationCache | None = None,
        footprint_service=_DEFAULT,
        verified_store=_DEFAULT,
    ) -> None:
        # Phase 15: provider chosen by settings.location_provider (Google or OpenStreetMap) -- see providers.py
        self._geocoding_service = geocoding_service or get_geocoding_service()
        self._source = source_for(self._geocoding_service)
        self._provider = provider_name(self._geocoding_service)
        self._campus_resolver = campus_resolver or CampusResolver(self._geocoding_service)
        self._building_resolver = building_resolver or BuildingResolver(self._geocoding_service)
        self._injected = geocoding_service is not None
        # Phase 15.3: the ORDER in which map providers are asked for the
        # building's one coordinate (settings.building_point_providers,
        # default "openstreetmap,google"). An injected building_resolver
        # (tests) keeps the original single-provider behaviour.
        self._building_lookups = (
            [(self._building_resolver, self._source, self._provider)]
            if (building_resolver is not None or geocoding_service is not None)
            else self._configured_building_lookups()
        )
        self._cache = cache if cache is not None else LocationCache()
        # Phase 15.1: OSM building outlines. Real Overpass only in the real
        # (non-injected) setup, so tests that inject a fake geocoder never
        # reach the network unless they pass a footprint service themselves.
        if footprint_service is _DEFAULT:
            # settings.building_footprint_provider, default "microsoft,openstreetmap" (footprint_sources.py)
            footprint_service = build_footprint_sources() if geocoding_service is None else None
        self._footprints = footprint_service
        # Phase 15.4: user-verified buildings (picked once on the map). Real
        # file only in the real setup; injected setups (tests) pass their own.
        if verified_store is _DEFAULT:
            verified_store = VerifiedBuildingStore() if geocoding_service is None else None
        self._verified = verified_store

    def resolve(
        self, *, institution: str | None = None, campus: str | None = None, building: str | None = None
    ) -> LocationResolutionResult:
        institution = (institution or "").strip() or None
        campus = (campus or "").strip() or None
        building = (building or "").strip() or None
        # This project's own examples use the same string for both
        # ("VIT Chennai" is both the institution and the campus) -- when
        # only one is given, the other defaults to it so downstream
        # matching/caching treats them as the one place the user meant.
        institution = institution or campus
        campus = campus or institution

        if building and not (institution or campus):
            return LocationResolutionResult(
                status=LocationResolutionStatus.AMBIGUOUS,
                message=(
                    f"{building!r} could not be uniquely identified. Please specify the institution or campus."
                ),
            )
        if not (institution or campus or building):
            return LocationResolutionResult(
                status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
                message="No institution, campus, or building was specified.",
            )

        cached = self._cache.get(institution, campus, building)
        if cached is not None:
            log.info("Location cache hit for institution=%r campus=%r building=%r", institution, campus, building)
            return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=cached)

        if building and self._verified is not None:
            record = self._verified.find(campus or institution, building)
            if record is not None:
                log.info("Building %r resolved from the user-verified list (picked on the map on %s)", building, record.get("selected_at"))
                return LocationResolutionResult(
                    status=LocationResolutionStatus.RESOLVED,
                    location=_location_from_verified(record, institution, campus, building),
                )

        try:
            query = _campus_query(institution, campus)
            campus_candidates = self._campus_resolver.resolve(query)
        except GeocodingError as exc:
            return self._fallback_after_geocoding_error(institution, campus, building, exc)

        # A city/area is never a campus -- never fly to its centre.
        vague = [c for c in campus_candidates if _too_vague(c)]
        campus_candidates = [c for c in campus_candidates if not _too_vague(c)]
        if vague and not campus_candidates:
            return LocationResolutionResult(
                status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
                message=(
                    f"{query!r} only matched a general area ({', '.join(f'{c.short_name} ({c.place_type})' for c in vague[:3])}), "
                    f"not a campus -- give the full institution name and city, e.g. \"VIT Chennai\"."
                ),
            )
        if not campus_candidates:
            return LocationResolutionResult(
                status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
                message=f"Could not resolve institution/campus {campus or institution!r} via {self._provider}.",
            )
        campus_match = campus_candidates[0]

        if building is None:
            location = self._build_resolved_location(
                institution=institution, campus=campus, building=None,
                candidate=campus_match, source=self._source,
                institution_match=bool(institution), campus_match_flag=bool(campus),
                building_match=True, campus_relationship=True,  # vacuously true -- no building was requested, see module docstring
            )
            self._cache.set(institution, campus, building, location)
            return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=location)

        warnings: list[str] = []
        seen_candidates: list[dict] = []
        building_candidate = None
        building_source = self._source
        errors: list[GeocodingError] = []
        for resolver, source, name in self._building_lookups:
            try:
                building_candidate = resolver.resolve_via_openstreetmap(building, campus_match)
            except AmbiguousBuildingError as exc:
                # Phase 15: several DIFFERENT places inside the campus match the
                # building name -- never pick one silently.
                return LocationResolutionResult(
                    status=LocationResolutionStatus.AMBIGUOUS,
                    message=f"{building!r} matches more than one place at {campus or institution!r}: {exc.names()}. Please be more specific.",
                    details={"candidates": [c.as_dict() for c in exc.candidates]},
                )
            except GeocodingError as exc:
                errors.append(exc)
                warnings.append(f"{name} building search failed: {exc}")
                continue
            finally:
                seen_candidates += [dict(c, provider=name) for c in getattr(resolver, "last_seen", [])]
            if building_candidate is not None:
                building_source = source
                log.info("Building %r found via %s at (%.6f, %.6f)", building, name,
                         building_candidate.latitude, building_candidate.longitude)
                break
        if building_candidate is None and errors and len(errors) == len(self._building_lookups):
            return self._fallback_after_geocoding_error(institution, campus, building, errors[-1])

        seen = {"provider_candidates": seen_candidates}

        if building_candidate is not None:
            in_campus = campus_match.bounding_box is not None and point_in_bbox(
                building_candidate.latitude, building_candidate.longitude, campus_match.bounding_box
            )
            outline = None
            if building_candidate.geometry is None:
                # Phase 15.1: one known point of the building -> the real outline containing it.
                outline = self._outline_for_point(building_candidate.latitude, building_candidate.longitude, warnings)
                if outline is not None:
                    building_candidate.geometry, building_candidate.geometry_parts = _largest_first(outline[0].rings)
            location = self._build_resolved_location(
                institution=institution, campus=campus, building=building,
                candidate=building_candidate, source=building_source,
                institution_match=bool(institution), campus_match_flag=bool(campus),
                building_match=True, campus_relationship=in_campus,
            )
            if outline is not None:
                _apply_outline(location, outline)
            self._cache.set(institution, campus, building, location)
            return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=location, warnings=warnings, details=seen)

        # Phase 15.1: the provider has no place for this building -- look for a
        # mapped OSM building inside the campus whose OWN name tags match it.
        named = self._named_footprint(building, campus_match, warnings)
        if isinstance(named, LocationResolutionResult):
            return named
        if named is not None:
            clat, clon = polygon_centroid([p for ring in named.rings for p in ring])
            geometry, parts = _largest_first(named.rings)
            osm_candidate = GeocodingCandidate(
                display_name=f"{named.label} (OpenStreetMap {named.osm_id})", latitude=clat, longitude=clon,
                bounding_box=None, importance=None, osm_type=named.osm_id.split("/")[0], osm_id=None,
                category="building", place_type=named.tags.get("building"), geometry=geometry,
                name=named.label, geometry_parts=parts,
            )
            in_campus = campus_match.bounding_box is not None and point_in_bbox(clat, clon, campus_match.bounding_box)
            location = self._build_resolved_location(
                institution=institution, campus=campus, building=building,
                candidate=osm_candidate, source=LocationSource.OPENSTREETMAP,
                institution_match=bool(institution), campus_match_flag=bool(campus),
                building_match=True, campus_relationship=in_campus,
            )
            location.geometry_source = "OPENSTREETMAP"
            location.geometry_meta = footprint_meta(named)
            location.geometry_note = f"Recognized by its OpenStreetMap name tags ({named.names()})."
            self._cache.set(institution, campus, building, location)
            return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=location, warnings=warnings, details=seen)

        # Priority #2/#3/#4: existing RAG / project building knowledge (see building_resolver.py).
        rag_hit = self._building_resolver.resolve_via_rag(building, campus)
        if rag_hit is not None:
            lat, lon, source_name = rag_hit
            match = MatchDetails(
                institution_match=bool(institution), campus_match=bool(campus), building_match=True,
                coordinates_valid=_valid_coordinate(lat, lon), campus_relationship=False, geometry_available=False,
                source_reliable=False,
            )
            outline = self._outline_for_point(lat, lon, warnings)  # Phase 15.1: one coordinate -> real boundary
            geometry, parts = _largest_first(outline[0].rings) if outline else (None, None)
            match.geometry_available = outline is not None
            location = ResolvedLocation(
                institution=institution, campus=campus, building=building,
                latitude=lat, longitude=lon,
                resolution_type=ResolutionType.BUILDING if outline else ResolutionType.POINT,
                source=LocationSource.RAG_KNOWLEDGE, confidence=_compute_confidence(match, LocationSource.RAG_KNOWLEDGE),
                geometry_available=outline is not None, geometry=geometry, geometry_parts=parts,
                raw_source_name=source_name, match_details=match,
            )
            if outline:
                _apply_outline(location, outline)
            self._cache.set(institution, campus, building, location)
            return LocationResolutionResult(
                status=LocationResolutionStatus.RESOLVED, location=location,
                warnings=[f"Resolved from this project's own RAG knowledge base, not {self._provider} -- verify independently."] + warnings,
                details=seen,
            )

        # Nothing worked -- NEVER fall back to the campus centroid for a
        # requested building (explicit spec requirement).
        return LocationResolutionResult(
            status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
            message=(
                f"{building!r} could not be reliably resolved within {campus or institution!r} via {self._provider} "
                f"or this project's own knowledge base."
            ),
            details={"campus_resolved": campus_match.as_dict(), **seen},
            warnings=warnings,
        )

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def resolve_address(self, address: str) -> LocationResolutionResult:
        """
        Phase 15.5 -- a street address / landmark / named place ("12 Anna
        Salai, Chennai", "Chennai Central railway station") as ONE lookup,
        instead of the campus -> building hierarchy. Rules:
          - a result that only names a whole city/area/street is too vague
            for an inspection -> LOCATION_NOT_RESOLVED (never its centre);
          - clearly different places (> address_ambiguity_distance_m apart)
            -> AMBIGUOUS, never a silent pick;
          - the real OpenStreetMap outline containing the point becomes the
            boundary when one is mapped; otherwise it stays a point.
        """
        address = (address or "").strip()
        if not address:
            return LocationResolutionResult(status=LocationResolutionStatus.LOCATION_NOT_RESOLVED, message="No address given.")
        cache_key = f"address: {address}"
        cached = self._cache.get(None, None, cache_key)
        if cached is not None:
            return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=cached)

        warnings: list[str] = []
        seen: list[dict] = []
        errors: list[GeocodingError] = []
        vague: list[str] = []
        for service in self._address_services():
            name = provider_name(service)
            try:
                candidates = service.search(address, limit=5)
            except GeocodingError as exc:
                errors.append(exc)
                warnings.append(f"{name} address search failed: {exc}")
                continue
            precise = []
            for c in candidates:
                too_vague = _too_vague(c)
                seen.append({"query": address, "name": c.display_name, "latitude": c.latitude, "longitude": c.longitude,
                             "accepted": not too_vague, "provider": name,
                             "reason": f"too general ({c.place_type})" if too_vague else "specific place"})
                log.info("Address candidate %r -> %s", c.display_name, f"rejected (too general: {c.place_type})" if too_vague else "ok")
                if too_vague:
                    vague.append(f"{c.short_name} ({c.place_type})")
                else:
                    precise.append(c)
            if not precise:
                continue
            far_apart = [precise[0]] + [
                c for c in precise[1:]
                if haversine_km(c.latitude, c.longitude, precise[0].latitude, precise[0].longitude) * 1000.0 > settings.address_ambiguity_distance_m
            ]
            if len(far_apart) > 1:
                return LocationResolutionResult(
                    status=LocationResolutionStatus.AMBIGUOUS,
                    message=f"{address!r} matches different places: "
                            + "; ".join(f"{c.display_name}" for c in far_apart[:4]) + ". Please add the area or city.",
                    details={"provider_candidates": seen},
                )
            best = precise[0]
            outline = self._outline_for_point(best.latitude, best.longitude, warnings) if best.geometry is None else None
            geometry, parts = (_largest_first(outline[0].rings) if outline else (best.geometry, best.geometry_parts))
            has_outline = geometry is not None
            source = source_for(service)
            match = MatchDetails(institution_match=False, campus_match=False, building_match=True, coordinates_valid=True,
                                 campus_relationship=False, geometry_available=has_outline, source_reliable=True)
            location = ResolvedLocation(
                institution=None, campus=None, building=best.short_name or address,
                latitude=best.latitude, longitude=best.longitude,
                resolution_type=ResolutionType.BUILDING if has_outline else ResolutionType.POINT,
                source=source,
                # A specific place from a real map provider: HIGH with its real
                # outline, MEDIUM when only the point is known.
                confidence=LocationConfidence.HIGH if has_outline else LocationConfidence.MEDIUM,
                geometry_available=has_outline, geometry=geometry, geometry_parts=parts,
                raw_source_name=best.display_name, match_details=match,
                place_id=best.place_id, formatted_address=best.formatted_address or best.display_name,
            )
            if outline:
                _apply_outline(location, outline)
            self._cache.set(None, None, cache_key, location)
            return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=location,
                                            warnings=warnings, details={"provider_candidates": seen})

        if errors and len(errors) == len(self._address_services()):
            return LocationResolutionResult(
                status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
                message=f"Map providers were unavailable for {address!r}: {errors[-1]} (no cached result).",
                warnings=warnings, details={"provider_candidates": seen},
            )
        message = (
            f"{address!r} only matches general areas ({', '.join(vague[:3])}) -- add a building name, street number or landmark."
            if vague else f"{address!r} was not found on any map."
        )
        return LocationResolutionResult(status=LocationResolutionStatus.LOCATION_NOT_RESOLVED, message=message,
                                        warnings=warnings, details={"provider_candidates": seen})

    def resolve_road_anchor(self, query: str) -> LocationResolutionResult:
        """
        Road scan: one point on or near the road (or the landmark) named in
        `query`. Unlike resolve_address, a road or street result is ACCEPTED --
        the road itself is what is being scanned, so its mapped point is the
        anchor. Whole cities/states (boundary results) are still refused.
        The result has no building; the road is picked from the map later.
        """
        query = (query or "").strip()
        if not query:
            return LocationResolutionResult(status=LocationResolutionStatus.LOCATION_NOT_RESOLVED, message="No place given.")
        errors: list[GeocodingError] = []
        for service in self._address_services():
            try:
                candidates = service.search(query, limit=5)
            except GeocodingError as exc:
                errors.append(exc)
                continue
            for c in candidates:
                if (c.category or "").lower() == "boundary":
                    continue
                location = ResolvedLocation(
                    institution=None, campus=None, building=None,
                    latitude=c.latitude, longitude=c.longitude,
                    resolution_type=ResolutionType.POINT,
                    source=source_for(service),
                    confidence=LocationConfidence.MEDIUM,
                    geometry_available=False, geometry=None,
                    raw_source_name=c.display_name,
                    match_details=MatchDetails(institution_match=False, campus_match=False, building_match=False,
                                               coordinates_valid=True, campus_relationship=False,
                                               geometry_available=False, source_reliable=True),
                    place_id=c.place_id, formatted_address=c.formatted_address or c.display_name,
                )
                return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=location)
        if errors and len(errors) == len(self._address_services()):
            return LocationResolutionResult(status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
                                            message=f"Map providers were unavailable for {query!r}: {errors[-1]}.")
        return LocationResolutionResult(status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
                                        message=f"{query!r} was not found on any map.")

    def _address_services(self):
        """Primary provider first (Google when configured), then OpenStreetMap -- injected setups use only the injected one."""
        if getattr(self, "_injected", False):
            return [self._geocoding_service]
        services = [self._geocoding_service]
        if source_for(self._geocoding_service) != LocationSource.OPENSTREETMAP:
            from app.location.geocoding_service import NominatimGeocodingService

            services.append(NominatimGeocodingService())
        return services

    def resolve_from_map_pick(
        self,
        *,
        institution: str | None,
        campus: str | None,
        building: str,
        latitude: float,
        longitude: float,
        campus_bbox: tuple[float, float, float, float] | None,
    ) -> LocationResolutionResult:
        """
        Phase 15.4 -- the user clicked the building on the map. The click is
        the one coordinate; the real OpenStreetMap outline containing it (or
        snapped to it) is the boundary. Saved as verified, so later requests
        resolve automatically. A click outside the campus is refused.
        """
        if not _valid_coordinate(latitude, longitude):
            return LocationResolutionResult(status=LocationResolutionStatus.LOCATION_NOT_RESOLVED, message="Invalid click coordinate.")
        if campus_bbox is not None and not point_in_bbox(latitude, longitude, campus_bbox):
            return LocationResolutionResult(
                status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
                message=f"That point is outside {campus or institution!r} -- click the building inside the campus.",
            )
        warnings: list[str] = []
        outline = self._outline_for_point(latitude, longitude, warnings)
        if outline is not None:
            footprint, note = outline
            geometry, parts = _largest_first(footprint.rings)
            clat, clon = polygon_centroid([p for ring in footprint.rings for p in ring])
            note = (f"Selected on the map by the user; outline from {SOURCE_LABELS.get(footprint.source, footprint.source)} "
                    f"({footprint.osm_id}). {note or ''}").strip()
            g_source, g_meta = footprint.source, footprint_meta(footprint)
        else:
            geometry, parts, clat, clon = None, None, latitude, longitude
            note = "Selected on the map by the user; no mapped outline at that point (kept as a point)."
            g_source, g_meta = None, None
        record = {
            "institution": institution, "campus": campus or institution, "building": building,
            "latitude": clat, "longitude": clon, "clicked": [latitude, longitude],
            "geometry": geometry, "geometry_parts": parts, "geometry_note": note,
            "geometry_source": g_source, "geometry_meta": g_meta,
        }
        if self._verified is not None:
            self._verified.save(record)
        location = _location_from_verified(dict(record, selected_at=datetime.now(timezone.utc).isoformat()), institution, campus, building)
        location.bounding_box = campus_bbox
        return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=location, warnings=warnings)

    def _configured_building_lookups(self):
        """[(BuildingResolver, LocationSource, provider name)] in settings.building_point_providers order."""
        from app.location.geocoding_service import NominatimGeocodingService

        lookups = []
        for name in [p.strip().lower() for p in settings.building_point_providers.split(",") if p.strip()]:
            if name in ("openstreetmap", "osm", "nominatim"):
                service = self._geocoding_service if isinstance(self._geocoding_service, NominatimGeocodingService) else NominatimGeocodingService()
            elif name == "google":
                if not settings.google_maps_api_key:
                    continue
                service = self._geocoding_service if source_for(self._geocoding_service) == LocationSource.GOOGLE_PLACES else get_geocoding_service("google")
            else:
                log.warning("Unknown building point provider %r ignored.", name)
                continue
            resolver = self._building_resolver if service is self._geocoding_service else BuildingResolver(service)
            lookups.append((resolver, source_for(service), provider_name(service)))
        return lookups or [(self._building_resolver, self._source, self._provider)]

    def neighbouring_obstacles(self, location: ResolvedLocation, search_radius_m: float):
        """
        Boundary fix: (obstacles, warning) -- the other mapped buildings
        around `location` (see app/location/obstacles.py). obstacles is None
        when the lookup could not be done.
        """
        from app.location.obstacles import fetch_neighbour_obstacles

        return fetch_neighbour_obstacles(self._footprints, location, search_radius_m)

    def _outline_for_point(self, lat: float, lon: float, warnings: list[str]):
        """
        (footprint, note) for the building outline containing this point (or
        snapped to it), from the first configured source that has one
        (default: Microsoft GlobalMLBuildingFootprints, then OpenStreetMap).
        None when no source has an outline at this point -- the point is
        then kept as a point; no boundary is invented.
        """
        sources = as_sources(self._footprints)
        if not sources:
            return None
        misses = []
        for name, service in sources:
            label = SOURCE_LABELS.get(name.upper(), name)
            try:
                footprints = service.footprints_near(lat, lon, radius_m=settings.footprint_snap_distance_m + 150.0)
            except GeocodingError as exc:
                warnings.append(f"Building outline lookup ({label}) failed: {exc}")
                continue
            low = [f for f in footprints if _below_min_confidence(f)]
            footprints = [f for f in footprints if not _below_min_confidence(f)]
            hit = footprint_for_point(footprints, lat, lon)
            if hit is None:
                misses.append(label + (f" ({len(low)} low-confidence outline(s) ignored)" if low else ""))
                continue
            footprint, snap_warning = hit
            if snap_warning:
                warnings.append(snap_warning)
            source_label = SOURCE_LABELS.get(footprint.source, label)
            meta = footprint_meta(footprint)
            extra = f", ML confidence {meta['confidence']:.2f}" if meta.get("confidence") is not None else ""
            note = (snap_warning or f"{source_label} outline {footprint.osm_id} contains the building's point") + extra + "."
            log.info("Building outline: %s (%s)", footprint.osm_id, note)
            return footprint, note
        warnings.append(
            "No mapped building outline contains this point (" + "; ".join(misses or ["no source answered"])
            + ") -- treated as a POINT (no boundary invented)."
        )
        return None

    def _named_footprint(self, building: str, campus_match: GeocodingCandidate, warnings: list[str]):
        """Footprint inside the campus whose own name tags match (OpenStreetMap only -- ML footprints are unnamed)."""
        service = self._footprints.named_search_service() if isinstance(self._footprints, FootprintSources) else self._footprints
        if service is None or campus_match.bounding_box is None:
            return None
        try:
            footprints = service.footprints_in_bbox(campus_match.bounding_box)
        except GeocodingError as exc:
            warnings.append(f"OpenStreetMap building search failed: {exc}")
            return None
        try:
            return find_named_footprint(footprints, building_name_variants(building))
        except AmbiguousFootprintError as exc:
            return LocationResolutionResult(
                status=LocationResolutionStatus.AMBIGUOUS,
                message=f"{building!r} matches several mapped buildings: {exc}. Please be more specific.",
            )

    def _fallback_after_geocoding_error(
        self, institution: str | None, campus: str | None, building: str | None, exc: GeocodingError
    ) -> LocationResolutionResult:
        """
        Spec CASE 3: "If OpenStreetMap/Nominatim is unavailable -> check
        verified cache -> if a verified cached location exists, use it
        -> otherwise LOCATION_NOT_RESOLVED. Never use guessed
        coordinates." The cache was already checked once for the exact
        requested key at the top of `resolve()` (a hit there returns
        immediately and never reaches this method) -- this is a second
        check, so a location that got cached by an EARLIER, successful
        call is still usable now even if this particular call's network
        request failed partway through (e.g. the campus resolved from
        cache-free real-time state but the building lookup's own
        network call then failed).
        """
        log.warning("Geocoding failed (%s) -- checking cache before giving up.", exc)
        cached = self._cache.get(institution, campus, building)
        if cached is not None:
            return LocationResolutionResult(
                status=LocationResolutionStatus.RESOLVED, location=cached,
                warnings=[f"{self._provider} was unavailable ({exc}) -- served from cache instead."],
            )
        return LocationResolutionResult(
            status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
            message=f"{self._provider} was unavailable ({exc}) and no verified cached location exists.",
        )

    @staticmethod
    def _build_resolved_location(
        *,
        institution: str | None,
        campus: str | None,
        building: str | None,
        candidate: GeocodingCandidate,
        source: LocationSource,
        institution_match: bool,
        campus_match_flag: bool,
        building_match: bool,
        campus_relationship: bool,
    ) -> ResolvedLocation:
        geometry_available = candidate.geometry is not None
        match = MatchDetails(
            institution_match=institution_match,
            campus_match=campus_match_flag,
            building_match=building_match,
            coordinates_valid=_valid_coordinate(candidate.latitude, candidate.longitude),
            campus_relationship=campus_relationship,
            geometry_available=geometry_available,
            source_reliable=(source in (LocationSource.OPENSTREETMAP, LocationSource.GOOGLE_PLACES)),
        )
        return ResolvedLocation(
            institution=institution,
            campus=campus,
            building=building,
            latitude=candidate.latitude,
            longitude=candidate.longitude,
            # Per spec section 5: BUILDING iff an actual footprint/polygon
            # is available, POINT otherwise -- regardless of whether this
            # was a campus-only or a specific-building resolution.
            resolution_type=ResolutionType.BUILDING if geometry_available else ResolutionType.POINT,
            source=source,
            confidence=_compute_confidence(match, source),
            geometry_available=geometry_available,
            geometry=candidate.geometry,
            bounding_box=candidate.bounding_box,
            raw_source_name=candidate.display_name,
            match_details=match,
            place_id=getattr(candidate, "place_id", None),
            formatted_address=getattr(candidate, "formatted_address", None),
            geometry_parts=getattr(candidate, "geometry_parts", None),
        )


def _demo_cli() -> None:
    """
    Manual test entry point -- run this on a machine with real internet
    access (see geocoding_service.py's docstring: not verified from
    inside this sandbox):
        python -m app.location.location_resolver "VIT Chennai" AB1
        python -m app.location.location_resolver "VIT Chennai"
    """
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    args = sys.argv[1:]
    campus_name = args[0] if args else "VIT Chennai"
    building_name = args[1] if len(args) > 1 else None

    resolver = LocationResolver()
    result = resolver.resolve(institution=campus_name, campus=campus_name, building=building_name)
    print(json.dumps(result.as_dict(), indent=2, default=str))


if __name__ == "__main__":
    _demo_cli()
