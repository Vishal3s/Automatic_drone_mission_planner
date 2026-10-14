"""
Neighbouring buildings as obstacles (boundary fix).

The inspection ring flies at facade level, 15 m from the target. In a
dense area (Chettinad, Chennai Central) that ring can pass over or
through the buildings next door. This module turns the REAL OpenStreetMap
outlines around the target into obstacles, each with a height when OSM
states one -- nothing is guessed:

- `height` / `building:height` tag  -> used as given ("OSM height tag"; for
                                       Microsoft ML footprints, its average-height
                                       estimate, labelled as such).
- `building:levels` only            -> levels x `settings.osm_metres_per_level`
                                       (a stated estimate, labelled as such).
- neither                            -> height UNKNOWN. A flight path that
                                       comes near it at any altitude is
                                       rejected by the safety check, never
                                       assumed to clear it.

The target building itself is left out: an outline is "the target" when
its centre is inside the target's outline, or the target's centre is
inside it (nested outlines). Point-only targets: the outline containing
the point.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.config import settings
from app.location.building_footprints import BuildingFootprint
from app.location.geocoding_service import GeocodingError
from app.location.geofence import point_in_polygon

log = logging.getLogger(__name__)

_NUMBER = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(m|metres|meters)?\s*$", re.IGNORECASE)


@dataclass
class Obstacle:
    """A neighbouring building outline the flight path must stay clear of (or above)."""

    footprint: BuildingFootprint
    height_m: float | None
    height_source: str              # "OSM height tag" | "estimated: N levels x 3.5 m" | "unknown"

    @property
    def label(self) -> str:
        return self.footprint.label

    def distance_m(self, lat: float, lon: float) -> float:
        return self.footprint.distance_m(lat, lon)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "osm_id": self.footprint.osm_id,
            "height_m": self.height_m,
            "height_source": self.height_source,
            "rings": [[[round(a, 7), round(b, 7)] for a, b in ring] for ring in self.footprint.rings],
        }


def _number(value) -> float | None:
    if value is None:
        return None
    m = _NUMBER.match(str(value))
    return float(m.group(1)) if m else None


def obstacle_height(tags: dict) -> tuple[float | None, str]:
    """(height in m or None, where it came from) -- see the module docstring."""
    for key in ("height", "building:height"):
        h = _number(tags.get(key))
        if h is not None and h > 0:
            return h, tags.get("height_source") or "OSM height tag"
    levels = _number(tags.get("building:levels"))
    if levels is not None and levels > 0:
        per = float(settings.osm_metres_per_level)
        return levels * per, f"estimated: {levels:g} levels x {per:g} m"
    return None, "unknown"


def _centre(rings: list[list[tuple[float, float]]]) -> tuple[float, float]:
    pts = [p for r in rings for p in r]
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def _is_target(footprint: BuildingFootprint, target_rings, target_point: tuple[float, float]) -> bool:
    if not target_rings:
        return footprint.contains(*target_point)
    flat, flon = _centre(footprint.rings)
    if any(point_in_polygon(flat, flon, r) for r in target_rings):
        return True
    tlat, tlon = _centre(target_rings)
    return footprint.contains(tlat, tlon)


def neighbour_obstacles(footprints: list[BuildingFootprint], resolved_location) -> list[Obstacle]:
    """Every outline except the target's own, with its height (known, estimated or unknown)."""
    target_rings = [r for r in resolved_location.footprint_rings if len(r) >= 3]
    point = (resolved_location.latitude, resolved_location.longitude)
    out = []
    for f in footprints:
        if _is_target(f, target_rings, point):
            continue
        h, src = obstacle_height(f.tags)
        out.append(Obstacle(footprint=f, height_m=h, height_source=src))
    return out


def fetch_neighbour_obstacles(footprint_service, resolved_location, search_radius_m: float) -> tuple[list[Obstacle] | None, str | None]:
    """
    (obstacles, warning). `obstacles` is None when the lookup could not be
    done (no service / Overpass failed) -- callers must then report
    "neighbouring buildings NOT checked", never "none found".
    """
    if footprint_service is None:
        return None, "Neighbouring buildings NOT checked -- no OpenStreetMap outline service is configured."
    try:
        footprints = footprint_service.footprints_near(
            resolved_location.latitude, resolved_location.longitude, radius_m=search_radius_m
        )
    except GeocodingError as exc:
        log.warning("Neighbour lookup failed: %s", exc)
        return None, f"Neighbouring buildings NOT checked -- OpenStreetMap lookup failed: {exc}"
    return neighbour_obstacles(footprints, resolved_location), None
