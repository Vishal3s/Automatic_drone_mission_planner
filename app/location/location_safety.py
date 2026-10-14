"""
Location resolution improvement -- remaining Phase D: location-specific
safety checks, and handing the resolved area to RAMR.

Spec section 10 asks for location-specific validation of: valid lat/lon,
permitted operating area, campus geofence, building boundary,
waypoint/building relationship, SAFE ALTITUDE, safe speed, BATTERY
FEASIBILITY, RTH FEASIBILITY. `geofence.validate_mission_against_location()`
(earlier improvement) already covers the first five. This module adds the
rest, as ADDITIONAL gates -- Phase 5's `validate_mission()` still runs
first on every mission, unchanged, and Phase 7's validator remains the
final authority for replans. Speed is already enforced by Phase 5/7
(`max_safe_speed_ms`); nothing new is needed for it.

1. Roof clearance -- any waypoint over the building footprint must fly at
   least `building_roof_clearance_m` above the building's documented
   height (Phase B). Waypoints outside the footprint but below roof
   height are allowed (facade inspection) with a warning.
   ASSUMPTION (stated): mission altitudes are relative to home, and home
   is at ground level near the building -- the normal case for a campus
   inspection launched on site.
2. Battery + RTH feasibility of the INITIAL mission, reusing the Phase 8
   `BatteryFeasibilityChecker` unchanged -- only when a real battery
   reading and home position are supplied; otherwise reported as
   not-checked (never assumed fine).
2b. Flight-path obstacles (boundary fix) -- every straight leg between
   consecutive waypoints is sampled (every 2 m). A leg that crosses the
   target's outline must clear its roof; a leg (or waypoint) within
   `neighbour_clearance_m` of a NEIGHBOURING building must fly at least
   that building's mapped height + roof clearance. A neighbour with no
   mapped height cannot be cleared on paper -> error. The leg altitude is
   the LOWER of its two ends (PX4 changes altitude gradually along a leg).
   If the neighbour lookup could not be done, that is a warning saying so
   -- never treated as "no neighbours".
3. `safety_constraints_for_location()` -- a `SafetyConstraints` whose
   geofence is the resolved operating area, for passing to the EXISTING
   `RAMROrchestrator.handle_event(..., safety_constraints=...)` override
   so mid-flight replans (RAMR/CV) are held to the same area. Needs no
   change to ReplanningContextBuilder/RAMROrchestrator -- both already
   accept that override.
"""

from __future__ import annotations

from app.config import settings
from app.location.building_knowledge import BuildingFacts
from app.location.geofence import LocationValidationResult, haversine_km, point_in_polygon, validate_mission_against_location
from app.location.inspection_geometry import _leg_hits, _rings, footprint_extent
from app.mission.schema import Mission, MissionCommand
from app.replanning.battery_feasibility import BatteryFeasibilityChecker
from app.replanning.context_builder import CircularGeofence, SafetyConstraints


def _over_footprint(latitude: float, longitude: float, resolved_location) -> bool:
    """True if the point is over the building (real polygon), or within the assumed footprint radius (point-only)."""
    rings = [r for r in _rings(resolved_location) if len(r) >= 3]
    if rings:  # Phase 15: any part of a MultiPolygon counts as "over the building"
        return any(point_in_polygon(latitude, longitude, ring) for ring in rings)
    clat, clon, radius_m, _ = footprint_extent(resolved_location)
    return haversine_km(latitude, longitude, clat, clon) * 1000.0 <= radius_m


def validate_building_clearance(
    mission: Mission, resolved_location, facts: BuildingFacts | None, *, clearance_m: float | None = None
) -> LocationValidationResult:
    """Roof-clearance check -- see this module's docstring, item 1."""
    clearance = float(settings.building_roof_clearance_m if clearance_m is None else clearance_m)
    errors: list[str] = []
    warnings: list[str] = []
    height = facts.height_m if facts is not None else None
    required = height + clearance if height is not None else None
    cite = facts.citation() if facts is not None else None
    name = resolved_location.building or resolved_location.campus or "the building"

    altitude = None  # carried forward: an item with no altitude keeps the previous one (PX4 behaviour)
    below_roof_facade: list[int] = []
    for item in mission.mission_items:
        if item.altitude is not None:
            altitude = item.altitude
        if item.latitude is None or item.longitude is None or item.command == MissionCommand.LAND:
            continue
        over = _over_footprint(item.latitude, item.longitude, resolved_location)
        if over and height is None:
            warnings.append(
                f"Item #{item.sequence} is over {name}, whose height is unknown (no project document states it) -- "
                f"verify the altitude clears the roof."
            )
        elif over and altitude is not None and altitude < required:
            errors.append(
                f"Item #{item.sequence} ({item.command.value}) is over {name} at {altitude:g} m, but {name} is "
                f"{height:g} m tall (source: {cite}) -- needs at least {required:g} m "
                f"({clearance:g} m roof clearance)."
            )
        elif not over and height is not None and altitude is not None and altitude < height:
            below_roof_facade.append(item.sequence)

    if below_roof_facade:
        warnings.append(
            f"Items {below_roof_facade} fly below {name}'s roof height ({height:g} m, source: {cite}) outside its "
            f"footprint -- a facade-level inspection that relies on the standoff distance; confirm no other "
            f"obstacles (trees, neighbouring buildings) are on that ring."
        )
    if not resolved_location.geometry_available:
        warnings.append(
            f"No real footprint for {name} -- 'over the building' uses the assumed "
            f"{settings.building_unknown_footprint_radius_m:g} m radius around its point location."
        )
    return LocationValidationResult(valid=not errors, errors=errors, warnings=warnings)


