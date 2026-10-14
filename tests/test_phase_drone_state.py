"""
RAMR novelty Phase 2 tests -- Drone State Management.
Run with: pytest tests/test_phase_drone_state.py -v

Extended (not replaced) when the actual Phase 2 spec added velocity,
heading, current_waypoint, and mission_progress to the tracked field
list -- every test that existed before that extension still passes
unchanged; this file adds coverage for the new fields and streams.

Two groups:
1. `DroneState`/`DroneStateManager` tests -- pure in-memory, no fakes needed.
2. `subscribe_drone_state()` tests -- fake `drone.telemetry.*` and
   `drone.mission.mission_progress()` async generators (finite
   sequences, unlike a real PX4 connection) so the concurrent
   consumption loops complete naturally and `asyncio.gather(...)`
   returns instead of running forever, same fake-shaped-like-the-real-API
   approach as test_phase7_8_px4_client.py (no real `mavsdk`, network, or
   PX4 SITL needed).
"""

from __future__ import annotations

import asyncio

from app.mavlink.drone_state import DroneStateManager, subscribe_drone_state

# ---------------------------------------------------------------------
# DroneStateManager -- pure state, no fakes
# ---------------------------------------------------------------------


def test_new_manager_snapshot_starts_all_none():
    manager = DroneStateManager()
    state = manager.snapshot()

    assert state.latitude_deg is None
    assert state.battery_remaining_percent is None
    assert state.velocity_north_m_s is None
    assert state.ground_speed_m_s is None
    assert state.heading_deg is None
    assert state.flight_mode is None
    assert state.is_armed is None
    assert state.current_waypoint is None
    assert state.mission_progress is None
    assert state.last_updated is None


def test_update_position_sets_all_four_fields_and_timestamp():
    manager = DroneStateManager()
    state = manager.update_position(
        latitude_deg=1.5, longitude_deg=2.5, absolute_altitude_m=100.0, relative_altitude_m=30.0
    )

    assert state.latitude_deg == 1.5
    assert state.longitude_deg == 2.5
    assert state.absolute_altitude_m == 100.0
    assert state.relative_altitude_m == 30.0
    assert state.last_updated is not None


def test_update_battery_sets_voltage_and_percent():
    manager = DroneStateManager()
    state = manager.update_battery(voltage_v=16.8, remaining_percent=0.75)

    assert state.battery_voltage_v == 16.8
    assert state.battery_remaining_percent == 0.75


def test_update_velocity_sets_ned_components_and_derives_ground_speed():
    manager = DroneStateManager()
    # 3-4-5 triangle on the horizontal plane -> ground speed 5.0
    state = manager.update_velocity(north_m_s=3.0, east_m_s=4.0, down_m_s=1.0)

    assert state.velocity_north_m_s == 3.0
    assert state.velocity_east_m_s == 4.0
    assert state.velocity_down_m_s == 1.0
    assert state.ground_speed_m_s == 5.0


def test_update_heading_sets_degrees():
    manager = DroneStateManager()
    state = manager.update_heading(90.0)
    assert state.heading_deg == 90.0


def test_update_flight_mode_stores_string_form():
    manager = DroneStateManager()
    state = manager.update_flight_mode("MISSION")
    assert state.flight_mode == "MISSION"


def test_update_armed_sets_boolean():
    manager = DroneStateManager()
    state = manager.update_armed(True)
    assert state.is_armed is True


def test_update_mission_progress_sets_current_waypoint_and_progress_tuple():
    manager = DroneStateManager()
    state = manager.update_mission_progress(2, 5)

    assert state.current_waypoint == 2
    assert state.mission_progress == (2, 5)


def test_updates_are_independent_of_each_other():
    manager = DroneStateManager()
    manager.update_position(
        latitude_deg=1.0, longitude_deg=2.0, absolute_altitude_m=50.0, relative_altitude_m=10.0
    )
    manager.update_armed(True)

    state = manager.snapshot()
    # Battery/flight_mode/velocity/mission_progress untouched by the
    # position/armed updates above.
    assert state.battery_voltage_v is None
    assert state.flight_mode is None
    assert state.velocity_north_m_s is None
    assert state.mission_progress is None
    assert state.latitude_deg == 1.0
    assert state.is_armed is True


def test_as_dict_is_json_serializable_shape():
    import json

    manager = DroneStateManager()
    manager.update_battery(voltage_v=16.0, remaining_percent=0.5)
    manager.update_mission_progress(1, 3)

    payload = json.dumps(manager.snapshot().as_dict())
    assert '"battery_voltage_v": 16.0' in payload
    assert '"battery_percent": 0.5' in payload
    assert '"mission_progress": [1, 3]' in payload


# ---------------------------------------------------------------------
# subscribe_drone_state() -- fake MAVSDK telemetry + mission generators
# ---------------------------------------------------------------------


class _FakePosition:
    def __init__(self, latitude_deg, longitude_deg, absolute_altitude_m, relative_altitude_m):
        self.latitude_deg = latitude_deg
        self.longitude_deg = longitude_deg
        self.absolute_altitude_m = absolute_altitude_m
        self.relative_altitude_m = relative_altitude_m


class _FakeBattery:
    def __init__(self, voltage_v, remaining_percent):
        self.voltage_v = voltage_v
        self.remaining_percent = remaining_percent


