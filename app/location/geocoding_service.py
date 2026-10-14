"""
Real-world coordinate & building resolution improvement -- geocoding.

    Use OpenStreetMap data through an appropriate API such as Nominatim
    for real-world geocoding... Do NOT hard-code VIT Chennai coordinates.
    Do NOT hard-code AB1 coordinates. The coordinates must come from the
    resolution process.

This is the ONLY file in this project that talks to an HTTP geocoding
API directly -- same "one file owns the external dependency" shape as
`app/llm/llm_client.py` (the only file that talks to Groq/Ollama) and
`app/mavlink/px4_client.py` (the only file that talks to MAVSDK). Every
caller (`campus_resolver.py`, `building_resolver.py`) works with this
module's own `GeocodingCandidate` objects, never raw Nominatim JSON.

`GeocodingService` is an abstract-by-convention interface (duck-typed,
matching this project's existing style of "one obvious implementation
today, swappable via constructor injection" rather than a formal ABC --
see `AdaptiveMissionReplanner`/`RAMROrchestrator`'s own constructor
patterns) -- `LocationResolver` accepts one via dependency injection so
tests can supply a fake instead of hitting the real network, the same
"fake at the boundary" pattern this project already uses for MAVSDK
(`tests/test_phase_orchestrator.py`) and the LLM client
(`tests/test_phase5_generator_integration.py`).

KNOWN LIMITATION, stated plainly: this class has NOT been exercised
against the real Nominatim service in this sandbox -- outbound HTTPS
here only reaches an allowlisted set of hosts, and
`nominatim.openstreetmap.org` is not on it (verified: a direct `curl`
to it from this sandbox gets a proxy-level 403, the same kind of network
restriction Phase 9/10's PX4 SITL work has always had). The HTTP
request/response handling below follows Nominatim's actual documented
API (https://nominatim.org/release-docs/latest/api/Search/) faithfully,
and every test in `tests/test_phase_location_resolution.py` exercises it
against a FAKE HTTP layer (monkeypatched `requests.Session`) proving the
request is built correctly and every response/error shape is handled --
but, exactly like Phase 9's PX4 SITL gap, real connectivity has not been
verified here. Run `python -m app.location.geocoding_service "VIT
Chennai"` on a machine with real internet access to confirm it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.config import settings

log = logging.getLogger(__name__)


class GeocodingError(Exception):
    """Base class for every geocoding failure -- callers can catch this alone if they don't need to distinguish."""


class GeocodingTimeoutError(GeocodingError):
    pass


class GeocodingConnectionError(GeocodingError):
    pass


class GeocodingRateLimitedError(GeocodingError):
    pass


class GeocodingInvalidResponseError(GeocodingError):
    """The HTTP call succeeded but the body wasn't parseable JSON, or wasn't shaped like a Nominatim response."""


class GeocodingAuthError(GeocodingError):
    """Phase 15 -- the provider rejected the credentials (missing/invalid/unauthorized API key)."""


@dataclass
class GeocodingCandidate:
    """
    One normalized search result -- this module's own shape, not raw
    Nominatim JSON. `latitude`/`longitude` are already validated as real
    numbers in range (an out-of-range or non-numeric value from the API
    raises `GeocodingInvalidResponseError` instead of ever reaching this
    dataclass -- see `_parse_result()` below).
    """

    display_name: str
    latitude: float
    longitude: float
    # (south, north, west, east) -- Nominatim's own `boundingbox` order,
    # converted to floats.
    bounding_box: tuple[float, float, float, float] | None
    importance: float | None  # Nominatim's own relevance score, 0..1 roughly -- higher is more confident
    osm_type: str | None
    osm_id: int | None
    category: str | None      # Nominatim's "class" field (e.g. "amenity", "building")
    place_type: str | None    # Nominatim's "type" field (e.g. "university", "yes")
    # Polygon outline as [(lat, lon), ...], only when the caller asked
    # for `want_polygon=True` AND Nominatim actually returned one for
    # this result (not every OSM feature has one) -- None otherwise.
    geometry: list[tuple[float, float]] | None = None
    # Phase 15 additions (optional; Nominatim leaves them None):
    place_id: str | None = None                                   # Google place ID
    name: str | None = None                                       # the place's own name, without its address
    formatted_address: str | None = None
    geometry_parts: list[list[tuple[float, float]]] | None = None  # every outer ring of a MultiPolygon

    @property
    def short_name(self) -> str:
        """The place's own name: `name` when the provider gives one, else the first segment of display_name."""
        return self.name or self.display_name.split(",")[0].strip()

    def as_dict(self) -> dict:
        return {
            "place_id": self.place_id,
            "name": self.short_name,
            "display_name": self.display_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "bounding_box": self.bounding_box,
            "importance": self.importance,
            "osm_type": self.osm_type,
            "osm_id": self.osm_id,
            "category": self.category,
            "place_type": self.place_type,
            "geometry_available": self.geometry is not None,
        }


