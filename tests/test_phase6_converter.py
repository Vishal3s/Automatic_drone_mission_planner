"""
Phase 6 tests -- run with:  pytest tests/test_phase6_converter.py -v

`app/mission/converter.py` lazily imports `mavsdk.mission` (same pattern
as the LLM client's lazy `ollama`/`groq` imports), so these tests never
need the real `mavsdk` package installed: a fake `mavsdk.mission` module
is injected into `sys.modules` before each test, with fake
`MissionItem`/`MissionPlan` classes that just record the exact
positional arguments they were called with. That's enough to verify
`to_mavsdk_mission_plan()`'s actual logic (coordinate inference for
TAKEOFF/LAND, the RETURN_TO_LAUNCH vs LAND branch, field values) without
a network, a PX4 SITL instance, or the real MAVSDK bindings.
"""

from __future__ import annotations

import math
import sys
import types

import pytest

from app.mission.schema import Mission, MissionCommand, MissionItem


class _FakeCameraAction:
    NONE = "CameraAction.NONE"


class _FakeVehicleAction:
    NONE = "VehicleAction.NONE"
    TAKEOFF = "VehicleAction.TAKEOFF"
    LAND = "VehicleAction.LAND"


class _FakeMissionItem:
    """Records every positional arg by the same names converter.py uses."""

    CameraAction = _FakeCameraAction
    VehicleAction = _FakeVehicleAction

    _FIELD_NAMES = (
        "latitude_deg", "longitude_deg", "relative_altitude_m", "speed_m_s",
        "is_fly_through", "gimbal_pitch_deg", "gimbal_yaw_deg", "camera_action",
        "loiter_time_s", "camera_photo_interval_s", "acceptance_radius_m",
        "yaw_deg", "camera_photo_distance_m", "vehicle_action",
    )

    def __init__(self, *args):
        assert len(args) == len(self._FIELD_NAMES), (
            f"MissionItem() called with {len(args)} positional args, expected "
            f"{len(self._FIELD_NAMES)} -- converter.py's argument order must match "
            f"mavsdk.mission.MissionItem's constructor exactly."
        )
        for name, value in zip(self._FIELD_NAMES, args):
            setattr(self, name, value)


class _FakeMissionPlan:
    def __init__(self, mission_items):
        self.mission_items = mission_items


@pytest.fixture(autouse=True)
def _fake_mavsdk(monkeypatch):
    fake_mission_module = types.ModuleType("mavsdk.mission")
    fake_mission_module.MissionItem = _FakeMissionItem
    fake_mission_module.MissionPlan = _FakeMissionPlan

    fake_mavsdk_package = types.ModuleType("mavsdk")
    fake_mavsdk_package.mission = fake_mission_module

    monkeypatch.setitem(sys.modules, "mavsdk", fake_mavsdk_package)
    monkeypatch.setitem(sys.modules, "mavsdk.mission", fake_mission_module)

    # converter.py may already be imported (with real/absent mavsdk) from
    # an earlier test module -- force a clean re-import so its lazy
    # `from mavsdk.mission import ...` picks up the fakes above.
    monkeypatch.delitem(sys.modules, "app.mission.converter", raising=False)

    yield


def _import_converter():
    import app.mission.converter as converter_module

    return converter_module


def test_takeoff_waypoint_rtl_produces_two_items_and_rtl_flag():
    converter = _import_converter()

    mission = Mission(
        mission_name="Simple",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=10.0, longitude=20.0, altitude=50),
            MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )

    result = converter.to_mavsdk_mission_plan(mission)

    assert result.return_to_launch_after_mission is True
    # RETURN_TO_LAUNCH produces no item of its own -- 2 items, not 3.
    assert len(result.plan.mission_items) == 2

    takeoff_item, waypoint_item = result.plan.mission_items

    assert takeoff_item.vehicle_action == "VehicleAction.TAKEOFF"
    assert waypoint_item.vehicle_action == "VehicleAction.NONE"