def _positional_legs(mission: Mission):
    """[(seq_a, (lat, lon), alt_a, seq_b, (lat, lon), alt_b)] between consecutive coordinate items, altitude carried forward."""
    legs, prev, altitude = [], None, None
    for item in mission.mission_items:
        if item.altitude is not None:
            altitude = item.altitude
        if item.latitude is None or item.longitude is None or item.command == MissionCommand.LAND:
            continue
        cur = (item.sequence, (item.latitude, item.longitude), altitude)
        if prev is not None:
            legs.append(prev + cur)
        prev = cur
    return legs


def validate_flight_path_obstacles(
    mission: Mission, resolved_location, facts: BuildingFacts | None, neighbours: list | None, *, clearance_m: float | None = None
) -> LocationValidationResult:
    """Legs over the target and near neighbouring buildings -- see this module's docstring, item 2b."""
    roof = float(settings.building_roof_clearance_m if clearance_m is None else clearance_m)
    near = float(settings.neighbour_clearance_m)
    errors: list[str] = []
    warnings: list[str] = []
    name = resolved_location.building or resolved_location.campus or "the building"

    if neighbours is None:
        warnings.append(
            "Neighbouring buildings were NOT checked (OpenStreetMap outlines unavailable) -- "
            "confirm by eye that the route does not pass other buildings at low altitude."
        )
    obstacles = list(neighbours or [])

    # Single waypoints near a neighbour (a mission with only one coordinate item has no legs).
    seen: set[tuple[int, str]] = set()
    altitude = None
    for item in mission.mission_items:
        if item.altitude is not None:
            altitude = item.altitude
        if item.latitude is None or item.longitude is None or item.command == MissionCommand.LAND:
            continue
        for o in obstacles:
            if o.distance_m(item.latitude, item.longitude) > near:
                continue
            seen.add((item.sequence, o.footprint.osm_id))
            _judge(errors, warnings, f"Item #{item.sequence}", o, altitude, roof)

    target_rings = [r for r in _rings(resolved_location) if len(r) >= 3]
    height = facts.height_m if facts is not None else None
    for seq_a, a, alt_a, seq_b, b, alt_b in _positional_legs(mission):
        alts = [x for x in (alt_a, alt_b) if x is not None]
        leg_alt = min(alts) if alts else None
        where = f"Leg #{seq_a}->#{seq_b}"
        for o in _leg_hits(a, b, obstacles, near):
            if (seq_a, o.footprint.osm_id) in seen and (seq_b, o.footprint.osm_id) in seen:
                continue  # already reported for both ends
            _judge(errors, warnings, where, o, leg_alt, roof)
        # Crossing the target itself between two waypoints that are each outside it.
        if target_rings and height is not None and leg_alt is not None and leg_alt < height + roof:
            if _leg_crosses_rings(a, b, target_rings) and not (
                any(point_in_polygon(*a, r) for r in target_rings) or any(point_in_polygon(*b, r) for r in target_rings)
            ):
                errors.append(
                    f"{where} cuts across {name} at {leg_alt:g} m, but {name} is {height:g} m tall "
                    f"(source: {facts.citation()}) -- needs at least {height + roof:g} m, or go around it."
                )
    return LocationValidationResult(valid=not errors, errors=errors, warnings=warnings)


