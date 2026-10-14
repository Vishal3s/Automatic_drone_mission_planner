"""
Camera orientation for a building inspection, from the REAL outline.

After the planner has produced (and Phase 5 has accepted) a mission, every
WAYPOINT/LOITER that is part of the inspection -- i.e. within
`settings.inspection_camera_max_distance_m` of the building's outline --
gets, deterministically:

- yaw_deg: the heading from that waypoint to the NEAREST point on the
  building's outline (so the camera faces the wall in front of it, not
  the building's centre, which matters for long buildings). Point-only
  targets: towards the point.
- gimbal_pitch_deg: aimed at the facade's mid-height when the building's
  height is known from the project documents (BuildingFacts), else level
  (0 deg) -- no height is guessed.
- take_photo: True.

Nothing here moves a waypoint or changes an altitude, so it cannot make a
validated mission unsafe. PX4 note: the nose follows the mission's yaw
only with parameter MIS_YAWMODE = 0 ("heading as set by mission"); with
the default it points towards the next waypoint. A gimbal angle needs a
gimbal (the default SITL quadcopter has none -- PX4 then ignores it).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan2, degrees, hypot

from app.config import settings
from app.location.inspection_geometry import _Local, _rings
from app.mission.schema import Mission, MissionCommand


@dataclass
class CameraAim:
    sequence: int
    yaw_deg: float
    gimbal_pitch_deg: float
    distance_m: float               # horizontal distance to the wall it faces
    aim_height_m: float | None      # None = height unknown -> camera level

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence, "yaw_deg": round(self.yaw_deg, 1),
            "gimbal_pitch_deg": round(self.gimbal_pitch_deg, 1), "distance_m": round(self.distance_m, 1),
            "aim_height_m": self.aim_height_m,
        }


def _nearest_on_outline(x: float, y: float, rings_xy) -> tuple[float, float]:
    best, best_d = None, float("inf")
    for ring in rings_xy:
        for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]):
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg2))
            px, py = ax + t * dx, ay + t * dy
            d = hypot(px - x, py - y)
            if d < best_d:
                best, best_d = (px, py), d
    return best


def orient_camera(mission: Mission, resolved_location, facts=None) -> tuple[Mission, list[CameraAim]]:
    """A copy of `mission` with yaw/gimbal/photo set on its inspection waypoints, plus what was set."""
    local = _Local(resolved_location.latitude, resolved_location.longitude)
    rings_xy = [[local.xy(lat, lon) for lat, lon in r] for r in _rings(resolved_location) if len(r) >= 3]
    if not rings_xy:
        rings_xy = [[(0.0, 0.0)]]  # point-only: aim at the point
    height = getattr(facts, "height_m", None) if facts is not None else None
    aim_h = height / 2.0 if height else None
    limit = float(settings.inspection_camera_max_distance_m)

    items, aims, altitude = [], [], None
    for item in mission.mission_items:
        if item.altitude is not None:
            altitude = item.altitude
        if item.command in (MissionCommand.WAYPOINT, MissionCommand.LOITER) and item.latitude is not None and item.longitude is not None:
            x, y = local.xy(item.latitude, item.longitude)
            tx, ty = _nearest_on_outline(x, y, rings_xy) if len(rings_xy[0]) > 1 else rings_xy[0][0]
            dist = hypot(tx - x, ty - y)
            if 0.5 < dist <= limit:
                yaw = (degrees(atan2(tx - x, ty - y)) + 360.0) % 360.0
                pitch = 0.0
                if aim_h is not None and altitude is not None:
                    pitch = max(-90.0, min(0.0, -degrees(atan2(altitude - aim_h, dist))))
                item = item.model_copy(update={"yaw_deg": round(yaw, 1) % 360.0, "gimbal_pitch_deg": round(pitch, 1), "take_photo": True})
                aims.append(CameraAim(item.sequence, item.yaw_deg, item.gimbal_pitch_deg, dist, aim_h))
        items.append(item)
    return mission.model_copy(update={"mission_items": items}), aims
