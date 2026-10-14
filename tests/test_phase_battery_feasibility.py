"""
RAMR novelty Phase 8 tests -- Battery Feasibility for Replanning.
Run with: pytest tests/test_phase_battery_feasibility.py -v

Pure Python -- no LLM/MAVSDK/PX4 SITL needed.
"""

from __future__ import annotations

from app.mission.schema import MissionCommand, MissionItem
from app.replanning.battery_feasibility import (
    BatteryFeasibilityChecker,
    BatteryFeasibilityStatus,
)

CHECKER = BatteryFeasibilityChecker()


# ---------------------------------------------------------------------
# The spec's own worked example, reproduced exactly.
# ---------------------------------------------------------------------


def test_spec_worked_example_is_feasible():
    # Current Battery = 30%, Required Remaining Mission = 12%, Required RTH = 10%,
    # Reserve = 5% -> Total Required = 27% -> FEASIBLE.
    # 12% = 6.0 km * 0.02 %/km ; 10% = 5.0 km * 0.02 %/km (default consumption rate).
    result = CHECKER.check(
        current_battery_percent=0.30,
        remaining_mission_distance_km=6.0,
        corrective_waypoint_distance_km=0.0,
        return_to_home_distance_km=5.0,
        battery_reserve_percent=0.05,
    )
    assert result.status == BatteryFeasibilityStatus.FEASIBLE
    assert result.feasible is True
    assert round(result.remaining_mission_required_percent, 4) == 0.12
    assert round(result.return_to_home_required_percent, 4) == 0.10
    assert round(result.total_required_percent, 4) == 0.27


def test_total_required_greater_than_available_is_not_feasible():
    # Same shape, but less battery available -> NOT_FEASIBLE.
    result = CHECKER.check(
        current_battery_percent=0.20,
        remaining_mission_distance_km=6.0,
        corrective_waypoint_distance_km=0.0,
        return_to_home_distance_km=5.0,
        battery_reserve_percent=0.05,
    )
    assert result.status == BatteryFeasibilityStatus.NOT_FEASIBLE
    assert result.feasible is False
    assert round(result.total_required_percent, 4) == 0.27


def test_exactly_equal_total_required_is_feasible():
    # Total Required > Available -> NOT_FEASIBLE; equal should still be FEASIBLE (not a strict >).
    result = CHECKER.check(
        current_battery_percent=0.27,
        remaining_mission_distance_km=6.0,
        corrective_waypoint_distance_km=0.0,
        return_to_home_distance_km=5.0,
        battery_reserve_percent=0.05,
    )
    assert result.status == BatteryFeasibilityStatus.FEASIBLE


# ---------------------------------------------------------------------
# Revised Mission + Return To Home -- the IMPORTANT RULE
# ---------------------------------------------------------------------


def test_corrective_waypoint_distance_is_included_in_total():
    without_corrective = CHECKER.check(
        current_battery_percent=0.30, remaining_mission_distance_km=6.0,
        corrective_waypoint_distance_km=0.0, return_to_home_distance_km=5.0, battery_reserve_percent=0.05,
    )
    with_corrective = CHECKER.check(
        current_battery_percent=0.30, remaining_mission_distance_km=6.0,
        corrective_waypoint_distance_km=2.0, return_to_home_distance_km=5.0, battery_reserve_percent=0.05,
    )
    assert with_corrective.total_required_percent > without_corrective.total_required_percent
    assert with_corrective.status == BatteryFeasibilityStatus.NOT_FEASIBLE  # 27% + 4% = 31% > 30%


def test_defaults_come_from_settings_when_not_overridden():
    from app.config import settings

    result = CHECKER.check(
        current_battery_percent=1.0,
        remaining_mission_distance_km=1.0,
        corrective_waypoint_distance_km=0.0,
        return_to_home_distance_km=0.0,
    )
    assert result.battery_reserve_percent == settings.min_battery_reserve_percent
    assert round(result.remaining_mission_required_percent, 6) == round(1.0 * settings.battery_consumption_percent_per_km, 6)


