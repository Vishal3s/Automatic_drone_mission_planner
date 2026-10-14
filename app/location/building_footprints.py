"""
Phase 15.1 -- recognize a building and get its REAL boundary from
OpenStreetMap building footprints (Overpass API).

Two ways a footprint is recognized -- both use real mapped data, neither
guesses:

1. By NAME: an OSM building inside the campus tagged name / ref /
   alt_name / short_name ... matching the requested building
   ("AB1", "Academic Block 1", "Academic Block-1", ...).
2. By ONE COORDINATE: given a point that is known to be the building
   (a Google Places result for that building, or one coordinate from the
   project's own documents), the footprint that CONTAINS that point is
   that building's boundary. A point slightly outside every outline
   (Google often places pins at an entrance) is snapped to the nearest
   outline only if it is within `footprint_snap_distance_m` and no other
   outline is comparably close; the snap is reported as a warning.

What this never does: start from the campus centre (or any point that is
not already known to be the building) and pick the nearest building --
that would be guessing which building is AB1.

OpenStreetMap data is (c) OpenStreetMap contributors, ODbL -- the map page
shows that attribution whenever an OSM outline is drawn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from math import cos, radians

from app.config import settings
from app.location.building_names import mentions_building
from app.location.geocoding_service import (
    GeocodingConnectionError,
    GeocodingError,
    GeocodingInvalidResponseError,
    GeocodingRateLimitedError,
    GeocodingTimeoutError,
)
from app.location.geofence import point_in_polygon

log = logging.getLogger(__name__)

_NAME_TAGS = ("name", "name:en", "ref", "alt_name", "short_name", "official_name", "loc_name", "addr:housename")
_M_PER_DEG = 111_320.0


@dataclass
class BuildingFootprint:
    osm_id: str                                   # e.g. "way/123456" (or "msft/<quadkey>/<n>" for Microsoft)
    rings: list[list[tuple[float, float]]]        # outer ring(s), (lat, lon)
    tags: dict = field(default_factory=dict)
    source: str = "OPENSTREETMAP"                 # "OPENSTREETMAP" | "MICROSOFT" (GlobalMLBuildingFootprints)

    @property
    def label(self) -> str:
        for key in _NAME_TAGS:
            if self.tags.get(key):
                return self.tags[key]
        if self.source == "MICROSOFT":
            return f"Microsoft footprint {self.osm_id.rsplit('/', 1)[-1]}"
        return self.osm_id

    def names(self) -> str:
        return " | ".join(str(self.tags[k]) for k in _NAME_TAGS if self.tags.get(k))

    def contains(self, lat: float, lon: float) -> bool:
        return any(point_in_polygon(lat, lon, ring) for ring in self.rings)

    def area_m2(self) -> float:
        total = 0.0
        for ring in self.rings:
            lat0 = ring[0][0]
            pts = [((lon) * _M_PER_DEG * cos(radians(lat0)), lat * _M_PER_DEG) for lat, lon in ring]
            s = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
            total += abs(s) / 2.0
        return total

    def distance_m(self, lat: float, lon: float) -> float:
        """0 if inside, else the shortest distance (m) to any edge -- local flat-earth, fine at building scale."""
        if self.contains(lat, lon):
            return 0.0
        kx = _M_PER_DEG * cos(radians(lat))
        best = float("inf")
        for ring in self.rings:
            for (a_lat, a_lon), (b_lat, b_lon) in zip(ring, ring[1:] + ring[:1]):
                ax, ay = (a_lon - lon) * kx, (a_lat - lat) * _M_PER_DEG
                bx, by = (b_lon - lon) * kx, (b_lat - lat) * _M_PER_DEG
                dx, dy = bx - ax, by - ay
                seg2 = dx * dx + dy * dy
                t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
                px, py = ax + t * dx, ay + t * dy
                best = min(best, (px * px + py * py) ** 0.5)
        return best


class AmbiguousFootprintError(GeocodingError):
    def __init__(self, footprints: list[BuildingFootprint]):
        self.footprints = footprints
        super().__init__("several mapped buildings match: " + "; ".join(f"{f.label} ({f.osm_id})" for f in footprints))


def _ring_from_geometry(points) -> list[tuple[float, float]] | None:
    try:
        ring = [(float(p["lat"]), float(p["lon"])) for p in points]
    except (KeyError, TypeError, ValueError):
        return None
    return ring if len(ring) >= 3 else None


def parse_overpass(payload: dict) -> list[BuildingFootprint]:
    """`out tags geom;` elements -> footprints. Ways: inline geometry. Relations: their outer member ways."""
    footprints = []
    for el in payload.get("elements", []) or []:
        if not isinstance(el, dict):
            continue
        rings: list[list[tuple[float, float]]] = []
        if el.get("type") == "way":
            ring = _ring_from_geometry(el.get("geometry") or [])
            if ring:
                rings.append(ring)
        elif el.get("type") == "relation":
            for member in el.get("members", []) or []:
                if member.get("type") == "way" and member.get("role", "outer") in ("outer", ""):
                    ring = _ring_from_geometry(member.get("geometry") or [])
                    if ring:
                        rings.append(ring)
        if rings:
            footprints.append(BuildingFootprint(osm_id=f"{el.get('type')}/{el.get('id')}", rings=rings, tags=el.get("tags") or {}))
    return footprints


class OverpassFootprintService:
    """Fetches OSM building outlines. Same error types as the geocoding services."""

    def __init__(self, *, url: str | None = None, timeout_s: float | None = None, session=None) -> None:
        self.url = url or settings.overpass_url
        self.timeout_s = settings.overpass_timeout_s if timeout_s is None else timeout_s
        self._session = session

    def _query(self, query: str) -> list[BuildingFootprint]:
        """Tries each configured Overpass endpoint in turn; only 'busy/unreachable' moves on to the next."""
        urls = [u.strip() for u in self.url.split(",") if u.strip()]
        last: GeocodingError | None = None
        for url in urls:
            try:
                return self._query_one(url, query)
            except (GeocodingConnectionError, GeocodingTimeoutError, GeocodingRateLimitedError) as exc:
                log.warning("Overpass endpoint %s failed (%s)%s", url, exc, " -- trying the next one" if url != urls[-1] else "")
                last = exc
        raise last or GeocodingConnectionError("No Overpass endpoint configured.")

    def _query_one(self, url: str, query: str) -> list[BuildingFootprint]:
        import requests

        session = self._session or requests.Session()
        try:
            response = session.post(
                url, data={"data": query}, timeout=self.timeout_s,
                headers={"User-Agent": settings.nominatim_user_agent},
            )
        except requests.exceptions.Timeout as exc:
            raise GeocodingTimeoutError(f"Overpass API timed out after {self.timeout_s}s") from exc
        except requests.exceptions.RequestException as exc:
            raise GeocodingConnectionError(f"Could not reach the Overpass API: {exc}") from exc
        if response.status_code in (429,):
            raise GeocodingRateLimitedError("Overpass API rate limit (HTTP 429) -- try again shortly.")
        if response.status_code in (502, 503, 504):
            raise GeocodingConnectionError(f"Overpass API busy/unavailable (HTTP {response.status_code}).")
        if response.status_code != 200:
            raise GeocodingInvalidResponseError(f"Overpass API returned HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GeocodingInvalidResponseError("Overpass API response was not JSON.") from exc
        return parse_overpass(payload)

    def footprints_in_bbox(self, bbox: tuple[float, float, float, float]) -> list[BuildingFootprint]:
        """`bbox` is (south, north, west, east)."""
        s, n, w, e = bbox
        box = f"{s},{w},{n},{e}"
        return self._query(f'[out:json][timeout:{int(self.timeout_s)}];(way["building"]({box});relation["building"]({box}););out tags geom;')

    def footprints_near(self, lat: float, lon: float, radius_m: float) -> list[BuildingFootprint]:
        around = f"around:{int(radius_m)},{lat},{lon}"
        return self._query(f'[out:json][timeout:{int(self.timeout_s)}];(way["building"]({around});relation["building"]({around}););out tags geom;')


# ---------------------------------------------------------------------
# Recognition (pure functions -- no network)
# ---------------------------------------------------------------------


def find_named_footprint(footprints: list[BuildingFootprint], variants: list[str]) -> BuildingFootprint | None:
    """Recognition #1 -- a footprint whose own name tags match the building. Several different ones -> ambiguous."""
    matches = [f for f in footprints if f.names() and mentions_building(f.names(), variants)]
    if len(matches) > 1:
        raise AmbiguousFootprintError(matches)
    return matches[0] if matches else None


def footprint_for_point(
    footprints: list[BuildingFootprint], lat: float, lon: float, *, snap_m: float | None = None
) -> tuple[BuildingFootprint, str | None] | None:
    """
    Recognition #2 -- the building at a point already known to be that
    building. Returns (footprint, warning-or-None), or None when no outline
    can be tied to the point without guessing.
    """
    snap = settings.footprint_snap_distance_m if snap_m is None else snap_m
    containing = [f for f in footprints if f.contains(lat, lon)]
    if containing:
        return min(containing, key=lambda f: f.area_m2()), None  # innermost if outlines are nested
    ranked = sorted(((f.distance_m(lat, lon), f) for f in footprints), key=lambda t: t[0])
    if not ranked or ranked[0][0] > snap:
        return None
    if len(ranked) > 1 and ranked[1][0] <= snap and ranked[1][0] < 2 * ranked[0][0] + 1.0:
        return None  # two outlines about equally close -- can't tell which building the point means
    d, f = ranked[0]
    return f, f"The building's point was {d:.1f} m outside the nearest mapped outline ({f.label}); snapped to it."
