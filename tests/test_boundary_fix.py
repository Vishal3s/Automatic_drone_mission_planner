"""
Boundary fix -- the inspection ring follows the building's outline and
avoids neighbouring buildings; the safety check judges every flight leg.
No network: outlines are built in local metres around a fixed point.
"""

from math import cos, radians

import pytest

from app.location.building_footprints import BuildingFootprint
from app.location.geofence import haversine_km, point_in_polygon
from app.location.inspection_geometry import compute_inspection_ring, render_inspection_ring
from app.location.location_safety import validate_flight_path_obstacles, validate_location_mission_safety
from app.location.models import LocationConfidence, LocationSource, MatchDetails, ResolutionType, ResolvedLocation
from app.location.obstacles import Obstacle, fetch_neighbour_obstacles, neighbour_obstacles, obstacle_height
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.ui.mission_view import _region

LAT0, LON0 = 12.8406, 80.1534
_M = 111_320.0


def ll(x, y):
    """Local metres (x east, y north) -> (lat, lon)."""
    return LAT0 + y / _M, LON0 + x / (_M * cos(radians(LAT0)))


def rect(x0, y0, x1, y1):
    return [ll(x0, y0), ll(x1, y0), ll(x1, y1), ll(x0, y1)]


# A long, narrow building: 200 m east-west, 20 m north-south.
LONG = rect(-100, -10, 100, 10)


def location(polygon=LONG, name="Long Block"):
    return ResolvedLocation(
        institution="VIT", campus="VIT Chennai", building=name,
        latitude=LAT0, longitude=LON0,
        resolution_type=ResolutionType.BUILDING if polygon else ResolutionType.POINT,
        source=LocationSource.OPENSTREETMAP, confidence=LocationConfidence.HIGH,
        geometry_available=polygon is not None, geometry=polygon, match_details=MatchDetails(),
    )


def fp(polygon, osm_id, **tags):
    return BuildingFootprint(osm_id=osm_id, rings=[polygon], tags=tags)


def obstacle(polygon, osm_id="way/9", height=None, name="Hostel"):
    tags = {"name": name}
    if height is not None:
        tags["height"] = str(height)
    h, src = obstacle_height(tags)
    return Obstacle(footprint=fp(polygon, osm_id, **tags), height_m=h, height_source=src)


def mission(points, altitude):
    items = [MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=altitude)]
    for i, (lat, lon) in enumerate(points, start=2):
        items.append(MissionItem(sequence=i, command=MissionCommand.WAYPOINT, latitude=lat, longitude=lon, altitude=altitude))
    items.append(MissionItem(sequence=len(items) + 1, command=MissionCommand.RETURN_TO_LAUNCH))
    return Mission(mission_name="test", mission_items=items)


# --------------------------------------------------------------- ring shape


def test_ring_follows_a_long_building_instead_of_a_big_circle():
    ring = compute_inspection_ring(location(), standoff_m=15)
    outline = fp(LONG, "way/1")
    assert ring.shape == "outline"
    for lat, lon in ring.points:
        assert not point_in_polygon(lat, lon, LONG)
        assert outline.distance_m(lat, lon) == pytest.approx(15.0, abs=0.5)
    # The old circle would have put every point ~115 m from the centre; the long sides now stay 25 m from it.
    nearest = min(haversine_km(LAT0, LON0, lat, lon) * 1000 for lat, lon in ring.points)
    assert nearest < 30


def test_point_count_grows_with_perimeter_and_is_capped():
    ring = compute_inspection_ring(location(), standoff_m=15)
    assert 8 < len(ring.points) <= 24
    small = compute_inspection_ring(location(rect(-10, -10, 10, 10)), standoff_m=15)
    assert len(small.points) == 8


def test_points_start_northmost_and_run_clockwise():
    ring = compute_inspection_ring(location(rect(-20, -20, 20, 20)), standoff_m=15, n_points=8)
    lats = [p[0] for p in ring.points]
    assert lats[0] == pytest.approx(max(lats), abs=1e-6)
    # Clockwise from north: the second point is east of the first.
    assert ring.points[1][1] > ring.points[0][1]


def test_point_only_keeps_the_assumed_circle():
    ring = compute_inspection_ring(location(None), standoff_m=15, n_points=6)
    assert ring.shape == "circle" and not ring.footprint_measured
    assert ring.radius_m == pytest.approx(75.0)


# --------------------------------------------------------------- neighbours


def test_point_near_neighbour_is_pulled_in_but_not_below_min_standoff():
    # Neighbour 18 m north of the long building: a 15 m standoff leaves only 3 m -> pulled in.
    north = obstacle(rect(-100, 28, 100, 48), height=12)
    ring = compute_inspection_ring(location(), standoff_m=15, neighbours=[north])
    pulled = [d for d in ring.details if d.note and "pulled in" in d.note]
    assert pulled
    for d in ring.details:
        assert d.standoff_m >= 8.0
        assert north.distance_m(d.latitude, d.longitude) > 5.0 or d.min_altitude_m is not None
    assert not ring.blocked


