"""
Structural scan -- one building flown as stacked rings up its facade.
No network: the outline is built in local metres around a fixed point.
"""

from math import cos, radians

import pytest

from app.config import settings
from app.location.building_knowledge import BuildingFacts
from app.location.models import LocationConfidence, LocationSource, MatchDetails, ResolutionType, ResolvedLocation
from app.mission.intent_parser import ScanMode, detect_scan_mode
from app.mission.schema import MissionCommand
from app.mission.structural_scan import StructuralScanError, plan_structural_scan
from app.mission.validator import validate_mission

LAT0, LON0 = 12.8406, 80.1534
_M = 111_320.0


def ll(x, y):
    return LAT0 + y / _M, LON0 + x / (_M * cos(radians(LAT0)))


def rect(x0, y0, x1, y1):
    return [ll(x0, y0), ll(x1, y0), ll(x1, y1), ll(x0, y1)]


def location(polygon=None, name="Ripon Building"):
    return ResolvedLocation(
        institution="VIT", campus="VIT Chennai", building=name,
        latitude=LAT0, longitude=LON0,
        resolution_type=ResolutionType.BUILDING if polygon else ResolutionType.POINT,
        source=LocationSource.OPENSTREETMAP, confidence=LocationConfidence.HIGH,
        geometry_available=polygon is not None, geometry=polygon, match_details=MatchDetails(),
    )


BOX = rect(-30, -15, 30, 15)  # 60 m x 30 m block


def test_structural_keyword_is_detected():
    assert detect_scan_mode("Structural scan of Ripon Building at 40 meters") is ScanMode.STRUCTURAL
    assert detect_scan_mode("Inspect AB1 of VIT Chennai at 40 meters") is None


def test_known_height_sets_the_top_and_levels_climb_to_it():
    facts = BuildingFacts(building="Ripon Building", height_m=30.0)
    plan = plan_structural_scan(location(BOX), facts)
    assert plan.levels_m[0] == settings.structural_base_altitude_m
    assert plan.levels_m[-1] == pytest.approx(30.0 - settings.structural_top_margin_m)
    assert len(plan.levels_m) >= 2


def test_mission_is_rule_valid_and_climbs_through_every_level():
    facts = BuildingFacts(building="Ripon Building", height_m=30.0)
    plan = plan_structural_scan(location(BOX), facts)
    assert validate_mission(plan.mission).valid
    cmds = [i.command for i in plan.mission.mission_items]
    assert cmds[0] is MissionCommand.TAKEOFF and cmds[-1] is MissionCommand.RETURN_TO_LAUNCH
    altitudes = {round(i.altitude, 1) for i in plan.mission.mission_items if i.command is MissionCommand.WAYPOINT}
    for level in plan.levels_m:
        assert level in altitudes


def test_every_waypoint_is_photographed_with_a_facade_tilt():
    facts = BuildingFacts(building="Ripon Building", height_m=30.0)
    plan = plan_structural_scan(location(BOX), facts)
    photos = [i for i in plan.mission.mission_items if i.take_photo]
    assert photos and all(i.gimbal_pitch_deg == settings.structural_gimbal_pitch_deg for i in photos)
    assert all(i.yaw_deg is not None for i in photos)


def test_unknown_height_uses_requested_altitude_and_warns():
    plan = plan_structural_scan(location(BOX), None, requested_altitude_m=25.0)
    assert plan.levels_m[-1] == 25.0
    assert any("not known" in w for w in plan.warnings)


def test_unknown_height_without_request_warns_about_the_default():
    plan = plan_structural_scan(location(BOX), None)
    assert plan.levels_m[-1] == settings.structural_default_height_m
    assert any("assumed" in w for w in plan.warnings)


def test_no_outline_plans_an_assumed_footprint_and_warns():
    plan = plan_structural_scan(location(None), BuildingFacts(building="x", height_m=30.0))
    assert validate_mission(plan.mission).valid
    assert any("ASSUMED footprint" in w for w in plan.warnings)


def test_level_count_is_capped():
    tall = BuildingFacts(building="x", height_m=95.0)
    plan = plan_structural_scan(location(BOX), tall)
    assert len(plan.levels_m) <= settings.structural_max_levels


def test_waypoints_fit_the_mission_limit():
    facts = BuildingFacts(building="x", height_m=95.0)
    plan = plan_structural_scan(location(BOX), facts)
    waypoints = [i for i in plan.mission.mission_items if i.command is MissionCommand.WAYPOINT]
    assert len(plan.mission.mission_items) <= settings.max_waypoints
    assert len(waypoints) >= 6 * len(plan.levels_m) or len(waypoints) == plan.points_per_level * len(plan.levels_m)


def test_alternating_rings_do_not_cross_back():
    facts = BuildingFacts(building="x", height_m=30.0)
    plan = plan_structural_scan(location(BOX), facts)
    wps = [i for i in plan.mission.mission_items if i.command is MissionCommand.WAYPOINT]
    n = plan.points_per_level
    first_of_level_2 = wps[n]          # starts ring 2, which runs in reverse
    last_of_level_1 = wps[n - 1]       # ring 1 ends where ring 2 starts
    assert (first_of_level_2.latitude, first_of_level_2.longitude) == (last_of_level_1.latitude, last_of_level_1.longitude)
