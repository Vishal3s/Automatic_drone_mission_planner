"""
Location resolution improvement -- remaining Phase C: perimeter-inspection
reference geometry for the EXISTING waypoint generator.

Spec section 9: "DO NOT create a completely new waypoint-generation
system... Continue using the existing waypoint-generation logic, including
where applicable: 360-degree perimeter inspection, standoff distance..."

This project's existing waypoint generator is the LLM planner
(`generate_validated_mission()`). Before this phase it only received the
building's centre point, so a "perimeter inspection" meant the LLM
inventing coordinates around it. This module computes, deterministically,
a ring of REFERENCE points around the real footprint at a safe standoff,
and hands them to the unchanged planner as context. The LLM still builds
the Mission; Phase 5 validation and the location checks still judge it.

Geometry (documented simplifications, no GIS dependency):
- Footprint known (OSM polygon): boundary fix -- the ring FOLLOWS the
  building's shape: the convex hull of every outline vertex, pushed out by
  the standoff (straight sides, rounded corners). It lies wholly outside
  the building whatever its shape (conservative for L/U shapes, whose
  inner corners it bridges), and stays close to long or narrow buildings
  instead of sweeping a big circle over the neighbours.
- Point only: the footprint is ASSUMED to fit within
  `settings.building_unknown_footprint_radius_m` of that point (a stated,
  deliberately generous assumption), then + standoff, as a circle. Always
  flagged.
- Neighbouring buildings (app/location/obstacles.py): points near another
  outline are pulled in toward the target (not below
  `inspection_min_standoff_m`); otherwise they get a minimum altitude from
  that building's mapped height, or are marked BLOCKED if its height is
  unknown.
- Local flat-earth offsets (metres -> degrees) -- accurate to well under a
  metre at building scale (a few hundred metres).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import atan2, ceil, cos, degrees, hypot, pi, radians, sin

from app.config import settings
from app.location.geofence import haversine_km

_METERS_PER_DEG_LAT = 111_320.0


@dataclass
class RingPoint:
    """One reference point, plus why it is where it is (boundary fix)."""

    latitude: float
    longitude: float
    standoff_m: float                      # actual horizontal distance kept from the target's outline
    min_altitude_m: float | None = None    # set when the point (or the leg into it) passes a neighbour that must be overflown
    note: str | None = None                # pulled in / over a neighbour / blocked
    blocked: bool = False                  # near a neighbour of UNKNOWN height -- cannot be flown safely at any stated altitude

    def as_dict(self) -> dict:
        return {
            "latitude": round(self.latitude, 7), "longitude": round(self.longitude, 7),
            "standoff_m": round(self.standoff_m, 1), "min_altitude_m": self.min_altitude_m,
            "note": self.note, "blocked": self.blocked,
        }


@dataclass
class InspectionRing:
    """
    Reference points around a building at a standoff distance.

    Boundary fix: with a real outline the ring FOLLOWS the building's
    shape (its convex hull pushed out by the standoff), instead of a circle
    around the farthest corner -- a long building no longer gets a ring
    that sweeps far out over its neighbours. With only a point it stays the
    documented assumed circle. `radius_m` is kept as the farthest distance
    from the centre to the ring (used for the map zoom and old callers).
    """

    center_latitude: float
    center_longitude: float
    footprint_radius_m: float       # radius that encloses the whole building footprint (measured or assumed)
    footprint_measured: bool        # True = from a real polygon; False = the documented assumption
    standoff_m: float
    radius_m: float                 # farthest ring point from the centre
    points: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon), clockwise from the northmost
    shape: str = "circle"           # "outline" (follows the footprint) | "circle" (point-only assumption)
    path: list[tuple[float, float]] = field(default_factory=list)    # the full ring line, for the map
    details: list[RingPoint] = field(default_factory=list)            # same order as `points`
    neighbours: list = field(default_factory=list)                    # obstacles.Obstacle, [] = checked and none
    neighbours_checked: bool = False
    notes: list[str] = field(default_factory=list)
    perimeter_m: float = 0.0        # length of the whole ring
    spacing_m: float = 0.0          # distance between consecutive points along it

    @property
    def blocked(self) -> list[int]:
        return [i for i, d in enumerate(self.details, start=1) if d.blocked]

    def as_dict(self) -> dict:
        return {
            "center": [self.center_latitude, self.center_longitude],
            "footprint_radius_m": round(self.footprint_radius_m, 2),
            "footprint_measured": self.footprint_measured,
            "standoff_m": self.standoff_m,
            "radius_m": round(self.radius_m, 2),
            "shape": self.shape,
            "points": [[round(lat, 7), round(lon, 7)] for lat, lon in self.points],
            "details": [d.as_dict() for d in self.details],
            "neighbours_checked": self.neighbours_checked,
            "neighbours": [n.as_dict() for n in self.neighbours],
            "notes": list(self.notes),
            "perimeter_m": round(self.perimeter_m, 1),
            "spacing_m": round(self.spacing_m, 1),
        }


def offset_point(latitude: float, longitude: float, distance_m: float, bearing_deg: float) -> tuple[float, float]:
    """Move `distance_m` from (lat, lon) along `bearing_deg` (0 = north, 90 = east)."""
    b = radians(bearing_deg)
    dlat = distance_m * cos(b) / _METERS_PER_DEG_LAT
    dlon = distance_m * sin(b) / (_METERS_PER_DEG_LAT * cos(radians(latitude)))
    return latitude + dlat, longitude + dlon


def polygon_centroid(polygon: list[tuple[float, float]]) -> tuple[float, float]:
    """Vertex average -- adequate for a building-scale footprint's centre (open or closed ring)."""
    pts = polygon[:-1] if len(polygon) > 1 and polygon[0] == polygon[-1] else polygon
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def _rings(resolved_location) -> list[list[tuple[float, float]]]:
    """Every real footprint ring (all MultiPolygon parts); [] for a point-only location."""
    rings = getattr(resolved_location, "footprint_rings", None)
    if rings is not None:
        return rings
    if resolved_location.geometry_available and resolved_location.geometry:
        return [list(resolved_location.geometry)]
    return []