class _FakeVelocityNed:
    def __init__(self, north_m_s, east_m_s, down_m_s):
        self.north_m_s = north_m_s
        self.east_m_s = east_m_s
        self.down_m_s = down_m_s


class _FakeHeading:
    def __init__(self, heading_deg):
        self.heading_deg = heading_deg


class _FakeMissionProgress:
    def __init__(self, current, total):
        self.current = current
        self.total = total


class _FakeTelemetry:
    """
    Shaped like `mavsdk.telemetry` (the real plugin), each stream a
    finite async generator so `subscribe_drone_state()`'s
    `asyncio.gather(...)` completes on its own -- a real PX4 connection's
    streams never end, which is fine: production callers cancel the task
    instead (see the module docstring), but tests need a natural end.
    """

    def __init__(self):
        self.positions = [
            _FakePosition(1.0, 2.0, 100.0, 20.0),
            _FakePosition(1.1, 2.1, 101.0, 21.0),
        ]
        self.batteries = [_FakeBattery(16.8, 0.9), _FakeBattery(16.5, 0.8)]
        self.velocities = [_FakeVelocityNed(1.0, 0.0, 0.0), _FakeVelocityNed(3.0, 4.0, 0.0)]
        self.headings = [_FakeHeading(45.0), _FakeHeading(90.0)]
        self.flight_modes = ["TAKEOFF", "MISSION"]
        self.armed_sequence = [False, True]

    async def position(self):
        for p in self.positions:
            yield p

    async def battery(self):
        for b in self.batteries:
            yield b

    async def velocity_ned(self):
        for v in self.velocities:
            yield v

    async def heading(self):
        for h in self.headings:
            yield h

    async def flight_mode(self):
        for m in self.flight_modes:
            yield m

    async def armed(self):
        for a in self.armed_sequence:
            yield a


class _FakeMission:
    def __init__(self):
        self.progress_sequence = [_FakeMissionProgress(1, 3), _FakeMissionProgress(2, 3)]

    async def mission_progress(self):
        for p in self.progress_sequence:
            yield p


class _FakeDrone:
    def __init__(self):
        self.telemetry = _FakeTelemetry()
        self.mission = _FakeMission()


def test_subscribe_drone_state_applies_final_value_of_each_stream():
    drone = _FakeDrone()
    manager = DroneStateManager()

    asyncio.run(subscribe_drone_state(drone, manager))

    state = manager.snapshot()
    assert state.latitude_deg == 1.1
    assert state.longitude_deg == 2.1
    assert state.battery_remaining_percent == 0.8
    assert state.velocity_north_m_s == 3.0
    assert state.ground_speed_m_s == 5.0
    assert state.heading_deg == 90.0
    assert state.flight_mode == "MISSION"
    assert state.is_armed is True
    assert state.current_waypoint == 2
    assert state.mission_progress == (2, 3)


def test_subscribe_drone_state_can_be_cancelled_mid_stream():
    """
    Against a real connection the streams never end, so callers must be
    able to cancel cleanly -- prove that a cancelled task doesn't hang or
    raise anything other than CancelledError, and that whatever updates
    arrived before cancellation were still applied.
    """

    class _NeverEndingTelemetry(_FakeTelemetry):
        async def position(self):
            for p in self.positions:
                yield p
            while True:
                await asyncio.sleep(1000)

    class _NeverEndingDrone:
        def __init__(self):
            self.telemetry = _NeverEndingTelemetry()
            self.mission = _FakeMission()

    async def _run():
        drone = _NeverEndingDrone()
        manager = DroneStateManager()
        task = asyncio.create_task(subscribe_drone_state(drone, manager))
        await asyncio.sleep(0.05)  # let the finite streams finish, position stall
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return manager

    manager = asyncio.run(_run())
    state = manager.snapshot()
    # Battery/flight_mode/armed/velocity/heading/mission streams are
    # finite and had time to finish.
    assert state.battery_remaining_percent == 0.8
    assert state.is_armed is True
    assert state.heading_deg == 90.0
    assert state.mission_progress == (2, 3)
    # Position stream was cancelled after its two fake values were yielded.
    assert state.latitude_deg == 1.1


def test_subscribe_drone_state_with_no_mission_running_leaves_progress_none():
    """
    Real PX4 behavior: `mission_progress()` produces nothing until a
    mission is actually uploaded and started -- this manager must not
    assume mission_progress is ever populated (e.g. when only observing
    telemetry, per this file's `_demo_cli()`, with no mission running).
    """

    class _NoMissionYet:
        async def mission_progress(self):
            return
            yield  # pragma: no cover -- makes this an async generator that yields nothing

    class _DroneWithNoMission:
        def __init__(self):
            self.telemetry = _FakeTelemetry()
            self.mission = _NoMissionYet()

    drone = _DroneWithNoMission()
    manager = DroneStateManager()

    asyncio.run(subscribe_drone_state(drone, manager))

    state = manager.snapshot()
    # Telemetry streams still populated normally.
    assert state.is_armed is True
    # Mission progress never arrived -- current_waypoint stays None, not a fabricated 0.
    assert state.current_waypoint is None
    assert state.mission_progress is None