def test_takeoff_borrows_coordinates_from_the_next_waypoint():
    converter = _import_converter()

    mission = Mission(
        mission_name="TakeoffCoords",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=11.1, longitude=22.2, altitude=60),
            MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )

    result = converter.to_mavsdk_mission_plan(mission)
    takeoff_item = result.plan.mission_items[0]

    assert takeoff_item.latitude_deg == 11.1
    assert takeoff_item.longitude_deg == 22.2
    # TAKEOFF's own altitude is used, NOT the waypoint's altitude.
    assert takeoff_item.relative_altitude_m == 50
    assert takeoff_item.vehicle_action == "VehicleAction.TAKEOFF"
    assert takeoff_item.speed_m_s > 0
    assert math.isnan(takeoff_item.acceptance_radius_m)


def test_land_borrows_coordinates_and_altitude_from_the_previous_waypoint():
    converter = _import_converter()

    mission = Mission(
        mission_name="LandCoords",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=33.3, longitude=44.4, altitude=70),
            MissionItem(sequence=3, command=MissionCommand.LAND),
        ],
    )

    result = converter.to_mavsdk_mission_plan(mission)

    assert result.return_to_launch_after_mission is False
    assert len(result.plan.mission_items) == 3

    land_item = result.plan.mission_items[-1]
    assert land_item.latitude_deg == 33.3
    assert land_item.longitude_deg == 44.4
    assert land_item.relative_altitude_m == 70  # borrowed from the waypoint before it
    assert land_item.vehicle_action == "VehicleAction.LAND"
    assert land_item.is_fly_through is False  # must stop to land, not fly through


def test_degenerate_takeoff_land_mission_raises_without_home_position():
    converter = _import_converter()

    mission = Mission(
        mission_name="NoWaypoints",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
            MissionItem(sequence=2, command=MissionCommand.LAND),
        ],
    )

    with pytest.raises(converter.MissionConversionError):
        converter.to_mavsdk_mission_plan(mission)


def test_degenerate_takeoff_land_mission_succeeds_with_home_position_fallback():
    converter = _import_converter()

    mission = Mission(
        mission_name="NoWaypoints",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
            MissionItem(sequence=2, command=MissionCommand.LAND),
        ],
    )

    result = converter.to_mavsdk_mission_plan(
        mission, home_latitude_deg=1.0, home_longitude_deg=2.0
    )

    takeoff_item, land_item = result.plan.mission_items
    assert (takeoff_item.latitude_deg, takeoff_item.longitude_deg) == (1.0, 2.0)
    assert (land_item.latitude_deg, land_item.longitude_deg) == (1.0, 2.0)
    # No waypoint anywhere to borrow an altitude from -- LAND falls back
    # to the last known altitude, which here is TAKEOFF's own altitude
    # (see converter.py's `last_known_altitude_m` tracking).
    assert land_item.relative_altitude_m == 50


def test_loiter_item_carries_hold_time_and_stops_rather_than_flies_through():
    converter = _import_converter()

    mission = Mission(
        mission_name="Loiter",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
            MissionItem(
                sequence=2, command=MissionCommand.LOITER,
                latitude=5.0, longitude=6.0, altitude=50, hold_time_s=45,
            ),
            MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )

    result = converter.to_mavsdk_mission_plan(mission)
    loiter_item = result.plan.mission_items[1]

    assert loiter_item.loiter_time_s == 45
    assert loiter_item.is_fly_through is False
    assert loiter_item.vehicle_action == "VehicleAction.NONE"


def test_waypoint_item_flies_through_and_uses_configured_default_speed():
    converter = _import_converter()
    from app.config import settings

    mission = Mission(
        mission_name="Waypoint",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1, longitude=2, altitude=50),
            MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )

    result = converter.to_mavsdk_mission_plan(mission)
    waypoint_item = result.plan.mission_items[1]

    assert waypoint_item.is_fly_through is True
    assert waypoint_item.speed_m_s == settings.mission_default_speed_ms
    assert math.isnan(waypoint_item.loiter_time_s)