def footprint_extent(resolved_location) -> tuple[float, float, float, bool]:
    """
    (center_lat, center_lon, footprint_radius_m, measured) for a
    `ResolvedLocation`. `measured` is False when the radius is the
    documented assumption for a point-only resolution.
    """
    rings = [r for r in _rings(resolved_location) if len(r) >= 3]
    if rings:
        # Phase 15: a MultiPolygon footprint uses every part's vertices.
        vertices = [p for ring in rings for p in ring]
        clat, clon = polygon_centroid(vertices)
        radius_m = max(haversine_km(clat, clon, lat, lon) for lat, lon in vertices) * 1000.0
        return clat, clon, radius_m, True
    return (
        resolved_location.latitude,
        resolved_location.longitude,
        float(settings.building_unknown_footprint_radius_m),
        False,
    )


# ---------------------------------------------------------------------
# Boundary fix -- shape-following ring (local metres, no GIS dependency)
# ---------------------------------------------------------------------


class _Local:
    """Flat-earth x/y metres around a reference point (x = east, y = north)."""

    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0, self.lon0 = lat0, lon0
        self.kx = _METERS_PER_DEG_LAT * cos(radians(lat0))

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * _METERS_PER_DEG_LAT

    def ll(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / _METERS_PER_DEG_LAT, self.lon0 + x / self.kx


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Monotone chain; counter-clockwise, no repeated last point."""
    pts = sorted(set((round(x, 3), round(y, 3)) for x, y in points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _offset_hull(hull: list[tuple[float, float]], d: float, arc_step_deg: float = 10.0):
    """
    The hull pushed outward by `d` (its Minkowski sum with a circle):
    straight edges moved out, rounded corners. Returns [(x, y, anchor_x,
    anchor_y)] counter-clockwise -- the anchor is the hull point each ring
    point is measured from, so it can later be pulled back in.
    """
    out = []
    n = len(hull)
    for i in range(n):
        prev, cur, nxt = hull[i - 1], hull[i], hull[(i + 1) % n]
        a_in = atan2(-(cur[0] - prev[0]), cur[1] - prev[1])    # outward normal of edge prev->cur (CCW)
        a_out = atan2(-(nxt[0] - cur[0]), nxt[1] - cur[1])     # outward normal of edge cur->nxt
        while a_out < a_in:
            a_out += 2 * pi
        steps = max(1, int(ceil(degrees(a_out - a_in) / arc_step_deg)))
        for k in range(steps + 1):
            a = a_in + (a_out - a_in) * k / steps
            out.append((cur[0] + d * cos(a), cur[1] + d * sin(a), cur[0], cur[1]))
    return out


def _resample(loop, count: int):
    """`count` points evenly spaced along a closed loop of (x, y, ax, ay), starting at its first entry."""
    segs = [(loop[i], loop[(i + 1) % len(loop)]) for i in range(len(loop))]
    lengths = [hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs]
    total = sum(lengths)
    out, j, acc = [], 0, 0.0
    for k in range(count):
        target = total * k / count
        while j < len(segs) - 1 and acc + lengths[j] < target:
            acc += lengths[j]
            j += 1
        a, b = segs[j]
        t = 0.0 if lengths[j] == 0 else (target - acc) / lengths[j]
        out.append(tuple(a[m] + (b[m] - a[m]) * t for m in range(4)))
    return out, total


def _ring_count(perimeter_m: float, n_points: int | None) -> int:
    if n_points is not None:
        return int(n_points)
    base = int(settings.inspection_ring_points)
    by_spacing = int(ceil(perimeter_m / float(settings.inspection_max_point_spacing_m)))
    return max(base, min(int(settings.inspection_max_ring_points), by_spacing))


def _start_north_clockwise(loop):
    """Reorder a CCW loop to start at its northmost entry and run clockwise."""
    i = max(range(len(loop)), key=lambda k: (loop[k][1], -loop[k][0]))
    ccw = loop[i:] + loop[:i]
    return [ccw[0]] + ccw[:0:-1]


def _point_clear(lat: float, lon: float, obstacles, clearance_m: float) -> list:
    return [o for o in obstacles if o.distance_m(lat, lon) <= clearance_m]


def _leg_hits(a: tuple[float, float], b: tuple[float, float], obstacles, clearance_m: float, step_m: float = 2.0) -> list:
    """Obstacles within `clearance_m` of the straight leg a->b (sampled every `step_m`)."""
    if not obstacles:
        return []
    local = _Local(*a)
    bx, by = local.xy(*b)
    n = max(1, int(ceil(hypot(bx, by) / step_m)))
    hits = []
    for o in obstacles:
        for k in range(n + 1):
            lat, lon = local.ll(bx * k / n, by * k / n)
            if o.distance_m(lat, lon) <= clearance_m:
                hits.append(o)
                break
    return hits


def _required_over(obstacles) -> tuple[float | None, bool, str]:
    """(min altitude, blocked, text) for flying over these obstacles."""
    clearance = float(settings.building_roof_clearance_m)
    unknown = [o for o in obstacles if o.height_m is None]
    names = ", ".join(sorted({o.label for o in obstacles}))
    if unknown and settings.neighbour_unknown_height_blocks:
        return None, True, f"over/next to {names} -- height not mapped, cannot be cleared safely"
    # Default: plan anyway. An unmapped height is ASSUMED (stated in the note), never treated as zero.
    assumed = float(settings.unknown_building_assumed_height_m)
    need = max(o.height_m if o.height_m is not None else assumed for o in obstacles) + clearance
    if unknown:
        unknown_names = ", ".join(sorted({o.label for o in unknown}))
        return need, False, (
            f"passes {names} -- height of {unknown_names} not mapped, assumed {assumed:g} m; "
            f"fly at least {need:g} m (verify visually)"
        )
    return need, False, f"passes {names} -- fly at least {need:g} m (mapped height + {clearance:g} m)"


def compute_inspection_ring(
    resolved_location, *, standoff_m: float | None = None, n_points: int | None = None,
    neighbours: list | None = None,
    start_near: tuple[float, float] | None = None,
) -> InspectionRing:
    """
    Reference points around the building.

    Real outline -> the ring follows its shape (convex hull + standoff),
    points evenly spaced along it (count grows with the perimeter, capped by
    settings). Point only -> the documented assumed circle.

    `neighbours` (obstacles.Obstacle list; None = not checked): a point
    that would be within `neighbour_clearance_m` of another building is
    first pulled in toward the target, down to `inspection_min_standoff_m`.
    If that is still not clear, it stays put and gets a minimum altitude
    (that building's mapped height + roof clearance) -- or, when the
    neighbour's height is unknown, is marked BLOCKED.
    """
    standoff = float(settings.inspection_standoff_m if standoff_m is None else standoff_m)
    if n_points is not None and int(n_points) < 3:
        raise ValueError("an inspection ring needs at least 3 points")

    clat, clon, footprint_radius_m, measured = footprint_extent(resolved_location)
    local = _Local(clat, clon)
    rings = [r for r in _rings(resolved_location) if len(r) >= 3]
    hull = _convex_hull([local.xy(lat, lon) for ring in rings for lat, lon in ring]) if rings else []

    if len(hull) >= 3:
        shape = "outline"
        loop = _offset_hull(hull, standoff)
    else:  # point only (or a degenerate outline) -- the assumed circle, as before
        shape = "circle"
        loop = [
            (r * sin(radians(a)), r * cos(radians(a)), footprint_radius_m * sin(radians(a)), footprint_radius_m * cos(radians(a)))
            for a in [-k * 5.0 for k in range(72)]  # counter-clockwise from north
            for r in [footprint_radius_m + standoff]
        ]

    loop = _start_north_clockwise(loop)
    if start_near is not None:
        # Efficient order: begin at the ring point closest to home (launch), still clockwise.
        hx, hy = local.xy(*start_near)
        k = min(range(len(loop)), key=lambda i: hypot(loop[i][0] - hx, loop[i][1] - hy))
        loop = loop[k:] + loop[:k]
    perimeter = sum(hypot(loop[i][0] - loop[i - 1][0], loop[i][1] - loop[i - 1][1]) for i in range(len(loop)))
    samples, _ = _resample(loop, _ring_count(perimeter, n_points))

    obstacles = list(neighbours or [])
    clearance = float(settings.neighbour_clearance_m)
    min_standoff = min(standoff, float(settings.inspection_min_standoff_m))
    details: list[RingPoint] = []
    notes: list[str] = []
    for x, y, ax, ay in samples:
        ux, uy = x - ax, y - ay
        norm = hypot(ux, uy) or 1.0
        ux, uy = ux / norm, uy / norm
        lat, lon = local.ll(x, y)
        point = RingPoint(lat, lon, standoff)
        hits = _point_clear(lat, lon, obstacles, clearance)
        if hits:
            s = standoff - 1.0
            while s >= min_standoff:
                plat, plon = local.ll(ax + ux * s, ay + uy * s)
                if not _point_clear(plat, plon, obstacles, clearance):
                    point = RingPoint(plat, plon, s, note=f"pulled in to {s:g} m to stay clear of {', '.join(sorted({o.label for o in hits}))}")
                    break
                s -= 1.0
            else:
                need, blocked, text = _required_over(hits)
                point = RingPoint(lat, lon, standoff, min_altitude_m=need, note=text, blocked=blocked)
        details.append(point)

    # Legs between consecutive points (and the closing leg) may cut a corner of a neighbour.
    for i in range(len(details)):
        a, b = details[i - 1], details[i]
        hits = _leg_hits((a.latitude, a.longitude), (b.latitude, b.longitude), obstacles, clearance)
        if not hits:
            continue
        need, blocked, text = _required_over(hits)
        for p in (a, b):
            if blocked:
                p.blocked = True
            elif need is not None and (p.min_altitude_m is None or need > p.min_altitude_m):
                p.min_altitude_m = need
            p.note = p.note or f"leg {text}"

    for i, d in enumerate(details, start=1):
        if d.note:
            notes.append(f"P{i}: {d.note}")

    points = [(d.latitude, d.longitude) for d in details]
    radius = (
        footprint_radius_m + standoff if shape == "circle"
        else max(haversine_km(clat, clon, lat, lon) * 1000.0 for lat, lon in points)
    )
    path = [local.ll(x, y) for x, y, _, _ in loop]
    return InspectionRing(
        center_latitude=clat, center_longitude=clon,
        footprint_radius_m=footprint_radius_m, footprint_measured=measured,
        standoff_m=standoff, radius_m=radius, points=points,
        shape=shape, path=path, details=details,
        neighbours=obstacles, neighbours_checked=neighbours is not None, notes=notes,
        perimeter_m=perimeter, spacing_m=perimeter / len(points),
    )


def render_inspection_ring(ring: InspectionRing, *, altitude_hint_m: float | None = None) -> str:
    """Plain-text block for the planner's location context -- tells the existing LLM planner to use these exact coordinates."""
    if ring.shape == "outline":
        basis = f"follows the real building outline at a {ring.standoff_m:g} m standoff"
    else:
        basis = (
            f"footprint NOT known -- a circle of {ring.radius_m:.1f} m assuming the building fits within "
            f"{ring.footprint_radius_m:g} m of its point location"
        )
    lines = [f"Perimeter inspection reference points ({len(ring.points)} points, clockwise, about "
             f"{ring.spacing_m:.0f} m apart; {basis}):"]
    for i, d in enumerate(ring.details or [RingPoint(lat, lon, ring.standoff_m) for lat, lon in ring.points], start=1):
        extra = ""
        if d.blocked:
            extra = "  [BLOCKED: next to a building of unknown height]"
        elif d.min_altitude_m is not None:
            extra = f"  [altitude at least {d.min_altitude_m:g} m here]"
        lines.append(f"  P{i}: latitude {d.latitude:.7f}, longitude {d.longitude:.7f}{extra}")
    instruction = (
        "For a perimeter inspection, use these points, in order, as the WAYPOINT coordinates "
        "(after TAKEOFF, before RETURN_TO_LAUNCH). Do not place waypoints closer to the building."
    )
    if altitude_hint_m is not None:
        instruction += f" Use altitude {altitude_hint_m:g} m unless the request says otherwise or a point above says higher."
    if any(d.min_altitude_m is not None for d in ring.details):
        instruction += " Where a point says 'altitude at least', its waypoint MUST use at least that altitude."
    if ring.neighbours:
        instruction += f" {len(ring.neighbours)} neighbouring building(s) are mapped nearby; the points above already avoid them."
    lines.append(instruction)
    return "\n".join(lines)
