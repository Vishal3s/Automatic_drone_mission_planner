"""
RAMR novelty Phase 2 -- Drone State Management.

This is an EXTENSION of the first version of this file (which was built
against my own inferred design before your actual Phase 2 spec was
given). Your spec asks for the same core idea -- one centralized,
real-time `DroneStateManager` -- plus a specific field list and an
explicit architectural rule, so this version adds what was missing
rather than replacing what was already correct:

    OBJECTIVE: Create one centralized real-time drone state.
    CREATE: DroneStateManager
    REUSE: the existing MAVSDK telemetry implementation.
    TRACK: latitude, longitude, altitude, battery percentage, velocity,
    heading, flight mode, armed state, current waypoint, mission
    progress, timestamp.
    ARCHITECTURE: MAVSDK -> Existing Telemetry Monitor -> DroneStateManager
    -> All Other Modules.
    REQUIREMENT: Do not make every new module independently subscribe to
    MAVSDK telemetry. Use DroneStateManager as the shared source of truth.

What was already correct and is kept unchanged: `DroneState` /
`DroneStateManager` as pure in-memory state (no MAVSDK/asyncio
dependency -- same isolation as `MissionStateManager`/`validator.py`),
and `subscribe_drone_state()` as the one piece that talks to MAVSDK,
deliberately unbounded (no `asyncio.wait_for(...)`) because continuous
tracking has no natural end, unlike every bounded wait in
`px4_client.py`.

What this version ADDS, to match your field list exactly:
  - `velocity_north_m_s` / `velocity_east_m_s` / `velocity_down_m_s`
    (the raw NED vector MAVSDK reports) plus a derived
    `ground_speed_m_s` (horizontal magnitude, computed from the two
    horizontal components -- not a separate telemetry stream, just
    arithmetic on values already received, so it's not "inventing"
    data).
  - `heading_deg`.
  - `current_waypoint` (the 1-based mission item index) and
    `mission_progress` (`(current, total)`), fed from
    `drone.mission.mission_progress()`.

Field names for velocity/heading verified against the MAVSDK-Proto
`telemetry.proto` message definitions the same way position/battery/
flight_mode/armed were verified for the first version of this file:
  - VelocityNed: north_m_s, east_m_s, down_m_s (RPC: SubscribeVelocityNed)
  - Heading: heading_deg, range 0-360 (RPC: SubscribeHeading)
  - Python plugin method names follow the same "drop Subscribe, snake_case"
    pattern already relied on for position()/battery()/flight_mode()/armed():
    velocity_ned(), heading().

On the "shared source of truth" requirement: `subscribe_drone_state()` is
now the ONE place a `DroneStateManager` gets fed from -- any future
module (Trigger Engine, CV analyzer, Battery Feasibility Checker, etc.)
is expected to call `manager.snapshot()` rather than subscribing to
`drone.telemetry.*` or `drone.mission.mission_progress()` itself. The one
deliberate exception is `app/mavlink/px4_client.py`'s existing
`_monitor_mission()` (Phase 7/8, already built and tested before this
phase existed), which still consumes `mission_progress()` directly on
ITS OWN connection to detect mission completion/timeout -- that's
pre-existing core execution logic, not a "new module" in the sense your
requirement means, so per Critical Rule 1 ("do not rewrite completed
Phase 1-8 implementation unless necessary") it is left exactly as-is
rather than rerouted through `DroneStateManager`. What IS true today,
and is a known limitation (see the README): `subscribe_drone_state()`
and `run_mission_on_px4()` each open their OWN `System()` connection when
run standalone, so they are not literally sharing one MAVSDK session yet
-- unifying them needs the "longer-lived session object" wrapper the
audit's Step 6 already flagged as separate, later work (Step 9's Phase
E), not something to smuggle in here. Within the scope this phase
controls, though, the rule is fully honored: there is exactly one
telemetry-consuming coroutine (`subscribe_drone_state()`), and every
field your spec asked for funnels through the one `DroneStateManager`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)


@dataclass
class DroneState:
    """
    Immutable-in-spirit snapshot of the drone's live telemetry. Any field
    is `None` until at least one update for it has been received --
    callers should not assume every field is populated immediately after
    `subscribe_drone_state()` starts (the four telemetry streams update
    independently and at different rates).
    """

    latitude_deg: float | None = None
    longitude_deg: float | None = None
    absolute_altitude_m: float | None = None
    relative_altitude_m: float | None = None  # the "altitude" your spec asks for -- relative to home/takeoff, same convention as MissionItem.altitude in app/mission/schema.py
    battery_voltage_v: float | None = None
    battery_remaining_percent: float | None = None  # the "battery percentage" your spec asks for
    velocity_north_m_s: float | None = None
    velocity_east_m_s: float | None = None
    velocity_down_m_s: float | None = None
    ground_speed_m_s: float | None = None  # derived: horizontal magnitude of the NED vector above
    heading_deg: float | None = None
    flight_mode: str | None = None
    is_armed: bool | None = None
    current_waypoint: int | None = None  # 1-based mission item index, from MissionProgress.current
    mission_progress: tuple[int, int] | None = None  # (current, total), from MissionProgress
    last_updated: datetime | None = None

    def as_dict(self) -> dict:
        """Flat, JSON-serializable summary -- same rationale as MissionState.as_dict()."""
        return {
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "absolute_altitude_m": self.absolute_altitude_m,
            "altitude_m": self.relative_altitude_m,
            "battery_percent": self.battery_remaining_percent,
            "battery_voltage_v": self.battery_voltage_v,
            "velocity_ned_m_s": {
                "north": self.velocity_north_m_s,
                "east": self.velocity_east_m_s,
                "down": self.velocity_down_m_s,
            },
            "ground_speed_m_s": self.ground_speed_m_s,
            "heading_deg": self.heading_deg,
            "flight_mode": self.flight_mode,
            "is_armed": self.is_armed,
            "current_waypoint": self.current_waypoint,
            "mission_progress": self.mission_progress,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }


class DroneStateManager:
    """
    Thread-unsafe (asyncio-single-loop only, like the rest of this
    project), mutable holder of the latest `DroneState`. Cheap to update
    and to read -- `snapshot()` returns the manager's live `DroneState`
    object directly (unlike `MissionStateManager.snapshot()`, which is
    effectively read-only-by-convention too, this is a plain attribute
    read, not a copy -- callers that need a frozen point-in-time copy
    should `copy.replace()` it themselves; nothing in this phase needs
    that yet).
    """

    def __init__(self) -> None:
        self._state = DroneState()

    def update_position(
        self,
        *,
        latitude_deg: float,
        longitude_deg: float,
        absolute_altitude_m: float,
        relative_altitude_m: float,
    ) -> DroneState:
        self._state.latitude_deg = latitude_deg
        self._state.longitude_deg = longitude_deg
        self._state.absolute_altitude_m = absolute_altitude_m
        self._state.relative_altitude_m = relative_altitude_m
        self._touch()
        return self._state

    def update_battery(self, *, voltage_v: float, remaining_percent: float) -> DroneState:
        self._state.battery_voltage_v = voltage_v
        self._state.battery_remaining_percent = remaining_percent
        self._touch()
        return self._state

    def update_flight_mode(self, mode: str) -> DroneState:
        self._state.flight_mode = str(mode)
        self._touch()
        return self._state

    def update_armed(self, is_armed: bool) -> DroneState:
        self._state.is_armed = is_armed
        self._touch()
        return self._state

    def update_velocity(self, *, north_m_s: float, east_m_s: float, down_m_s: float) -> DroneState:
        self._state.velocity_north_m_s = north_m_s
        self._state.velocity_east_m_s = east_m_s
        self._state.velocity_down_m_s = down_m_s
        # Ground speed = horizontal magnitude of the NED vector -- arithmetic
        # on values just received, not a separate MAVSDK stream.
        self._state.ground_speed_m_s = (north_m_s**2 + east_m_s**2) ** 0.5
        self._touch()
        return self._state

    def update_heading(self, heading_deg: float) -> DroneState:
        self._state.heading_deg = heading_deg
        self._touch()
        return self._state

    def update_mission_progress(self, current: int, total: int) -> DroneState:
        self._state.current_waypoint = current
        self._state.mission_progress = (current, total)
        self._touch()
        return self._state

    def snapshot(self) -> DroneState:
        return self._state

    def _touch(self) -> None:
        self._state.last_updated = datetime.now(timezone.utc)


async def subscribe_drone_state(drone, manager: DroneStateManager) -> None:
    """
    Continuously consume `drone.telemetry.position()/.battery()/
    .velocity_ned()/.heading()/.flight_mode()/.armed()` and
    `drone.mission.mission_progress()`, feeding every update into
    `manager`. This is the ONE place a `DroneStateManager` is fed from --
    per your Phase 2 architecture requirement, no other module in this
    project subscribes to these streams independently (see this module's
    docstring for the one pre-existing, deliberate exception:
    `px4_client.py`'s own mission-completion monitoring).

    Runs until every one of the six underlying async generators ends
    (which, against a real PX4 SITL connection, is effectively "forever"
    -- MAVSDK telemetry streams don't end on their own, and
    `mission_progress()` simply produces nothing until a mission is
    actually running) or until this coroutine's enclosing task is
    cancelled. Deliberately unbounded -- see this module's docstring for
    why a timeout would be wrong here, unlike every wait in
    `px4_client.py`.

    Callers control the lifetime by running this inside a task and
    cancelling it, e.g.:

        task = asyncio.create_task(subscribe_drone_state(drone, manager))
        ...                                   # do other things; read manager.snapshot()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    """

    async def _watch_position() -> None:
        async for position in drone.telemetry.position():
            manager.update_position(
                latitude_deg=position.latitude_deg,
                longitude_deg=position.longitude_deg,
                absolute_altitude_m=position.absolute_altitude_m,
                relative_altitude_m=position.relative_altitude_m,
            )

    async def _watch_battery() -> None:
        async for battery in drone.telemetry.battery():
            manager.update_battery(
                voltage_v=battery.voltage_v,
                remaining_percent=battery.remaining_percent,
            )

    async def _watch_velocity() -> None:
        async for velocity in drone.telemetry.velocity_ned():
            manager.update_velocity(
                north_m_s=velocity.north_m_s,
                east_m_s=velocity.east_m_s,
                down_m_s=velocity.down_m_s,
            )

    async def _watch_heading() -> None:
        async for heading in drone.telemetry.heading():
            manager.update_heading(heading.heading_deg)

    async def _watch_flight_mode() -> None:
        async for mode in drone.telemetry.flight_mode():
            manager.update_flight_mode(mode)

    async def _watch_armed() -> None:
        async for is_armed in drone.telemetry.armed():
            manager.update_armed(is_armed)

    async def _watch_mission_progress() -> None:
        async for progress in drone.mission.mission_progress():
            manager.update_mission_progress(progress.current, progress.total)

    log.info(
        "Starting continuous telemetry subscription "
        "(position/battery/velocity/heading/flight_mode/armed/mission_progress)."
    )
    await asyncio.gather(
        _watch_position(),
        _watch_battery(),
        _watch_velocity(),
        _watch_heading(),
        _watch_flight_mode(),
        _watch_armed(),
        _watch_mission_progress(),
    )


def _demo_cli() -> None:
    """
    Manual test entry point -- connects to a running PX4 SITL instance,
    streams live drone state for a fixed window, and prints it:
        python -m app.mavlink.drone_state [seconds]
    Needs PX4 SITL already running and reachable at PX4_CONNECTION_URL
    (see the README's Phase 7/8 section) -- this does NOT run a mission,
    it only observes telemetry, so it's safe to run alongside (or
    instead of) `python -m app.mavlink.px4_client`.
    """
    import sys

    from app.config import settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    window_s = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0

    async def _run() -> None:
        from mavsdk import System

        drone = System()
        await drone.connect(system_address=settings.px4_connection_url)
        print(f"Connecting to {settings.px4_connection_url} ...")

        async for state in drone.core.connection_state():
            if state.is_connected:
                break

        manager = DroneStateManager()
        task = asyncio.create_task(subscribe_drone_state(drone, manager))

        print(f"Streaming telemetry for {window_s}s ...\n")
        for _ in range(int(window_s)):
            await asyncio.sleep(1.0)
            print(manager.snapshot().as_dict())

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


if __name__ == "__main__":
    _demo_cli()
