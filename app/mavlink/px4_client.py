"""
Phase 7/8 -- connects to PX4 SITL via MAVSDK, uploads a converted mission
(Phase 6), runs it, and reports whether it was accepted (executed
without error) or rejected (any stage failed) -- the final
"PX4 SITL Execution -> accepted/rejected result" node in the pipeline.

SAFETY (standing project constraint, unchanged since Phase 1): this
module is designed and tested ONLY against PX4 SITL, a simulator.
`PX4_CONNECTION_URL` in `.env` is expected to point at a simulated
vehicle. Nothing here is safe to point at real hardware, and this
project does not give instructions for doing so.

Every MAVSDK call in this file was checked against the current (3.17.2,
pinned in requirements.txt) MAVSDK-Python source/docs before being
written -- see the inline references. The two load-bearing, non-obvious
facts:

1. `System.connect()` does not itself wait for a connection -- it just
   starts MAVSDK listening/dialing. You have to separately consume
   `drone.core.connection_state()` (an async generator) until you see
   `state.is_connected`. Left unbounded, this would hang forever if PX4
   SITL isn't running, so every wait in this file is wrapped in
   `asyncio.wait_for(..., timeout=...)`.
2. Every MAVSDK plugin call that can fail (mission upload, arm, mission
   start) raises a plugin-specific exception -- `mavsdk.mission.MissionError`
   / `mavsdk.action.ActionError` -- rather than returning a bool. Both
   have a `__str__` that includes the actual MAVLink-level failure
   reason (from `MissionResult`/`ActionResult`), which is what
   `MissionExecutionResult.error` surfaces to the caller.

Design choice: `run_mission_on_px4()` does NOT raise on a failed/rejected
execution (connection timeout, preflight not ready, upload rejected, arm
refused, mission timeout) -- exactly like Phase 5's
`generate_validated_mission()` doesn't raise on a rule-invalid mission.
A rejected SITL run is a normal, expected outcome this project's later
phases (Phase 9's automated tests, Phase 11's UI) need to display, not
an exceptional program state.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.config import settings
from app.mission.converter import MissionConversionError, to_mavsdk_mission_plan
from app.mission.schema import Mission

log = logging.getLogger(__name__)


class PX4ConnectionError(Exception):
    """Could not connect to PX4 SITL, or it never became flight-ready, in time."""


class PX4MissionExecutionError(Exception):
    """The mission was uploaded and started but did not finish in time."""

    def __init__(self, message: str, progress_log: list[tuple[int, int]] | None = None):
        super().__init__(message)
        # Carries whatever progress was observed before the timeout, so
        # the caller doesn't lose it just because the mission stalled --
        # see run_mission_on_px4()'s "monitor" except-branch.
        self.progress_log = progress_log or []


@dataclass
class MissionExecutionResult:
    """
    What `run_mission_on_px4()` always returns -- success or failure,
    never an exception for an expected/normal rejection.
    """

    accepted: bool
    # Which stage the result reflects: "connect", "preflight", "convert",
    # "upload", "arm", "start", "monitor", or "complete" (only "complete"
    # implies accepted=True).
    stage: str
    error: str | None
    progress_log: list[tuple[int, int]] = field(default_factory=list)
    return_to_launch_after_mission: bool | None = None


async def _wait_for_connection(drone, timeout_s: float, system_address: str) -> None:
    async def _watch() -> None:
        async for state in drone.core.connection_state():
            if state.is_connected:
                return

    try:
        await asyncio.wait_for(_watch(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise PX4ConnectionError(
            f"Timed out after {timeout_s}s waiting for PX4 SITL to connect at "
            f"{system_address!r}. Is PX4 SITL running (e.g. `make px4_sitl gz_x500`), "
            f"and does PX4_CONNECTION_URL in .env match how it's reachable from here? "
            f"See the README's Phase 7 section for the WSL2 networking gotcha if PX4 "
            f"SITL is running inside WSL2 while this runs on Windows."
        ) from exc


async def _wait_for_preflight_ready(drone, timeout_s: float) -> None:
    async def _watch() -> None:
        async for health in drone.telemetry.health():
            if health.is_global_position_ok and health.is_home_position_ok:
                return

    try:
        await asyncio.wait_for(_watch(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise PX4ConnectionError(
            f"PX4 SITL connected, but did not report a valid global+home position "
            f"within {timeout_s}s. SITL usually needs a few seconds after startup for "
            f"the simulated GPS/EKF to settle -- try again, or check PX4 SITL's own "
            f"console output for EKF/sensor warnings."
        ) from exc


async def _get_home_position(drone, timeout_s: float) -> tuple[float, float]:
    async def _watch() -> tuple[float, float]:
        async for position in drone.telemetry.home():
            return position.latitude_deg, position.longitude_deg
        raise PX4ConnectionError("telemetry.home() ended without ever yielding a position.")

    try:
        return await asyncio.wait_for(_watch(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise PX4ConnectionError(
            f"Timed out after {timeout_s}s waiting for PX4 SITL to report a home position."
        ) from exc


async def _monitor_mission(
    drone,
    timeout_s: float,
    on_progress=None,
) -> list[tuple[int, int]]:
    progress_log: list[tuple[int, int]] = []

    async def _watch() -> None:
        async for progress in drone.mission.mission_progress():
            progress_log.append((progress.current, progress.total))
            log.info("Mission progress: %d/%d", progress.current, progress.total)
            if on_progress is not None:
                # RAMR novelty Phase 1 (Mission State Management) hook --
                # additive only: existing callers that don't pass
                # `on_progress` see no change at all. Lets a
                # MissionStateManager (app/mission/state.py) stay in sync
                # with live progress without this file needing to know
                # anything about mission-state tracking itself.
                on_progress(progress.current, progress.total)
            if await drone.mission.is_mission_finished():
                return

    try:
        await asyncio.wait_for(_watch(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        last = progress_log[-1] if progress_log else (0, 0)
        raise PX4MissionExecutionError(
            f"Mission did not finish within {timeout_s}s (last progress: "
            f"{last[0]}/{last[1]}).",
            progress_log=progress_log,
        ) from exc

    return progress_log


async def run_mission_on_px4(
    mission: Mission,
    *,
    system_address: str | None = None,
    connect_timeout_s: float | None = None,
    mission_timeout_s: float = 180.0,
    on_progress=None,
) -> MissionExecutionResult:
    """
    Full Phase 7/8 pipeline: connect to PX4 SITL, wait for it to be
    flight-ready, convert `mission` (Phase 6) using PX4's own reported
    home position, upload it, arm, start, and monitor to completion.

    `mission` must already be schema-valid AND rule-valid (i.e. already
    passed through `app.mission.validator.validate_mission()` with
    `valid=True`, e.g. via `app.mission.generator.generate_validated_mission()`)
    -- this function doesn't re-check that, matching `to_mavsdk_mission_plan()`'s
    own contract (see `app/mission/converter.py`).

    Never raises for an expected rejection (connection/preflight
    timeout, conversion failure, upload/arm/start rejected by PX4,
    mission timeout) -- see this module's docstring for why. Only truly
    unexpected errors (e.g. a bug, or an exception type this function
    doesn't know about) propagate.

    `on_progress`: OPTIONAL, added for the RAMR novelty's Phase 1 (Mission
    State Management, see app/mission/state.py) -- an optional
    `(current: int, total: int) -> None` callback invoked every time a
    new `MissionProgress` is observed during the monitor stage. Every
    existing call site that omits it (Phase 1-8 behavior) is completely
    unaffected -- this parameter defaults to `None` and is a pure
    addition, not a change to any existing code path.
    """
    # Lazy import -- same reasoning as every other MAVSDK/provider import
    # in this project (app/llm/llm_client.py, app/mission/converter.py):
    # importing this module shouldn't require `mavsdk` to be installed
    # unless a connection is actually attempted.
    from mavsdk import System

    address = system_address or settings.px4_connection_url
    connect_timeout = connect_timeout_s if connect_timeout_s is not None else settings.px4_connect_timeout_s

    drone = System()
    try:
        await drone.connect(system_address=address)
        await _wait_for_connection(drone, connect_timeout, address)
    except PX4ConnectionError as exc:
        return MissionExecutionResult(accepted=False, stage="connect", error=str(exc))

    return await execute_mission_on_system(
        drone, mission, connect_timeout_s=connect_timeout,
        mission_timeout_s=mission_timeout_s, on_progress=on_progress,
    )


async def execute_mission_on_system(
    drone,
    mission: Mission,
    *,
    connect_timeout_s: float | None = None,
    mission_timeout_s: float = 180.0,
    on_progress=None,
    on_stage=None,
    preupload_check=None,
) -> MissionExecutionResult:
    """
    Everything `run_mission_on_px4()` does AFTER the connection is up --
    preflight, convert, upload, arm, start, monitor -- on an ALREADY
    connected MAVSDK `System`. Split out (UI phase) so a long-lived
    connection (app/mavlink/drone_link.py) can fly a mission while the
    same connection also streams telemetry and accepts Return-to-Launch /
    Hold. `run_mission_on_px4()` calls this unchanged, so its behaviour is
    identical to before.

    Optional, UI-only hooks (default None = old behaviour):
      on_stage(stage)                  -- "preflight", "upload", "arm", "start", "monitor"
      preupload_check(home_lat, home_lon) -> error str | None
                                       -- refuse BEFORE uploading (e.g. the
                                          mission is far from PX4's home)
    """
    from mavsdk.action import ActionError
    from mavsdk.mission import MissionError

    connect_timeout = connect_timeout_s if connect_timeout_s is not None else settings.px4_connect_timeout_s

    def stage(name: str) -> None:
        if on_stage is not None:
            on_stage(name)

    stage("preflight")
    try:
        await _wait_for_preflight_ready(drone, connect_timeout)
        home_latitude_deg, home_longitude_deg = await _get_home_position(drone, connect_timeout)
    except PX4ConnectionError as exc:
        return MissionExecutionResult(accepted=False, stage="preflight", error=str(exc))

    if preupload_check is not None:
        problem = preupload_check(home_latitude_deg, home_longitude_deg)
        if problem:
            return MissionExecutionResult(accepted=False, stage="preflight", error=problem)

    try:
        converted = to_mavsdk_mission_plan(
            mission,
            home_latitude_deg=home_latitude_deg,
            home_longitude_deg=home_longitude_deg,
        )
    except MissionConversionError as exc:
        return MissionExecutionResult(accepted=False, stage="convert", error=str(exc))

    stage("upload")
    try:
        await drone.mission.upload_mission(converted.plan)
        await drone.mission.set_return_to_launch_after_mission(
            converted.return_to_launch_after_mission
        )
    except MissionError as exc:
        return MissionExecutionResult(
            accepted=False, stage="upload", error=str(exc),
            return_to_launch_after_mission=converted.return_to_launch_after_mission,
        )

    stage("arm")
    try:
        await drone.action.arm()
    except ActionError as exc:
        return MissionExecutionResult(
            accepted=False, stage="arm", error=str(exc),
            return_to_launch_after_mission=converted.return_to_launch_after_mission,
        )

    stage("start")
    try:
        await drone.mission.start_mission()
    except MissionError as exc:
        return MissionExecutionResult(
            accepted=False, stage="start", error=str(exc),
            return_to_launch_after_mission=converted.return_to_launch_after_mission,
        )

    stage("monitor")
    try:
        progress_log = await _monitor_mission(drone, mission_timeout_s, on_progress=on_progress)
    except PX4MissionExecutionError as exc:
        return MissionExecutionResult(
            accepted=False, stage="monitor", error=str(exc),
            progress_log=exc.progress_log,
            return_to_launch_after_mission=converted.return_to_launch_after_mission,
        )

    return MissionExecutionResult(
        accepted=True, stage="complete", error=None,
        progress_log=progress_log,
        return_to_launch_after_mission=converted.return_to_launch_after_mission,
    )


def _demo_cli() -> None:
    """
    Manual test entry point -- runs the FULL Phase 3-8 pipeline end to
    end against a running PX4 SITL instance:
        python -m app.mavlink.px4_client "Take off, fly to one waypoint at 30 meters, and return home."
    Needs: the configured LLM provider reachable (see Phase 3's README
    section) AND PX4 SITL already running and reachable at
    PX4_CONNECTION_URL (see this file's Phase 7/8 README section).
    """
    import json
    import sys

    from app.llm.llm_client import LLMGenerationError
    from app.mission.generator import generate_validated_mission

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    request = " ".join(sys.argv[1:]) or (
        "Take off, fly to one waypoint at 30 meters, and return home."
    )
    print(f"Request: {request}\n")

    try:
        generation = generate_validated_mission(request)
    except LLMGenerationError as exc:
        print(f"\nLLM generation failed: {exc}")
        raise SystemExit(1)

    print(generation.mission.model_dump_json(indent=2))
    print(f"\nValidation: {json.dumps(generation.validation.as_dict(), indent=2)}")

    if not generation.accepted:
        print("\nMission failed rule validation -- not sending to PX4 SITL.")
        raise SystemExit(1)

    print("\nConnecting to PX4 SITL and running the mission...")
    result = asyncio.run(run_mission_on_px4(generation.mission))

    print(f"\nStage: {result.stage}")
    print(f"Accepted: {result.accepted}")
    if result.error:
        print(f"Error: {result.error}")
    if result.progress_log:
        print(f"Final progress: {result.progress_log[-1][0]}/{result.progress_log[-1][1]}")
    if not result.accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    _demo_cli()
