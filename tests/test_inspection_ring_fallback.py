"""
A perimeter inspection must fly around the building. If the LLM returns a
rule-valid mission with no inspection waypoints (TAKEOFF -> RETURN_TO_LAUNCH),
the computed ring is flown instead. No network, no LLM.
"""

from math import cos, radians
from types import SimpleNamespace

from app.location.inspection_geometry import compute_inspection_ring
from app.location.models import LocationConfidence, LocationSource, MatchDetails, ResolutionType, ResolvedLocation
from app.mission.generator import MissionGenerationResult
from app.mission.location_aware_generator import _ensure_inspection_ring
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.validator import validate_mission

LAT0, LON0 = 12.985623, 80.218431
_M = 111_320.0


def ll(x, y):
    return LAT0 + y / _M, LON0 + x / (_M * cos(radians(LAT0)))


BOX = [ll(-20, -15), ll(20, -15), ll(20, 15), ll(-20, 15)]


def location():
    return ResolvedLocation(
        institution=None, campus=None, building="OLIR Super Speciality Hospital",
        latitude=LAT0, longitude=LON0, resolution_type=ResolutionType.BUILDING,
        source=LocationSource.OPENSTREETMAP, confidence=LocationConfidence.HIGH,
        geometry_available=True, geometry=BOX, match_details=MatchDetails(),
    )


def result_for(items):
    mission = Mission(mission_name="Hospital Inspection", mission_items=items)
    return MissionGenerationResult(mission=mission, validation=validate_mission(mission), attempts=1)


def takeoff_rtl(alt=50.0):
    return [MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=alt),
            MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH)]


def test_takeoff_and_return_only_is_replaced_by_the_ring():
    ring = compute_inspection_ring(location())
    res = result_for(takeoff_rtl(50.0))
    assert res.accepted  # rule-valid, which is exactly the problem
    note = _ensure_inspection_ring(res, ring, SimpleNamespace(altitude_m=None), "OLIR")
    assert note and "ring" in note
    wps = [i for i in res.mission.mission_items if i.command is MissionCommand.WAYPOINT]
    assert len(wps) == len(ring.details)
    assert all(i.altitude >= 50.0 for i in wps)  # the LLM's take-off altitude is kept
    assert res.accepted and res.mission.mission_items[-1].command is MissionCommand.RETURN_TO_LAUNCH


def test_requested_altitude_wins():
    ring = compute_inspection_ring(location())
    res = result_for(takeoff_rtl(50.0))
    _ensure_inspection_ring(res, ring, SimpleNamespace(altitude_m=30.0), "OLIR")
    wps = [i for i in res.mission.mission_items if i.command is MissionCommand.WAYPOINT]
    assert wps and min(i.altitude for i in wps) == 30.0


def test_a_mission_that_already_flies_the_ring_is_left_alone():
    ring = compute_inspection_ring(location())
    items = [MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30.0)]
    for lat, lon in ring.points:
        items.append(MissionItem(sequence=len(items) + 1, command=MissionCommand.WAYPOINT, latitude=lat, longitude=lon, altitude=30.0))
    items.append(MissionItem(sequence=len(items) + 1, command=MissionCommand.RETURN_TO_LAUNCH))
    res = result_for(items)
    before = res.mission
    assert _ensure_inspection_ring(res, ring, SimpleNamespace(altitude_m=30.0), "OLIR") is None
    assert res.mission is before
