"""
Phase 15 -- Google Maps Platform as a geocoding PROVIDER for the existing
LocationResolver.

This is deliberately NOT a second location resolver. It implements the
exact same `search(query, *, limit, viewbox, bounded, want_polygon)`
interface as `NominatimGeocodingService` (earlier improvement), returns
the same `GeocodingCandidate` objects, and raises the same
`GeocodingError` subclasses -- so `CampusResolver`, `BuildingResolver`,
`LocationResolver`, the cache, confidence scoring and every safety check
work unchanged. The earlier spec asked for exactly this seam ("keep the
new location functionality independent enough that another map provider
can be added later").

APIs used (current as of this phase -- checked against Google's docs):
- Places API (New) Text Search:
    POST https://places.googleapis.com/v1/places:searchText
    headers X-Goog-Api-Key, X-Goog-FieldMask; body textQuery, pageSize,
    locationRestriction/locationBias (rectangle low/high), regionCode.
  The deprecated legacy Places API is not used.
- Geocoding API v4 SearchDestinations (building outlines):
    POST https://geocode.googleapis.com/v4/geocode/destinations
    body {"place": "places/<PLACE_ID>"}; a building outline is
    `destinations[].primary.displayPolygon` (GeoJSON, RFC 7946, [lng, lat])
    when `primary.structureType == "BUILDING"`. This replaced the
    deprecated v3 `extra_computations=BUILDING_AND_ENTRANCES` experiment.

Honest limits, stated plainly:
- Google often knows a campus but NOT an internal building code like
  "AB1". A Google result is therefore never accepted just because it was
  returned: BuildingResolver additionally requires a NAME match and that
  the result lies inside the campus (see building_resolver.py).
- Building outlines are not available for every building/region. When
  SearchDestinations has no BUILDING polygon (or the API isn't enabled on
  the key), the result stays a POINT with geometry=None. A polygon is
  never invented from a point.
- Outlines are only requested for campus-scoped building searches (a
  `viewbox` is given) and only for the top few candidates -- each call is
  billed, and a campus outline is not needed.
- Caching: Google's Service Specific Terms allow latitude/longitude to be
  cached for up to 30 consecutive days, and place IDs indefinitely. The
  existing LocationCache TTL default (`location_cache_ttl_hours` = 720 h
  = 30 days) already satisfies that -- do not raise it above 720 while
  using Google.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.location.geocoding_service import (
    GeocodingAuthError,
    GeocodingCandidate,
    GeocodingConnectionError,
    GeocodingError,
    GeocodingInvalidResponseError,
    GeocodingRateLimitedError,
    GeocodingTimeoutError,
)

log = logging.getLogger(__name__)

PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DESTINATIONS_URL = "https://geocode.googleapis.com/v4/geocode/destinations"
_PLACES_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.viewport,places.types,places.primaryType"
)
_DESTINATIONS_FIELD_MASK = "destinations.primary"
_MAX_OUTLINE_LOOKUPS = 3


def _valid_coordinate(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def _rings_from_geojson(geojson: dict | None) -> list[list[tuple[float, float]]]:
    """GeoJSON Polygon/MultiPolygon -> outer rings as (lat, lon) lists. Anything else -> [] (no footprint)."""
    if not isinstance(geojson, dict):
        return []
    gtype, coords = geojson.get("type"), geojson.get("coordinates")
    try:
        if gtype == "Polygon":
            polygons = [coords]
        elif gtype == "MultiPolygon":
            polygons = coords
        else:
            return []
        rings = []
        for polygon in polygons:
            ring = [(float(lat), float(lon)) for lon, lat in polygon[0]]
            if len(ring) >= 3 and all(_valid_coordinate(a, b) for a, b in ring):
                rings.append(ring)
        return rings
    except (IndexError, TypeError, ValueError):
        return []


def _ring_area(ring: list[tuple[float, float]]) -> float:
    """Planar shoelace area in degree^2 -- only used to pick the LARGEST ring, so units don't matter."""
    total = 0.0
    for (y1, x1), (y2, x2) in zip(ring, ring[1:] + ring[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _parse_place(raw: dict, rank: int) -> GeocodingCandidate | None:
    """One Places (New) result -> GeocodingCandidate; None for a malformed/invalid-coordinate entry."""
    try:
        lat = float(raw["location"]["latitude"])
        lon = float(raw["location"]["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not _valid_coordinate(lat, lon):
        return None

    bbox = None
    viewport = raw.get("viewport")
    if isinstance(viewport, dict):
        try:
            low, high = viewport["low"], viewport["high"]
            bbox = (float(low["latitude"]), float(high["latitude"]), float(low["longitude"]), float(high["longitude"]))
        except (KeyError, TypeError, ValueError):
            bbox = None

    name = (raw.get("displayName") or {}).get("text") or ""
    address = raw.get("formattedAddress") or ""
    types = raw.get("types") or []
    return GeocodingCandidate(
        display_name=f"{name}, {address}" if name and address else (name or address),
        latitude=lat,
        longitude=lon,
        bounding_box=bbox,
        # Google exposes no relevance score; its own result ORDER is its
        # relevance ranking, so rank becomes a 0..1 score (first = 1.0).
        importance=1.0 / (rank + 1),
        osm_type=None,
        osm_id=None,
        category="google_place",
        place_type=raw.get("primaryType") or (types[0] if types else None),
        geometry=None,
        place_id=raw.get("id"),
        name=name or None,
        formatted_address=address or None,
    )


class GooglePlacesGeocodingService:
    """
    Usage (identical to NominatimGeocodingService):
        service = GooglePlacesGeocodingService()
        campus = service.search("VIT Chennai")
        buildings = service.search("AB1", viewbox=campus[0].bounding_box, bounded=True, want_polygon=True)
    """

    provider_name = "Google Places"
    source_name = "GOOGLE_PLACES"
    strict_viewbox = True  # locationRestriction is a hard limit -- see BuildingResolver

    def __init__(self, *, api_key: str | None = None, timeout_s: float | None = None, region_code: str | None = None, session=None) -> None:
        self.api_key = settings.google_maps_api_key if api_key is None else api_key
        self.timeout_s = settings.google_maps_timeout_s if timeout_s is None else timeout_s
        self.region_code = settings.google_maps_region_code if region_code is None else region_code
        self._session = session  # injectable for tests; real requests.Session created lazily

    def _get_session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    # -----------------------------------------------------------------
    # HTTP + error mapping (shared by both endpoints)
    # -----------------------------------------------------------------

    def _post(self, url: str, body: dict, field_mask: str, what: str) -> dict:
        if not self.api_key:
            raise GeocodingAuthError(
                "GOOGLE_MAPS_API_KEY is not set -- add it to .env (server key with Places API (New) + "
                "Geocoding API enabled), or set LOCATION_PROVIDER=openstreetmap."
            )
        import requests

        headers = {"Content-Type": "application/json", "X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": field_mask}
        try:
            response = self._get_session().post(url, json=body, headers=headers, timeout=self.timeout_s)
        except requests.exceptions.Timeout as exc:
            raise GeocodingTimeoutError(f"Google {what} timed out after {self.timeout_s}s") from exc
        except requests.exceptions.ConnectionError as exc:
            raise GeocodingConnectionError(f"Could not reach Google {what}: {exc}") from exc
        except requests.exceptions.RequestException as exc:  # noqa: BLE001
            raise GeocodingConnectionError(f"Google {what} request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code != 200:
            error = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
            status = error.get("status", "")
            reasons = {d.get("reason") for d in error.get("details", []) if isinstance(d, dict)}
            message = error.get("message") or (response.text or "")[:200]
            if response.status_code in (401, 403) or "API_KEY_INVALID" in reasons or status in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
                raise GeocodingAuthError(f"Google {what} rejected the API key ({response.status_code} {status}): {message}")
            if response.status_code == 429 or status == "RESOURCE_EXHAUSTED":
                raise GeocodingRateLimitedError(f"Google {what} quota/rate limit exceeded: {message}")
            if response.status_code >= 500:
                raise GeocodingConnectionError(f"Google {what} unavailable (HTTP {response.status_code}): {message}")
            raise GeocodingInvalidResponseError(f"Google {what} returned HTTP {response.status_code} {status}: {message}")

        if not isinstance(payload, dict):
            raise GeocodingInvalidResponseError(f"Google {what} response was not a JSON object.")
        return payload

    # -----------------------------------------------------------------
    # Public interface -- same as NominatimGeocodingService.search()
    # -----------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        viewbox: tuple[float, float, float, float] | None = None,
        bounded: bool = False,
        want_polygon: bool = False,
    ) -> list[GeocodingCandidate]:
        """`viewbox` is (south, north, west, east), this project's convention. `bounded=True` -> hard restriction."""
        body: dict = {"textQuery": query, "pageSize": max(1, min(int(limit), 20))}
        if viewbox is not None:
            south, north, west, east = viewbox
            rectangle = {"rectangle": {"low": {"latitude": south, "longitude": west}, "high": {"latitude": north, "longitude": east}}}
            body["locationRestriction" if bounded else "locationBias"] = rectangle
        if self.region_code:
            body["regionCode"] = self.region_code

        payload = self._post(PLACES_TEXT_SEARCH_URL, body, _PLACES_FIELD_MASK, "Places Text Search")
        places = payload.get("places", [])  # absent key == zero results (not an error)
        if not isinstance(places, list):
            raise GeocodingInvalidResponseError("Google Places response 'places' was not a list.")
        candidates = [c for i, raw in enumerate(places) if isinstance(raw, dict) and (c := _parse_place(raw, i)) is not None]
        log.info("Google Places search %r -> %d candidate(s) (of %d raw)", query, len(candidates), len(places))

        if want_polygon and viewbox is not None:
            for candidate in candidates[:_MAX_OUTLINE_LOOKUPS]:
                self._attach_building_outline(candidate)
        return candidates

    def _attach_building_outline(self, candidate: GeocodingCandidate) -> None:
        """Adds a REAL building outline from Geocoding v4 if Google has one; otherwise leaves the candidate a point."""
        if not candidate.place_id:
            return
        try:
            payload = self._post(DESTINATIONS_URL, {"place": f"places/{candidate.place_id}"}, _DESTINATIONS_FIELD_MASK, "Geocoding destinations")
        except GeocodingError as exc:
            # An outline is an enhancement, not a requirement: the point stays valid.
            log.warning("No building outline for %r (%s) -- keeping it as a POINT.", candidate.short_name, exc)
            return
        for destination in payload.get("destinations", []) or []:
            primary = (destination or {}).get("primary") or {}
            if primary.get("structureType") != "BUILDING":
                continue
            rings = _rings_from_geojson(primary.get("displayPolygon"))
            if rings:
                rings.sort(key=_ring_area, reverse=True)
                candidate.geometry = rings[0]
                candidate.geometry_parts = rings if len(rings) > 1 else None
                return


def _demo_cli() -> None:
    """
    Manual check on YOUR machine (needs GOOGLE_MAPS_API_KEY in .env):
        python -m app.location.google_places_service "VIT Chennai"
    """
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    query = " ".join(sys.argv[1:]) or "VIT Chennai"
    try:
        candidates = GooglePlacesGeocodingService().search(query)
    except GeocodingError as exc:
        print(f"Google lookup failed: {exc}")
        raise SystemExit(1)
    print(f"Query: {query!r} -> {len(candidates)} candidate(s)\n")
    for c in candidates:
        print(json.dumps(c.as_dict(), indent=2))


if __name__ == "__main__":
    _demo_cli()
