"""
Structural scan -- the building is flown as several rings stacked in altitude.

Requested as e.g. "Structural scan of Ripon Building, Chennai". Where the
perimeter inspection flies ONE ring around a building, a structural scan
flies a ring at each of several levels, from a base altitude up to the top
of the facade, so every storey of every wall is photographed.

Built deterministically (no LLM): the geometry is fully known from the real
outline, so a language model adds no value here and only adds risk. The
result goes through the SAME checks as every other mission: Phase 5 rule
validation (`validate_mission`) and the location safety gates
(`validate_location_mission_safety`), run by location_aware_generator.

Plan:
- Top of the facade = mapped building height - `structural_top_margin_m`.
  Height from the project documents (BuildingFacts) or the outline's
  mapped height. If neither is known, the requested altitude is used, or
  `structural_default_height_m` -- and that is reported as a warning.
- Levels evenly spaced from `structural_base_altitude_m` to the top, at most
  one every `structural_level_spacing_m`, at most `structural_max_levels`.
- Each level is one full ring at the inspection standoff. Rings alternate
  direction so the drone never has to cross back over the start.
- A ring point next to another building is raised to that building's
  clearance altitude (from the ring's own checks). A point flagged BLOCKED
  (next to a building of unknown height) stops the scan -- it is never flown.
- Total waypoints must fit `max_waypoints`; the points per ring shrink (or
  levels drop) to fit, and the reduction is reported.
- The camera faces the nearest wall at each waypoint with a fixed, slightly
  downward gimbal tilt, so the facade stays in frame at every level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from math import ceil

from app.config import settings
from app.location.inspection_geometry import compute_inspection_ring
from app.mission.inspection_camera import CameraAim, orient_camera
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.validator import validate_mission

log = logging.getLogger(__name__)


class StructuralScanError(ValueError):
    """The structural scan cannot be planned safely for this building -- the reason is the message."""


@dataclass
class StructuralPlan:
    mission: Mission
    levels_m: list[float]           # altitude of each level, bottom to top
    points_per_level: int
    height_m: float | None          # building height used for the top (None = assumed/requested)
    height_source: str              # where the top came from
    camera_aims: list[CameraAim] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _top_altitude(height_m: float | None, requested_alt: float | None, warnings: list[str]) -> tuple[float, str]:
    margin = float(settings.structural_top_margin_m)
    if height_m is not None:
        return height_m - margin, "building height"
    if requested_alt is not None:
        warnings.append(
            f"Building height not known -- scanning up to the requested {requested_alt:g} m. "
            "Check the top of the facade by eye before flying."
        )
        return float(requested_alt), "requested altitude (height unknown)"
    warnings.append(
        f"Building height not known -- scanning up to an assumed {settings.structural_default_height_m:g} m. "
        "Check the top of the facade by eye before flying."
    )
    return float(settings.structural_default_height_m), "assumed height (unknown)"


def _levels(base: float, top: float, count: int | None = None) -> list[float]:
    if count is None:
        spacing = float(settings.structural_level_spacing_m)
        count = int(ceil((top - base) / spacing)) + 1
    count = max(2, min(int(settings.structural_max_levels), count))
    step = (top - base) / (count - 1)
    return [round(base + step * k, 1) for k in range(count)]


def plan_structural_scan(
    location,
    facts=None,
    *,
    neighbours: list | None = None,
    start_near: tuple[float, float] | None = None,
    requested_altitude_m: float | None = None,
    name: str | None = None,
) -> StructuralPlan:
    """
    Plan the structural scan of one building. Raises `StructuralScanError`
    with a plain reason when it cannot be done safely.
    """
    warnings: list[str] = []
    if not location.geometry_available:
        warnings.append(
            "No building outline was found (map lookup unavailable) -- the rings use an ASSUMED footprint "
            "around the building's point. Confirm the walls' position by eye before flying."
        )

    height = getattr(facts, "height_m", None) if facts is not None else None
    height_source = "project documents" if height is not None else None
    if height is None:
        meta_height = (getattr(location, "geometry_meta", None) or {}).get("height_m")
        if meta_height is not None:
            height = float(meta_height)
            height_source = "outline height"

    base = float(settings.structural_base_altitude_m)
    top, source = _top_altitude(height, requested_altitude_m, warnings)
    if height_source and height is not None:
        source = height_source
    top = min(top, float(settings.max_altitude_m))
    if top <= base + float(settings.structural_level_spacing_m) * 0.5:
        raise StructuralScanError(
            f"The facade top ({top:g} m) is too close to the base altitude ({base:g} m) for a structural scan."
        )

    levels = _levels(base, top)
    budget = int(settings.max_waypoints) - 2  # TAKEOFF and RETURN_TO_LAUNCH are not ring points
    cap = int(settings.inspection_max_ring_points)
    while len(levels) > 2 and len(levels) * 6 > budget:
        levels = _levels(base, top, len(levels) - 1)
    points = min(cap, budget // len(levels))
    if points < 6:
        raise StructuralScanError(
            "The building has too many levels for one mission at this waypoint limit. Ask for a shorter scan."
        )
    if points < min(cap, budget // 2):
        warnings.append(f"Waypoint limit: {points} points per level, {len(levels)} levels.")

    ring = compute_inspection_ring(location, neighbours=neighbours, start_near=start_near, n_points=points)
    if ring.blocked:
        raise StructuralScanError(
            "Part of the route passes next to a building of unknown height "
            f"(ring point {', '.join('P' + str(i) for i in ring.blocked)}). It is not flown."
        )
    warnings.extend(ring.notes)

    items: list[MissionItem] = []
    seq = 1
    items.append(MissionItem(sequence=seq, command=MissionCommand.TAKEOFF, altitude=levels[0]))
    seq += 1

    details = ring.details
    for k, level_alt in enumerate(levels):
        order = list(range(len(details)))
        if k % 2 == 1:
            order = order[::-1]
        for idx in order:
            d = details[idx]
            alt = level_alt
            if d.min_altitude_m is not None:
                alt = max(alt, float(d.min_altitude_m))
            if alt > float(settings.max_altitude_m):
                raise StructuralScanError(
                    f"A neighbouring building needs about {alt:g} m at ring point P{idx + 1}, "
                    f"above the {settings.max_altitude_m:g} m limit. It is not flown."
                )
            items.append(MissionItem(
                sequence=seq, command=MissionCommand.WAYPOINT,
                latitude=round(d.latitude, 7), longitude=round(d.longitude, 7), altitude=round(alt, 1),
            ))
            seq += 1

    items.append(MissionItem(sequence=seq, command=MissionCommand.RETURN_TO_LAUNCH))
    mission = Mission(mission_name=name or f"Structural scan ({len(levels)} levels)", mission_items=items)

    mission, aims = orient_camera(mission, location, facts)
    pitch = float(settings.structural_gimbal_pitch_deg)
    photo_items = []
    for item in mission.mission_items:
        if item.take_photo:
            item = item.model_copy(update={"gimbal_pitch_deg": pitch})
        photo_items.append(item)
    mission = mission.model_copy(update={"mission_items": photo_items})
    aims = [CameraAim(a.sequence, a.yaw_deg, pitch, a.distance_m, a.aim_height_m) for a in aims]

    return StructuralPlan(
        mission=mission, levels_m=levels, points_per_level=points,
        height_m=height, height_source=source, camera_aims=aims, warnings=warnings,
    )
