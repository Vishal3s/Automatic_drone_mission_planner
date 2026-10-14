"""
UI phase -- ONE long-lived MAVSDK connection to the drone (PX4 SITL by
default, `settings.px4_connection_url`).

The web UI needs three things on the same connection, which the original
one-shot `run_mission_on_px4()` cannot give it:
  1. live telemetry while idle AND during a mission (the existing
     `subscribe_drone_state()` feeding the existing `DroneStateManager`),
  2. flying a mission (the existing `execute_mission_on_system()`),
  3. operator commands at any time: Return-to-Launch and Hold.

Nothing here plans, edits or validates a mission -- it only flies a
mission the existing pipeline already accepted. The LLM never reaches
this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.config import settings
from app.mavlink.drone_state import DroneStateManager, subscribe_drone_state
from app.mavlink.px4_client import (
    MissionExecutionResult,
    PX4ConnectionError,
    _wait_for_connection,
    execute_mission_on_system,
)

log = logging.getLogger(__name__)


class DroneLink:
    def __init__(self, *, address: str | None = None, system_factory=None) -> None:
        self.address = address or settings.px4_connection_url
        self._system_factory = system_factory  # tests inject a fake System
        self.drone = None
        self.state = DroneStateManager()
        self._telemetry_task: asyncio.Task | None = None
        self.connected = False
        self.last_error: str | None = None

    async def connect(self, *, timeout_s: float | None = None) -> bool:
        """Connect once and start streaming telemetry. Returns True when connected."""
        if self.connected:
            return True
        timeout = timeout_s if timeout_s is not None else settings.px4_connect_timeout_s
        if self._system_factory is None:
            from mavsdk import System

            self.drone = System()
        else:
            self.drone = self._system_factory()
        try:
            await self.drone.connect(system_address=self.address)
            await _wait_for_connection(self.drone, timeout, self.address)
        except PX4ConnectionError as exc:
            self.last_error = str(exc)
            self.drone = None
            return False
        self.connected, self.last_error = True, None
        self._telemetry_task = asyncio.create_task(self._telemetry())
        log.info("Drone link connected at %s", self.address)
        return True

    async def _telemetry(self) -> None:
        try:
            await subscribe_drone_state(self.drone, self.state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- a telemetry stream error must not kill the link
            log.warning("Telemetry stream stopped: %s", exc)
            self.last_error = f"Telemetry stream stopped: {exc}"

    async def fly(self, mission, *, on_progress=None, on_stage=None, preupload_check=None,
                  mission_timeout_s: float = 900.0) -> MissionExecutionResult:
        if not self.connected:
            return MissionExecutionResult(accepted=False, stage="connect", error="Drone is not connected.")
        return await execute_mission_on_system(
            self.drone, mission, on_progress=on_progress, on_stage=on_stage,
            preupload_check=preupload_check, mission_timeout_s=mission_timeout_s,
        )

    async def return_to_launch(self) -> None:
        self._require()
        await self.drone.action.return_to_launch()

    async def hold(self) -> None:
        self._require()
        await self.drone.action.hold()

    async def home(self) -> tuple[float, float] | None:
        """PX4's home position, or None if it isn't reported within a few seconds."""
        if not self.connected:
            return None

        async def _first():
            async for h in self.drone.telemetry.home():
                return (h.latitude_deg, h.longitude_deg)

        try:
            return await asyncio.wait_for(_first(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return None

    async def close(self) -> None:
        if self._telemetry_task is not None:
            self._telemetry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._telemetry_task
        self._telemetry_task, self.connected, self.drone = None, False, None

    def _require(self) -> None:
        if not self.connected:
            raise RuntimeError("Drone is not connected.")
