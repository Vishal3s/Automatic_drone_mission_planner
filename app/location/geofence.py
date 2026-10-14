"""
Real-world coordinate & building resolution improvement -- small,
dependency-free geometry helpers, plus the new location-specific safety
check the spec's section 10 asks for:

    Every resolved coordinate and every generated waypoint MUST pass
    through the existing safety-validation system. Do NOT bypass the
    existing safety validator. Where appropriate, add location-specific
    validation for: valid latitude/longitude, permitted operating area,
    campus geofence, building boundary, waypoint/building relationship...

This module does NOT replace or modify `app.mission.validator` (Phase 5)
or `app.replanning.safety_validator` (Phase 7) -- both keep running
completely unchanged, and Phase 5's `validate_mission()` in particular
still runs FIRST on every generated mission (see
`app/mission/location_aware_generator.py`). What's here is a genuinely
NEW check neither of those phases could have had: "does this mission's
coordinates actually correspond to the real-world location the user
asked to inspect" -- meaningless before this improvement existed (there
was no verified real-world location to check against), so it isn't a
gap in Phase 5/7, it's new information those phases never had.

`point_in_bbox`/`haversine_km` reimplement (rather than import) the same
kind of primitives `app.replanning.replanner`'s `_haversine_km` and
`app.replanning.context_builder`'s `CircularGeofence` already use --
duplicated deliberately: those are module-private helpers in phases this
improvement must not modify, and the formula is a two-line, well-known
one (not meaningfully "shared logic" worth an awkward cross-module
private import).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import atan2, cos, radians, sin, sqrt

from app.mission.schema import Mission

_EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km -- the same well-known formula used elsewhere in this project."""
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * atan2(sqrt(a), sqrt(1 - a))


def point_in_bbox(latitude: float, longitude: float, bounding_box: tuple[float, float, float, float]) -> bool:
    """`bounding_box` is (south, north, west, east) -- this project's own convention (matches Nominatim's own field order)."""
    south, north, west, east = bounding_box
    return south <= latitude <= north and west <= longitude <= east


def point_in_polygon(latitude: float, longitude: float, polygon: list[tuple[float, float]]) -> bool:
    """
    Standard ray-casting point-in-polygon test, treating (lat, lon) as
    plain 2D coordinates -- a well-known, documented simplification that
    is accurate enough at building/campus scale (a few hundred meters),
    not a geodesic polygon library (this project deliberately avoids
    adding a GIS dependency like shapely for one small check -- see this
    project's standing "avoid introducing unnecessary technologies" rule).
    `polygon` is a list of (lat, lon) points, open or closed (the first
    point is implicitly connected back to the last).
    """
    if len(polygon) < 3:
        return False
    inside = False
    n = len(polygon)
    x, y = longitude, latitude
    for i in range(n):
        x1, y1 = polygon[i][1], polygon[i][0]
        x2, y2 = polygon[(i + 1) % n][1], polygon[(i + 1) % n][0]
        if (y1 > y) != (y2 > y):
            x_intersect = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_intersect:
                inside = not inside
    return inside


@dataclass
class LocationValidationResult:
    """What `validate_mission_against_location()` always returns -- same shape/spirit as `ValidationResult` (Phase 5)."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


def validate_mission_against_location(mission: Mission, resolved_location, *, radius_km: float) -> LocationValidationResult:
    """
    Checks every coordinate-bearing item (WAYPOINT/LAND/LOITER) in
    `mission` against the resolved real-world location:
      1. Coordinates are structurally valid lat/lon (already guaranteed
         by `MissionItem`'s own Pydantic field constraints -- re-checked
         here anyway so this function is self-contained and its
         docstring's promise ("valid latitude/longitude") is literally
         true regardless of what upstream already enforced).
      2. If the resolved location has a building polygon
         (`geometry_available`), each item must fall either inside that
         polygon OR within `radius_km` of it (a real inspection flies
         AROUND a building's perimeter, not only inside its footprint --
         see this project's spec section 5, "standoff distance").
      3. Otherwise (a point-only resolution), each item must be within
         `radius_km` of that point.
    TAKEOFF and bare RETURN_TO_LAUNCH items (no coordinates) are skipped
    -- there's nothing to check.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for item in mission.mission_items:
        if item.latitude is None or item.longitude is None:
            continue
        if not (-90.0 <= item.latitude <= 90.0 and -180.0 <= item.longitude <= 180.0):
            errors.append(f"Item #{item.sequence} ({item.command.value}) has an out-of-range coordinate.")
            continue

        if resolved_location.geometry_available and resolved_location.geometry:
            if point_in_polygon(item.latitude, item.longitude, resolved_location.geometry):
                continue
            distance_km = min(
                haversine_km(item.latitude, item.longitude, plat, plon)
                for plat, plon in resolved_location.geometry
            )
        else:
            distance_km = haversine_km(
                item.latitude, item.longitude, resolved_location.latitude, resolved_location.longitude
            )

        if distance_km > radius_km:
            errors.append(
                f"Item #{item.sequence} ({item.command.value}) is {distance_km:.3f}km from the resolved "
                f"location ({resolved_location.building or resolved_location.campus or resolved_location.institution}) "
                f"-- outside the {radius_km}km permitted operating area."
            )

    return LocationValidationResult(valid=not errors, errors=errors, warnings=warnings)
