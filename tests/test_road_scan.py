"""
Road scan -- fly along a named road. No network: roads are synthetic.
"""

from math import cos, radians

import pytest

from app.config import settings
from app.mission.intent_parser import ScanMode, detect_scan_mode
from app.mission.road_scan import RoadScanError, RoadWay, names_match, pick_named_road, plan_road_scan, road_name_from_address
from app.mission.schema import MissionCommand
from app.mission.validator import validate_mission

LAT0, LON0 = 12.9000, 80.1500
_M = 111_320.0


def ll(x, y):
    return LAT0 + y / _M, LON0 + x / (_M * cos(radians(LAT0)))


def straight(length_m=400.0):
    return [ll(x, 0.0) for x in range(0, int(length_m) + 1, 20)]


def road(name="Karanai Main Road", length_m=400.0):
    return RoadWay(name=name, points=straight(length_m))


def test_road_detection_and_address_split():
    assert detect_scan_mode("Road scan of Karanai Main Road, Sonalur at 20m") is ScanMode.ROAD
    assert detect_scan_mode("Scan the roads of Karanai Main Rd") is ScanMode.ROAD
    assert detect_scan_mode("Structural scan of Ripon Building") is ScanMode.STRUCTURAL
    assert road_name_from_address("Karanai Main Rd, Sonalur, Tamil Nadu 600130") == "Karanai Main Rd"


def test_abbreviations_match_full_names():
    assert names_match("Karanai Main Road", "Karanai Main Rd")
    assert names_match("Karanai Main Rd", "Karanai Main Road")
    assert not names_match("Karanai Side Road", "Karanai Main Rd")


def test_nearest_named_road_is_picked_and_others_ignored():
    far = RoadWay(name="Karanai Main Road", points=[ll(x, 900.0) for x in range(0, 401, 20)])
    near = road()
    other = RoadWay(name="Park Street", points=straight())
    way, s, warnings, d = pick_named_road([far, near, other], "Karanai Main Rd", ll(200, 0))
    assert way is near and d < 1.0


def test_missing_road_is_refused_with_reason():
    with pytest.raises(RoadScanError, match="No mapped road named"):
        plan_road_scan([road("Park Street")], "Karanai Main Rd", ll(200, 0))


def test_plan_follows_the_road_and_is_rule_valid():
    plan = plan_road_scan([road()], "Karanai Main Rd", ll(200, 0), altitude_m=20.0)
    assert validate_mission(plan.mission).valid
    wps = [i for i in plan.mission.mission_items if i.command is MissionCommand.WAYPOINT]
    assert len(wps) >= 2
    assert all(abs(i.altitude - 20.0) < 0.01 for i in wps)
    assert all(abs(i.yaw_deg - 90.0) < 1.0 for i in wps[:-1])      # road runs east -> heading 90
    assert all(i.gimbal_pitch_deg == settings.road_scan_gimbal_pitch_deg for i in wps)
    assert all(i.take_photo for i in wps)


def test_long_road_is_trimmed_to_the_configured_stretch():
    plan = plan_road_scan([road(length_m=2000.0)], "Karanai Main Rd", ll(1000, 0), altitude_m=20.0)
    assert plan.length_m <= settings.road_scan_max_length_m + 5


def test_missing_altitude_uses_default_and_warns():
    plan = plan_road_scan([road()], "Karanai Main Rd", ll(200, 0))
    assert plan.altitude_m == settings.road_scan_altitude_m
    assert any("No altitude" in w for w in plan.warnings)
    assert any("trees, poles" in w for w in plan.warnings)


def test_altitude_out_of_range_is_refused():
    with pytest.raises(RoadScanError, match="outside the allowed range"):
        plan_road_scan([road()], "Karanai Main Rd", ll(200, 0), altitude_m=500.0)
