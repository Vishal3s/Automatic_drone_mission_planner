"""
Road scan -- fly along a named road and point the camera down the road.

Requested as e.g. "Road scan of Karanai Main Road, Sonalur, Tamil Nadu 600130 at 20m".
The address gives the place; the road is the mapped road with that name
nearest to it (OpenStreetMap). The scan is a single pass along a stretch of
that road (at most `road_scan_max_length_m`, centred on the place), at a
constant altitude, with the nose following the road and the gimbal tilted
down (`road_scan_gimbal_pitch_deg`) so the surface stays in frame.

What it does NOT do, stated plainly so nobody relies on it:
- It does not know about trees, poles, wires or traffic. The map has no
  such data, so the plan always carries a warning to check the route by eye.
- It flies the mapped centreline. No lateral offset is applied.
- Road names are matched loosely (Rd/Road, St/Street); two roads with the
  same name nearby use the closest and say so.

Deterministic, like the structural scan: no LLM. The result goes through
Phase 5 rule validation and the location safety check in
location_aware_generator.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from math import atan2, ceil, cos, degrees, hypot, radians

from app.config import settings
from app.location.geofence import haversine_km
from app.mission.inspection_camera import CameraAim
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.validator import validate_mission

log = logging.getLogger(__name__)

_M_PER_DEG_LAT = 111_320.0
_ABBREVIATIONS = {"rd": "road", "st": "street", "ave": "avenue", "blvd": "boulevard", "ln": "lane", "dr": "drive", "hwy": "highway"}


class RoadScanError(ValueError):
    """The road scan cannot be planned -- the message is the plain reason."""


@dataclass
class RoadWay:
    name: str
    points: list[tuple[float, float]]   # (lat, lon), in road order


@dataclass
class RoadScanPlan:
    mission: Mission
    road_name: str
    length_m: float
    altitude_m: float
    altitude_source: str
    camera_aims: list[CameraAim] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------


def _tokens(name: str) -> list[str]:
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    return [_ABBREVIATIONS.get(w, w) for w in words]


def road_name_from_address(address: str) -> str:
    """'Karanai Main Rd, Sonalur, Tamil Nadu 600130' -> 'Karanai Main Rd'."""
    return (address or "").split(",")[0].strip()


def names_match(osm_name: str, wanted: str) -> bool:
    """True when every word of the wanted road name appears in the mapped name (Rd == Road)."""
    want = _tokens(wanted)
    if not want:
        return False
    have = set(_tokens(osm_name))
    return all(t in have for t in want)


# ---------------------------------------------------------------------
# Geometry (local metres, flat earth -- fine at road-stretch scale)
# ---------------------------------------------------------------------


class _Local:
    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0, self.lon0 = lat0, lon0
        self.kx = _M_PER_DEG_LAT * cos(radians(lat0))

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * _M_PER_DEG_LAT

    def ll(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / _M_PER_DEG_LAT, self.lon0 + x / self.kx


def _distance_to_polyline(local: _Local, pts_xy, px: float, py: float) -> tuple[float, float, float]:
    """(distance m, arc length m from the start to the nearest point, segment index)."""
    best = (float("inf"), 0.0, 0)
    travelled = 0.0
    for i in range(len(pts_xy) - 1):
        (ax, ay), (bx, by) = pts_xy[i], pts_xy[i + 1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        seg = seg2 ** 0.5
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        cx, cy = ax + t * dx, ay + t * dy
        d = hypot(cx - px, cy - py)
        if d < best[0]:
            best = (d, travelled + t * seg, i)
        travelled += seg
    return best


def _window(polyline: list[tuple[float, float]], s_center: float, max_len: float) -> list[tuple[float, float]]:
    """A piece of `polyline` of at most `max_len` metres, centred on arc length `s_center`."""
    local = _Local(*polyline[0])
    xy = [local.xy(lat, lon) for lat, lon in polyline]
    cum = [0.0]
    for (ax, ay), (bx, by) in zip(xy, xy[1:]):
        cum.append(cum[-1] + hypot(bx - ax, by - ay))
    total = cum[-1]
    if total <= max_len:
        return polyline
    start = max(0.0, min(s_center - max_len / 2, total - max_len))
    end = start + max_len

    def at(s: float) -> tuple[float, float]:
        for i in range(len(cum) - 1):
            if cum[i] <= s <= cum[i + 1]:
                seg = cum[i + 1] - cum[i] or 1.0
                t = (s - cum[i]) / seg
                (ax, ay), (bx, by) = xy[i], xy[i + 1]
                return local.ll(ax + t * (bx - ax), ay + t * (by - ay))
        return polyline[-1]

    out = [at(start)] + [polyline[i] for i in range(len(polyline)) if start < cum[i] < end] + [at(end)]
    return out


def _resample(polyline: list[tuple[float, float]], spacing_m: float, max_points: int) -> list[tuple[float, float]]:
    """Points evenly spaced along the polyline, both ends included, at most `max_points`."""
    local = _Local(*polyline[0])
    xy = [local.xy(lat, lon) for lat, lon in polyline]
    cum = [0.0]
    for (ax, ay), (bx, by) in zip(xy, xy[1:]):
        cum.append(cum[-1] + hypot(bx - ax, by - ay))
    total = cum[-1]
    count = max(2, min(max_points, int(ceil(total / spacing_m)) + 1))
    out = []
    j = 0
    for k in range(count):
        s = total * k / (count - 1)
        while j < len(cum) - 2 and cum[j + 1] < s:
            j += 1
        seg = cum[j + 1] - cum[j] or 1.0
        t = max(0.0, min(1.0, (s - cum[j]) / seg))
        (ax, ay), (bx, by) = xy[j], xy[j + 1]
        out.append(local.ll(ax + t * (bx - ax), ay + t * (by - ay)))
    return out


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    local = _Local(*a)
    bx, by = local.xy(*b)
    return (degrees(atan2(bx, by)) + 360.0) % 360.0


# ---------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------


def pick_named_road(ways: list[RoadWay], wanted: str, point: tuple[float, float]) -> tuple[RoadWay, float, list[str], float]:
    """The mapped way with that name nearest `point` -> (way, arc length to the nearest point m, warnings, distance m). Raises RoadScanError if none."""
    matching = [w for w in ways if w.points and names_match(w.name, wanted)]
    if not matching:
        raise RoadScanError(
            f"No mapped road named '{wanted}' was found near this place. Check the road name and the place, "
            "or give a point on the road."
        )
    local = _Local(*point)
    px, py = local.xy(*point)
    scored = []
    for w in matching:
        pts_xy = [local.xy(lat, lon) for lat, lon in w.points]
        d, s, _ = _distance_to_polyline(local, pts_xy, px, py)
        scored.append((d, s, w))
    scored.sort(key=lambda t: t[0])
    best_d, best_s, best = scored[0]
    warnings = []
    if len(scored) > 1 and scored[1][0] - best_d < 50.0 and scored[1][2].points != best.points:
        warnings.append(
            f"More than one road named '{wanted}' is mapped close to this place; the closest one was used "
            f"({best_d:.0f} m away). Confirm it is the road you meant."
        )
    return best, best_s, warnings, best_d


def plan_road_scan(
    ways: list[RoadWay],
    wanted: str,
    point: tuple[float, float],
    *,
    altitude_m: float | None = None,
    name: str | None = None,
) -> RoadScanPlan:
    """Plan one pass along the named road near `point`. Raises RoadScanError with a plain reason."""
    way, _s, warnings, _d = pick_named_road(ways, wanted, point)
    local = _Local(*point)
    px, py = local.xy(*point)
    pts_xy = [local.xy(lat, lon) for lat, lon in way.points]
    _dist, s_center, _seg = _distance_to_polyline(local, pts_xy, px, py)

    stretch = _window(way.points, s_center, float(settings.road_scan_max_length_m))
    max_points = int(settings.max_waypoints) - 2
    if altitude_m is not None:
        altitude, altitude_source = float(altitude_m), "requested altitude"
    else:
        altitude, altitude_source = float(settings.road_scan_altitude_m), "default altitude"
        warnings.append(
            f"No altitude was given -- flying at the default {settings.road_scan_altitude_m:g} m. "
            "Check the height above the road by eye before flying."
        )
    if altitude > float(settings.max_altitude_m) or altitude < float(settings.min_altitude_m):
        raise RoadScanError(
            f"The altitude {altitude:g} m is outside the allowed range "
            f"({settings.min_altitude_m:g}-{settings.max_altitude_m:g} m)."
        )

    points = _resample(stretch, float(settings.road_scan_spacing_m), max_points)
    length = sum(
        haversine_km(a[0], a[1], b[0], b[1]) * 1000.0 for a, b in zip(stretch, stretch[1:])
    )
    warnings.append(
        "The map has no data on trees, poles, wires or traffic along this road -- check the route by eye before flying."
    )

    items = [MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=altitude)]
    aims: list[CameraAim] = []
    pitch = float(settings.road_scan_gimbal_pitch_deg)
    for k, (lat, lon) in enumerate(points):
        nxt = points[k + 1] if k + 1 < len(points) else points[k - 1]
        # Nose along the road: towards the next point, or along the last leg for the final point.
        yaw = round(_bearing((lat, lon), nxt) if k + 1 < len(points) else _bearing(nxt, (lat, lon)), 1) % 360.0
        items.append(MissionItem(
            sequence=k + 2, command=MissionCommand.WAYPOINT,
            latitude=round(lat, 7), longitude=round(lon, 7), altitude=round(altitude, 1),
            yaw_deg=yaw, gimbal_pitch_deg=pitch, take_photo=True,
        ))
        aims.append(CameraAim(k + 2, yaw, pitch, 0.0, None))
    items.append(MissionItem(sequence=len(items) + 1, command=MissionCommand.RETURN_TO_LAUNCH))

    mission = Mission(mission_name=name or f"Road scan of {way.name}", mission_items=items)
    return RoadScanPlan(
        mission=mission, road_name=way.name, length_m=round(length, 1),
        altitude_m=altitude, altitude_source=altitude_source, camera_aims=aims, warnings=warnings,
    )