# ---------------------------------------------------------------------
# check_from_positions() -- derives distances from real coordinates
# ---------------------------------------------------------------------


def test_check_from_positions_walks_corrective_then_remaining_then_home():
    # Straight line on the same longitude: current(1.000) -> corrective(1.005) -> remaining(1.010) -> home(1.000)
    result = CHECKER.check_from_positions(
        current_battery_percent=0.50,
        current_latitude_deg=1.000, current_longitude_deg=2.000,
        remaining_waypoints=[MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1.010, longitude=2.000, altitude=30)],
        corrective_waypoints=[MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1.005, longitude=2.000, altitude=30)],
        home_latitude_deg=1.000, home_longitude_deg=2.000,
    )
    # corrective leg (1.000->1.005) + remaining leg (1.005->1.010) + RTH leg (1.010->1.000, the FULL span back)
    assert result.corrective_waypoint_distance_km > 0
    assert result.remaining_mission_distance_km > 0
    assert result.return_to_home_distance_km > 0
    # RTH leg should be roughly the corrective+remaining legs combined (straight line back to start).
    assert result.return_to_home_distance_km == result.corrective_waypoint_distance_km + result.remaining_mission_distance_km


def test_check_from_positions_with_no_waypoints_is_a_direct_return_to_home():
    result = CHECKER.check_from_positions(
        current_battery_percent=0.50,
        current_latitude_deg=1.000, current_longitude_deg=2.000,
        remaining_waypoints=[], corrective_waypoints=[],
        home_latitude_deg=1.009, home_longitude_deg=2.000,
    )
    assert result.remaining_mission_distance_km == 0.0
    assert result.corrective_waypoint_distance_km == 0.0
    assert result.return_to_home_distance_km > 0


def test_check_from_positions_ignores_items_without_coordinates():
    # RETURN_TO_LAUNCH carries no coordinates -- should not contribute to distance.
    result = CHECKER.check_from_positions(
        current_battery_percent=0.50,
        current_latitude_deg=1.000, current_longitude_deg=2.000,
        remaining_waypoints=[MissionItem(sequence=1, command=MissionCommand.RETURN_TO_LAUNCH)],
        corrective_waypoints=[],
        home_latitude_deg=1.000, home_longitude_deg=2.000,
    )
    assert result.remaining_mission_distance_km == 0.0
    assert result.return_to_home_distance_km == 0.0  # position never moved, home == current


# ---------------------------------------------------------------------
# SUCCESS CRITERIA: rejects adaptations leaving insufficient battery for
# safe return.
# ---------------------------------------------------------------------


def test_insufficient_battery_for_safe_return_is_not_feasible():
    result = CHECKER.check_from_positions(
        current_battery_percent=0.10,  # very low
        current_latitude_deg=1.000, current_longitude_deg=2.000,
        remaining_waypoints=[MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1.050, longitude=2.000, altitude=30)],
        corrective_waypoints=[],
        home_latitude_deg=1.000, home_longitude_deg=2.000,
        battery_reserve_percent=0.15,
    )
    assert result.status == BatteryFeasibilityStatus.NOT_FEASIBLE


def test_as_dict_shape():
    result = CHECKER.check(
        current_battery_percent=0.30, remaining_mission_distance_km=6.0,
        corrective_waypoint_distance_km=0.0, return_to_home_distance_km=5.0, battery_reserve_percent=0.05,
    )
    d = result.as_dict()
    assert d["status"] == "FEASIBLE"
    assert set(d.keys()) == {
        "status", "current_battery_percent", "remaining_mission_distance_km",
        "corrective_waypoint_distance_km", "return_to_home_distance_km",
        "remaining_mission_required_percent", "corrective_waypoint_required_percent",
        "return_to_home_required_percent", "battery_reserve_percent", "total_required_percent", "reason",
    }