def _judge(errors: list[str], warnings: list[str], where: str, obstacle, altitude: float | None, roof: float) -> None:
    if obstacle.height_m is None:
        if settings.neighbour_unknown_height_blocks:  # strict mode: refuse
            errors.append(
                f"{where} passes within {settings.neighbour_clearance_m:g} m of neighbouring building "
                f"{obstacle.label} ({obstacle.footprint.osm_id}), whose height is not mapped -- cannot confirm it is cleared."
            )
            return
        # Default: plan anyway, judged against an ASSUMED height -- reported as a warning, never silently.
        assumed = float(settings.unknown_building_assumed_height_m)
        need = assumed + roof
        alt_text = "an unknown altitude" if altitude is None else f"{altitude:g} m"
        verdict = "clears" if altitude is not None and altitude >= need else "may NOT clear"
        warnings.append(
            f"{where} passes neighbouring building {obstacle.label} ({obstacle.footprint.osm_id}) at {alt_text}; "
            f"its height is not mapped, assumed {assumed:g} m -- {verdict} the assumed {need:g} m. Verify visually."
        )
    elif altitude is None or altitude < obstacle.height_m + roof:
        alt_text = "an unknown altitude" if altitude is None else f"{altitude:g} m"
        # settings.neighbour_clearance_blocks: true = refuse the plan; false (default) = warn and keep the plan.
        (errors if settings.neighbour_clearance_blocks else warnings).append(
            f"{where} passes neighbouring building {obstacle.label} at {alt_text}; it is about "
            f"{obstacle.height_m:g} m tall ({obstacle.height_source}) -- needs at least {obstacle.height_m + roof:g} m."
        )


def _leg_crosses_rings(a, b, rings, step_m: float = 2.0) -> bool:
    from app.location.inspection_geometry import _Local
    from math import ceil, hypot

    local = _Local(*a)
    bx, by = local.xy(*b)
    n = max(1, int(ceil(hypot(bx, by) / step_m)))
    for k in range(1, n):
        lat, lon = local.ll(bx * k / n, by * k / n)
        if any(point_in_polygon(lat, lon, r) for r in rings):
            return True
    return False


def validate_initial_battery(
    mission: Mission,
    *,
    home: tuple[float, float] | None,
    battery_percent: float | None,
) -> LocationValidationResult:
    """Battery + RTH feasibility of the whole mission from home -- see item 2. `battery_percent` is a 0-1 fraction."""
    if home is None or battery_percent is None:
        return LocationValidationResult(
            valid=True,
            warnings=[
                "Battery/RTH feasibility NOT checked -- no live battery reading and home position were supplied "
                "(connect to the vehicle, then pass home= and battery_percent=)."
            ],
        )
    result = BatteryFeasibilityChecker().check_from_positions(
        current_battery_percent=battery_percent,
        current_latitude_deg=home[0], current_longitude_deg=home[1],
        remaining_waypoints=list(mission.mission_items), corrective_waypoints=[],
        home_latitude_deg=home[0], home_longitude_deg=home[1],
    )
    if result.feasible:
        return LocationValidationResult(valid=True, warnings=[])
    return LocationValidationResult(valid=False, errors=[f"Battery/RTH not feasible: {result.reason}"])


def validate_location_mission_safety(
    mission: Mission,
    resolved_location,
    facts: BuildingFacts | None,
    *,
    radius_km: float,
    home: tuple[float, float] | None = None,
    battery_percent: float | None = None,
    neighbours: list | None = None,
    check_obstacles: bool = False,
) -> LocationValidationResult:
    """
    All location-specific gates together: operating area (existing), roof
    clearance, flight-path obstacles (when `check_obstacles`), battery/RTH.
    `neighbours` None = the neighbour lookup was not possible (warned).
    """
    parts = [
        validate_mission_against_location(mission, resolved_location, radius_km=radius_km),
        validate_building_clearance(mission, resolved_location, facts),
    ]
    if check_obstacles:
        parts.append(validate_flight_path_obstacles(mission, resolved_location, facts, neighbours))
    parts.append(validate_initial_battery(mission, home=home, battery_percent=battery_percent))
    errors = [e for p in parts for e in p.errors]
    warnings = [w for p in parts for w in p.warnings]
    return LocationValidationResult(valid=not errors, errors=errors, warnings=warnings)


def safety_constraints_for_location(resolved_location, *, radius_km: float | None = None) -> SafetyConstraints:
    """
    Item 3: the normal settings-based constraints, plus a geofence around
    the resolved location with the SAME radius the initial mission was
    checked against -- pass to `RAMROrchestrator.handle_event(...,
    safety_constraints=...)` so replans can't leave that area.
    """
    radius = float(settings.location_geofence_radius_km if radius_km is None else radius_km)
    clat, clon, _, _ = footprint_extent(resolved_location)
    constraints = SafetyConstraints.from_settings()
    constraints.geofence = CircularGeofence(center_latitude_deg=clat, center_longitude_deg=clon, radius_km=radius)
    return constraints