def _valid_coordinate(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def _parse_polygon(geojson: dict | None) -> list[tuple[float, float]] | None:
    """
    Nominatim's `polygon_geojson=1` returns a GeoJSON geometry whose
    `coordinates` are [lon, lat] pairs (GeoJSON's own axis order, the
    opposite of this project's lat/lon convention everywhere else --
    flipped here, once, so nothing downstream has to remember that).
    Only Polygon/MultiPolygon outer rings are extracted; anything else
    (Point, LineString -- which Nominatim can return for a way with no
    closed footprint) is treated as "no usable footprint" rather than
    guessed at.
    """
    if not geojson:
        return None
    gtype = geojson.get("type")
    coords = geojson.get("coordinates")
    if not coords:
        return None
    try:
        if gtype == "Polygon":
            ring = coords[0]
        elif gtype == "MultiPolygon":
            ring = coords[0][0]
        else:
            return None
        return [(float(lat), float(lon)) for lon, lat in ring]
    except (IndexError, TypeError, ValueError):
        return None


def _parse_result(raw: dict) -> GeocodingCandidate | None:
    """Returns None (not a raised error) for a single malformed result within an otherwise-valid response array."""
    try:
        lat = float(raw["lat"])
        lon = float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not _valid_coordinate(lat, lon):
        return None

    bbox_raw = raw.get("boundingbox")
    bounding_box = None
    if bbox_raw and len(bbox_raw) == 4:
        try:
            bounding_box = tuple(float(v) for v in bbox_raw)  # (south, north, west, east)
        except (TypeError, ValueError):
            bounding_box = None

    return GeocodingCandidate(
        display_name=raw.get("display_name", ""),
        latitude=lat,
        longitude=lon,
        bounding_box=bounding_box,
        importance=raw.get("importance"),
        osm_type=raw.get("osm_type"),
        osm_id=raw.get("osm_id"),
        category=raw.get("class"),
        place_type=raw.get("type"),
        geometry=_parse_polygon(raw.get("geojson")),
    )


class NominatimGeocodingService:
    """
    Usage:
        service = NominatimGeocodingService()
        candidates = service.search("VIT Chennai")
        candidates = service.search("AB1", viewbox=campus.bounding_box, bounded=True, want_polygon=True)

    Raises a `GeocodingError` subclass for every failure mode the spec
    lists (timeout, connection failure, rate-limit, invalid response) --
    "no results" and "multiple results" are NOT errors, they're just an
    empty or multi-element return list; the caller (`CampusResolver`/
    `BuildingResolver`) decides what to do with that.
    """

    # Phase 15: lets LocationResolver label results by provider without
    # hard-coding OpenStreetMap (see app/location/providers.py).
    provider_name = "OpenStreetMap/Nominatim"
    source_name = "OPENSTREETMAP"
    # bounded=1 is a hard restriction, and BuildingResolver rejects anything
    # outside the campus anyway -- so the extra unbounded pass only cost time
    # (Nominatim allows 1 request/second).
    strict_viewbox = True

    def __init__(
        self,
        *,
        base_url: str | None = None,
        user_agent: str | None = None,
        timeout_s: float | None = None,
        min_request_interval_s: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.nominatim_base_url).rstrip("/")
        self.user_agent = user_agent or settings.nominatim_user_agent
        self.timeout_s = timeout_s if timeout_s is not None else settings.nominatim_timeout_s
        self.min_request_interval_s = (
            min_request_interval_s if min_request_interval_s is not None else settings.nominatim_min_request_interval_s
        )
        self._last_request_at: float = 0.0
        self._session = None  # lazily created -- see _get_session()

    def _get_session(self):
        # Lazy import -- same reasoning as every other optional/heavy
        # dependency in this project (cv2/numpy in app/vision, chromadb
        # in app/rag): don't force `requests`'s import cost onto every
        # caller of this module, only onto callers that actually geocode.
        import requests

        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent})
        return self._session

    def _throttle(self) -> None:
        """
        Nominatim's usage policy caps requests at 1/second. A simple
        sleep-based throttle (not a token bucket/queue -- this project
        has no concurrent geocoding callers) is enough to honor that.
        """
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_request_interval_s - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        viewbox: tuple[float, float, float, float] | None = None,
        bounded: bool = False,
        want_polygon: bool = False,
    ) -> list[GeocodingCandidate]:
        """
        `viewbox` is (south, north, west, east) -- this project's own
        `ResolvedLocation.bounding_box` order (matching Nominatim's own
        `boundingbox` response field) -- converted to Nominatim's
        `<left>,<top>,<right>,<bottom>` request-parameter order
        internally, so every OTHER part of this codebase can use one
        consistent tuple shape. `bounded=True` makes Nominatim treat the
        viewbox as a hard restriction (used by `BuildingResolver` to
        scope a building search to its campus), not just a preference.
        """
        import requests

        params = {
            "q": query,
            "format": "jsonv2",
            "limit": str(limit),
            "addressdetails": "0",
        }
        if want_polygon:
            params["polygon_geojson"] = "1"
        if viewbox is not None:
            south, north, west, east = viewbox
            params["viewbox"] = f"{west},{north},{east},{south}"
            if bounded:
                params["bounded"] = "1"

        self._throttle()
        session = self._get_session()
        try:
            response = session.get(f"{self.base_url}/search", params=params, timeout=self.timeout_s)
        except requests.exceptions.Timeout as exc:
            raise GeocodingTimeoutError(f"Nominatim request timed out after {self.timeout_s}s: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise GeocodingConnectionError(f"Could not connect to Nominatim at {self.base_url}: {exc}") from exc
        except requests.exceptions.RequestException as exc:  # noqa: BLE001 -- any other requests-level failure
            raise GeocodingConnectionError(f"Nominatim request failed: {exc}") from exc

        if response.status_code == 429:
            raise GeocodingRateLimitedError("Nominatim rate limit exceeded (HTTP 429) -- see its usage policy.")
        if not response.ok:
            raise GeocodingInvalidResponseError(
                f"Nominatim returned HTTP {response.status_code}: {response.text[:300]!r}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise GeocodingInvalidResponseError(f"Nominatim response was not valid JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise GeocodingInvalidResponseError(
                f"Expected a JSON array from Nominatim's /search, got {type(payload).__name__}."
            )

        candidates = [c for raw in payload if isinstance(raw, dict) and (c := _parse_result(raw)) is not None]
        log.info("Nominatim search %r -> %d candidate(s) (of %d raw result(s))", query, len(candidates), len(payload))
        return candidates


def _demo_cli() -> None:
    """
    Manual test entry point -- run this on a machine with real internet
    access to verify real Nominatim connectivity (see this module's
    docstring: that has NOT been done from inside this sandbox):
        python -m app.location.geocoding_service "VIT Chennai"
    """
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    query = " ".join(sys.argv[1:]) or "VIT Chennai"
    service = NominatimGeocodingService()
    try:
        candidates = service.search(query)
    except GeocodingError as exc:
        print(f"Geocoding failed: {exc}")
        raise SystemExit(1)

    print(f"Query: {query!r} -> {len(candidates)} candidate(s)\n")
    for i, c in enumerate(candidates, 1):
        print(f"--- [{i}] ---")
        print(json.dumps(c.as_dict(), indent=2))


if __name__ == "__main__":
    _demo_cli()