def test_point_too_close_to_tall_neighbour_gets_minimum_altitude():
    # Gap only 10 m: no standoff >= 8 m keeps 5 m from it -> must overfly it (12 m + 10 m clearance).
    north = obstacle(rect(-100, 20, 100, 40), height=12)
    ring = compute_inspection_ring(location(), standoff_m=15, neighbours=[north])
    over = [d for d in ring.details if d.min_altitude_m is not None]
    assert over and all(d.min_altitude_m == pytest.approx(22.0) for d in over)
    assert not ring.blocked
    text = render_inspection_ring(ring, altitude_hint_m=15)
    assert "altitude at least 22 m" in text


def test_neighbour_of_unknown_height_blocks_the_point(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "neighbour_unknown_height_blocks", True)
    north = obstacle(rect(-100, 20, 100, 40), height=None)
    ring = compute_inspection_ring(location(), standoff_m=15, neighbours=[north])
    assert ring.blocked
    assert "BLOCKED" in render_inspection_ring(ring)


def test_heights_from_osm_tags():
    assert obstacle_height({"height": "12 m"}) == (12.0, "OSM height tag")
    h, src = obstacle_height({"building:levels": "4"})
    assert h == pytest.approx(14.0) and src.startswith("estimated")
    assert obstacle_height({}) == (None, "unknown")


def test_target_outline_is_not_its_own_neighbour():
    target = fp(LONG, "way/1", name="Long Block")
    other = fp(rect(-100, 40, 100, 60), "way/2", name="Hostel")
    found = neighbour_obstacles([target, other], location())
    assert [o.footprint.osm_id for o in found] == ["way/2"]


def test_lookup_failure_is_reported_not_treated_as_none():
    from app.location.geocoding_service import GeocodingConnectionError

    class Down:
        def footprints_near(self, *a, **k):
            raise GeocodingConnectionError("busy")

    obstacles, warning = fetch_neighbour_obstacles(Down(), location(), 200)
    assert obstacles is None and "NOT checked" in warning
    obstacles, warning = fetch_neighbour_obstacles(None, location(), 200)
    assert obstacles is None and warning


# --------------------------------------------------------------- flight-path check


def test_leg_through_neighbour_below_its_height_is_rejected():
    hostel = obstacle(rect(-10, 40, 10, 60), height=20)
    route = [ll(-40, 50), ll(40, 50)]  # straight through the hostel, both ends outside it
    low = validate_flight_path_obstacles(mission(route, 15), location(), None, [hostel])
    assert not low.valid and "Hostel" in low.errors[0] and "30 m" in low.errors[0]
    high = validate_flight_path_obstacles(mission(route, 35), location(), None, [hostel])
    assert high.valid


def test_neighbour_with_unknown_height_is_rejected_at_any_altitude(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "neighbour_unknown_height_blocks", True)
    hut = obstacle(rect(-10, 40, 10, 60), height=None, name="Hut")
    result = validate_flight_path_obstacles(mission([ll(-40, 50), ll(40, 50)], 100), location(), None, [hut])
    assert not result.valid and "not mapped" in result.errors[0]


def test_neighbours_not_checked_is_a_warning():
    result = validate_flight_path_obstacles(mission([ll(-40, 50)], 30), location(), None, None)
    assert result.valid and "NOT checked" in result.warnings[0]


def test_leg_cutting_across_the_target_below_its_roof_is_rejected():
    from app.location.building_knowledge import BuildingFacts

    facts = BuildingFacts(building="Long Block", height_m=36.0, height_source="test.pdf", height_page=1)
    route = [ll(0, -30), ll(0, 30)]  # south side to north side, straight over the building
    result = validate_flight_path_obstacles(mission(route, 20), location(), facts, [])
    assert not result.valid and "cuts across" in result.errors[0]
    ok = validate_flight_path_obstacles(mission(route, 50), location(), facts, [])
    assert ok.valid


def test_generated_ring_passes_the_flight_path_check():
    north = obstacle(rect(-100, 20, 100, 40), height=12)
    ring = compute_inspection_ring(location(), standoff_m=15, neighbours=[north])
    result = validate_location_mission_safety(
        mission(ring.points, 25), location(), None, radius_km=1.0, neighbours=[north], check_obstacles=True,
    )
    assert result.valid, result.errors


def test_map_region_carries_the_ring_path_and_neighbours():
    north = obstacle(rect(-100, 30, 100, 50), height=12)
    region = _region(compute_inspection_ring(location(), standoff_m=15, neighbours=[north]))
    assert region["type"] == "outline" and len(region["path"]) > 8
    assert region["neighbours_checked"] and region["neighbours"][0]["height_m"] == 12.0


def test_unknown_height_neighbour_is_only_a_warning_by_default():
    hut = obstacle(rect(-10, 40, 10, 60), height=None, name="Hut")
    result = validate_flight_path_obstacles(mission([ll(-40, 50), ll(40, 50)], 20), location(), None, [hut])
    assert result.valid and any("not mapped" in w for w in result.warnings)
    ring = compute_inspection_ring(location(), standoff_m=15, neighbours=[obstacle(rect(-100, 20, 100, 40), height=None)])
    assert not ring.blocked
