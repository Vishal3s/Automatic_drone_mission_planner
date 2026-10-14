"""
RAMR novelty Phase 9 -- Mid-Flight Mission Update Manager.

    OBJECTIVE: Safely update the active PX4 mission. THIS IS A CRITICAL
    PHASE.
    CREATE: MissionUpdateManager
    RESPONSIBILITIES: (1) Detect active replanning state. (2) Prevent
    multiple simultaneous replanning requests. (3) Get current mission
    progress. (4) Identify completed waypoints. (5) Generate updated
    remaining mission. (6) Safely pause or hold the drone if required.
    (7) Upload the revised mission. (8) Ensure the mission does not
    restart from WP1. (9) Resume execution. (10) Verify mission progress
    after update.
    RESEARCH AND TEST THE ACTUAL BEHAVIOR OF: MAVSDK Mission API, PX4
    SITL, mission pause, mission clear, mission upload, current mission
    item, mission resume. DO NOT ASSUME MID-FLIGHT MISSION REPLACEMENT
    WORKS. TEST IT SEPARATELY FIRST.
    SUCCESS CRITERIA: the drone does not restart from WP1, does not fly
    backwards unnecessarily, does not duplicate completed waypoints,
    safely continues from the current position, and completes the
    updated mission. DO NOT PROCEED UNTIL THIS WORKS RELIABLY.

===============================================================================
RESEARCH -- what was actually verified before writing this module, and
what was NOT (read this before trusting the sequence below)
===============================================================================

Every claim below was checked against the current MAVSDK-Python "Mission"
plugin reference (mavsdk-python-docs.s3-website.../plugins/mission.html,
mirroring mavsdk.mavlink.io) and the MAVSDK C++ "Missions" guide
(mavsdk.mavlink.io/main/en/cpp/guide/missions.html) while writing this
file -- not assumed from memory, per your instruction:

1. `pause_mission()` -- documented: "Pause the mission. Pausing the
   mission puts the vehicle into HOLD mode. A multicopter should just
   hover at the spot ... while paused." This is THE documented way to
   safely hold the drone (responsibility 6) -- not `action.hold()` or
   anything else, and it's why this module pauses BEFORE touching the
   mission at all.
2. `clear_mission()` -- documented only as: "Clear the mission saved on
   the vehicle." Nothing in the docs says whether `upload_mission()`
   already implicitly replaces the whole item list on its own (the
   underlying MAVLink mission-upload transaction is a full
   MISSION_COUNT/MISSION_ITEM exchange that logically redefines the
   entire list already) -- i.e. whether an explicit `clear_mission()`
   before `upload_mission()` is even necessary. This module calls it
   anyway, AFTER pausing, as defense-in-depth against any leftover
   mission state -- but this specific ordering (clear while paused, vs.
   skipping clear and just re-uploading) is exactly the kind of thing
   your spec says "DO NOT ASSUME ... TEST IT SEPARATELY FIRST" about,
   and it has NOT been exercised against a real PX4 SITL instance in
   this environment -- see "WHAT WAS NOT VERIFIED" below.
3. `upload_mission(mission_plan)` -- documented: "Upload a list of
   mission items to the system... Once uploaded the mission can be
   started and executed even if the connection is lost." No documented
   restriction on calling this while armed/flying, but also no
   documented GUARANTEE that doing so is safe -- see the open MAVSDK
   issue below.
4. `set_current_mission_item(index)` -- documented: "Sets the mission
   item index to go to. By setting the current index to 0, the mission
   is restarted from the beginning." This is the load-bearing fact this
   whole design leans on: the index is relative to WHATEVER PLAN IS
   CURRENTLY LOADED, not to some persistent global waypoint count. So a
   freshly uploaded remaining-mission plan's OWN item 0 is "the
   beginning" of THAT plan -- which is exactly the "current position /
   new waypoint" item this project's replanning pipeline (Phase 4/6/7)
   always puts first, never the original mission's WP1. This module
   deliberately does NOT call `set_current_mission_item()` at all: after
   `upload_mission()`, the freshly uploaded plan's current item already
   defaults to its own item 0 (confirmed by the same doc line -- "the
   mission is restarted from the beginning" describes what index 0
   MEANS, not an action you must additionally take), so calling
   `start_mission()` directly already resumes from the new plan's first
   item, without ever touching an index that could be misread as
   "restart from the ORIGINAL WP1."
5. `start_mission()` -- documented (guide, not just reference): "Start
   or resume a paused mission using Mission::start_mission." Confirms
   this is the single, correct resume call from a `pause_mission()` HOLD
   -- there is no separate "resume" method.
6. `mission_progress()` -- documented only as "Subscribe to mission
   progress updates," with no notes on behavior across a re-upload. This
   module treats a post-update `MissionProgress.total` reading as
   referring to the NEWLY uploaded plan (not the pre-update one) --
   consistent with fact 4 above -- and uses that as its verification
   signal (responsibility 10): the first progress reading after resume
   should report a `total` matching the newly uploaded plan's item
   count, not the old one.
7. A real, closed-but-unresolved MAVSDK feature request
   (github.com/mavlink/MAVSDK-Python/issues/442) asks for exactly this
   capability -- pause a mission, redirect, resume from the next
   waypoint -- and has NO implementation notes or confirmed working
   pattern attached. This independently confirms your spec's caution is
   warranted, not overcautious.
8. A REAL, closed PX4 bug (github.com/PX4/PX4-Autopilot/issues/22491)
   documents that PX4 computes mission altitude RELATIVE TO THE VEHICLE'S
   CURRENT HOME POSITION at the time a mission runs -- moving/re-reading
   home mid-flight measurably corrupted altitude in a reported case
   (drone descended or even crashed depending on the altitude delta).
   Direct, concrete consequence for this module: it NEVER re-queries
   `telemetry.home()` for a mid-flight update -- it requires the SAME
   `home_latitude_deg`/`home_longitude_deg` the ORIGINAL mission upload
   already used to be passed in by the caller (see `apply_update()`'s
   parameters), rather than fetching a fresh home position that could
   have drifted or been re-reported differently mid-flight.

WHAT WAS **NOT** VERIFIED (read this before trusting this module
"reliably" per your DO NOT PROCEED instruction):

This cloud sandbox has no PX4 SITL / Gazebo / jMAVSim instance available
-- there is no realistic flight-dynamics simulator this environment can
run, and this project's own existing PX4-touching tests (going back to
Phase 7/8, see `tests/test_phase7_8_px4_client.py`) have ALWAYS run
against a hand-built fake `mavsdk` module, never a real SITL instance, in
this environment -- real SITL testing in this project has only ever
happened on YOUR machine (the PX4_CONNECTION_URL/terminal output you
pasted earlier in this conversation). This module is built the same
way -- see `tests/test_phase_mission_update_manager.py`, which extends
that SAME fake-MAVSDK pattern to assert the exact CALL ORDER and DATA
this module produces (pause before clear/upload, no TAKEOFF item ever
uploaded, no `set_current_mission_item()` call, resume via
`start_mission()`, verification reads `mission_progress()` against the
NEW plan's item count) -- but a fake can only prove this module calls
the documented API correctly, in a sensible order. It CANNOT prove PX4
actually behaves as documented when a real vehicle receives
pause->clear->upload->start mid-flight -- that is real flight-controller
behavior no fake can simulate, and per fact 2/7 above, is NOT fully
documented even by MAVSDK/PX4 themselves.

Per your explicit instruction, this module therefore does NOT claim to
be proven reliable. Before this is used for anything beyond further
software-only integration, run `tests/test_phase_mission_update_manager
.py`'s exact TEST SCENARIO (WP1->WP2->WP3->WP4->WP5, trigger at WP3)
against YOUR real PX4 SITL setup and confirm, by watching the simulated
vehicle (e.g. QGroundControl or the SITL console), that it does not
restart from WP1, does not fly backwards unnecessarily, does not
duplicate completed waypoints, and completes the updated mission -- the
SUCCESS CRITERIA your spec states, verified by observation, not just by
this module's return value. `_demo_cli()` below is written to make that
manual SITL run easy to do.

Sources consulted while writing this module:
- http://mavsdk-python-docs.s3-website.eu-central-1.amazonaws.com/plugins/mission.html
- https://mavsdk.mavlink.io/main/en/cpp/guide/missions.html
- https://github.com/mavlink/MAVSDK-Python/issues/442
- https://github.com/PX4/PX4-Autopilot/issues/22491

===============================================================================
Design notes (how the 10 responsibilities map to code)
===============================================================================

1/2. Detect active replanning state / prevent concurrent requests: TWO
   independent layers, deliberately redundant. (a) An `asyncio.Lock` per
   `MissionUpdateManager` instance -- `apply_update()` checks
   `lock.locked()` and REJECTS immediately (does not queue/wait) if an
   update is already running, matching "prevent," not "serialize." (b)
   `MissionStateManager.start_replanning()` (Phase 1, reused unchanged)
   -- its own `_transition()` guard already refuses to move
   REPLANNING -> REPLANNING or UPDATING -> REPLANNING, so even a second
   `MissionUpdateManager` instance sharing the same `MissionStateManager`
   is refused. Neither layer is new state-tracking invented here.
3/4. Get current mission progress / identify completed waypoints:
   NOT recomputed -- read directly off `MissionStateManager.snapshot()`
   (Phase 1's `current_waypoint`/`completed_waypoints`/
   `remaining_waypoints`, already kept live by `px4_client.py`'s
   `on_progress` hook). This module trusts that existing shared state,
   exactly like Phase 5's `ReplanningContextBuilder` already does.
5. Generate updated remaining mission: NOT done here either -- this
   module RECEIVES an already safety-VALIDATED `Mission` (the output of
   RAMR novelty Phase 7's `app.replanning.safety_validator
   .enforce_before_mavsdk()`) as `approved_mission`. Reusing Phases 4-7
   rather than re-deriving a route here also keeps this project's
   standing rule intact: a safety-critical action (what gets uploaded to
   a flying vehicle) is never produced by this module calling an LLM
   itself.
6. Safely pause or hold: `drone.mission.pause_mission()`, always, before
   `clear_mission()`/`upload_mission()` -- see research point 1/2 above:
   pausing first means any transient "no mission loaded" state during
   clear+upload happens while the vehicle is safely hovering in HOLD,
   not while PX4's MISSION flight mode is actively trying to navigate a
   list that might momentarily be empty.
7. Upload the revised mission: `clear_mission()` then
   `upload_mission()` then `set_return_to_launch_after_mission()`, reusing
   `app.mission.converter.to_mavsdk_mission_plan()` (Phase 6) exactly as
   `px4_client.py` already does -- no second converter.
8. Ensure the mission does not restart from WP1: enforced THREE ways.
   (a) `approved_mission` already passed through
   `app.mission.validator.validate_remaining_mission()` (Phase 4/5)
   upstream, which REJECTS a TAKEOFF item outright -- WP1 in this
   project's convention is always a TAKEOFF item (see
   `app/mission/schema.py`/`validator.py`), so a rule-valid remaining
   mission structurally cannot BE the original mission replayed from the
   start. (b) This module independently re-checks that defensively
   (`_reject_if_starts_with_takeoff()`) before ever touching the drone --
   the same "don't just trust an earlier layer" pattern Phase 7's
   corrective-waypoint grounding check already established. (c) Per
   research point 4, `set_current_mission_item()` is deliberately NEVER
   called -- nothing here can accidentally re-select index 0 of a STALE
   plan, because no plan besides the freshly uploaded one is ever
   addressed by index.
9. Resume execution: `drone.mission.start_mission()` -- per research
   point 5, the single documented resume call from a `pause_mission()`
   HOLD.
10. Verify mission progress after update: `_verify_progress()` -- reads
    `mission_progress()` (bounded by a timeout, like every other wait in
    this project) and confirms the observed `total` matches the newly
    uploaded plan's item count, i.e. that telemetry is now reporting
    against the NEW plan, not stale progress from the old one.

Failure recovery (not explicitly asked for by the RESPONSIBILITIES list,
but required by "safely" in the OBJECTIVE and this project's standing
safety-first rule): if `clear_mission()` has already succeeded by the
time something later fails (upload rejected, start rejected, or
verification fails), the OLD plan is genuinely gone from the vehicle --
`MissionStateManager.fail_replan()`'s normal assumption ("the drone
keeps flying its old plan, nothing was actually applied") no longer
holds. `_attempt_restore_previous_mission()` re-uploads the PRE-UPDATE
remaining mission (captured via `MissionStateManager.remaining_route()`,
Phase 1's own scaffolding, built before this phase ever touches
anything) and resumes it. `MissionUpdateResult
.flight_controller_mission_restored` tells the caller honestly whether
that recovery succeeded, failed, or was never needed (`None`) -- if
restore also fails, the vehicle is deliberately left PAUSED in HOLD
(hovering) rather than in an undefined state; HOLD is this module's
worst-case-safe fallback, consistent with RAMR novelty Phase 7's own
ABORT/HOLD-style last resort.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.mission.converter import MissionConversionError, to_mavsdk_mission_plan
from app.mission.schema import Mission, MissionCommand
from app.mission.state import MissionStateError, MissionStateManager

log = logging.getLogger(__name__)


@dataclass
class MissionUpdateResult:
    """What `MissionUpdateManager.apply_update()` always returns."""

    accepted: bool
    # Which stage the result reflects: "guard", "precondition", "pause",
    # "convert", "clear", "upload", "resume", "verify", or "complete"
    # (only "complete" implies accepted=True).
    stage: str
    error: str | None
    verified_progress: tuple[int, int] | None = None
    # Only meaningful when accepted=False AND clear_mission() had already
    # run: True = the pre-update remaining mission was successfully
    # re-uploaded and resumed; False = restore was attempted and failed
    # (vehicle left paused/hovering); None = restore was never needed
    # (failure happened before clear_mission() ran, so the old plan was
    # never touched).
    flight_controller_mission_restored: bool | None = None


class MissionUpdateManager:
    """
    Usage:
        manager = MissionUpdateManager()
        result = await manager.apply_update(
            drone=drone,                          # an already-connected mavsdk.System, mid-flight
            mission_state_manager=state_manager,   # Phase 1, already tracking the ACTIVE mission
            approved_mission=safety_result.approved_mission,  # Phase 7's enforce_before_mavsdk() output
            home_latitude_deg=home_lat, home_longitude_deg=home_lon,  # the SAME home used for the original upload
            return_to_launch_after_mission=True,
        )
        if result.accepted:
            ...  # caller resumes its own mission_progress() monitoring loop (e.g. px4_client._monitor_mission)
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @staticmethod
    def is_update_in_progress(mission_state_manager: MissionStateManager) -> bool:
        """Responsibility 1, exposed as a standalone check a caller can use before even trying."""
        state = mission_state_manager.snapshot()
        return state.mission_status.value in ("REPLANNING", "UPDATING")

    async def apply_update(
        self,
        *,
        drone,
        mission_state_manager: MissionStateManager,
        approved_mission: Mission,
        home_latitude_deg: float,
        home_longitude_deg: float,
        return_to_launch_after_mission: bool | None = None,
        pause_settle_s: float = 1.0,
        progress_verify_timeout_s: float = 15.0,
    ) -> MissionUpdateResult:
        # Lazy import -- same reasoning as every other MAVSDK import in this project.
        from mavsdk.mission import MissionError

        # -- Responsibility 2: prevent multiple simultaneous replanning requests (layer 1: this manager's own lock) --
        if self._lock.locked():
            return MissionUpdateResult(
                accepted=False, stage="guard",
                error="A mission update is already in progress on this MissionUpdateManager; "
                "rejecting the concurrent request rather than queuing it.",
            )

        async with self._lock:
            # -- Responsibilities 1 & 2, layer 2: the existing MissionState state machine (Phase 1) --
            try:
                mission_state_manager.start_replanning()  # RUNNING -> REPLANNING; raises if not RUNNING
            except MissionStateError as exc:
                return MissionUpdateResult(accepted=False, stage="guard", error=str(exc))

            # Capture the PRE-UPDATE remaining route now, while it's still what MissionState reports --
            # this is the recovery fallback if something fails after clear_mission() has already run.
            previous_remaining_mission = mission_state_manager.remaining_route()

            # -- Responsibility 8 (defense in depth #2 -- see module docstring): never accept a
            # revised mission that starts with TAKEOFF, i.e. a replay of the ORIGINAL mission from WP1.
            if self._starts_with_takeoff(approved_mission):
                mission_state_manager.fail_replan(
                    "approved_mission's first item is TAKEOFF -- refusing to upload what would "
                    "restart the mission from WP1."
                )
                return MissionUpdateResult(
                    accepted=False, stage="precondition",
                    error="approved_mission must not start with TAKEOFF (would restart from WP1).",
                )

            mission_state_manager.begin_update()  # REPLANNING -> UPDATING

            # -- Responsibility 6: safely pause/hold the drone BEFORE touching the mission at all --
            try:
                await drone.mission.pause_mission()
                if pause_settle_s > 0:
                    await asyncio.sleep(pause_settle_s)
            except MissionError as exc:
                mission_state_manager.fail_replan(f"pause_mission() failed: {exc}")
                return MissionUpdateResult(accepted=False, stage="pause", error=str(exc))

            # -- Convert the approved remaining mission using the SAME home position as the
            # original upload (see research point 8 -- never re-query telemetry.home() mid-flight) --
            try:
                converted = to_mavsdk_mission_plan(
                    approved_mission,
                    home_latitude_deg=home_latitude_deg,
                    home_longitude_deg=home_longitude_deg,
                )
            except MissionConversionError as exc:
                restored = await self._attempt_restore_previous_mission(
                    drone, previous_remaining_mission, home_latitude_deg, home_longitude_deg
                )
                mission_state_manager.fail_replan(f"conversion failed: {exc}")
                return MissionUpdateResult(
                    accepted=False, stage="convert", error=str(exc),
                    flight_controller_mission_restored=restored,
                )

            # -- Responsibility 7, part 1: clear the vehicle's stored mission (see research point 2) --
            try:
                await drone.mission.clear_mission()
            except MissionError as exc:
                # Nothing destructive has happened to the FC's mission yet if clear() itself
                # raised (it either cleared or it didn't) -- but treat it conservatively and
                # still attempt restore, since we cannot be certain from here.
                restored = await self._attempt_restore_previous_mission(
                    drone, previous_remaining_mission, home_latitude_deg, home_longitude_deg
                )
                mission_state_manager.fail_replan(f"clear_mission() failed: {exc}")
                return MissionUpdateResult(
                    accepted=False, stage="clear", error=str(exc),
                    flight_controller_mission_restored=restored,
                )

            # -- Responsibility 7, part 2: upload the revised mission. Past this point, the OLD
            # plan is genuinely gone -- any later failure MUST attempt restore. --
            try:
                await drone.mission.upload_mission(converted.plan)
                # RAMR novelty Phase 10 integration-testing fix: prefer the RTL flag the
                # converter itself derived from `approved_mission`'s actual content
                # (matches the ORIGINAL Phase 7/8 px4_client.py precedent -- it always uses
                # `converted.return_to_launch_after_mission`, never a separately-supplied
                # value) -- `return_to_launch_after_mission` here is an OPTIONAL override for
                # a caller with a specific reason to force it, not a required, easily-out-of-
                # sync duplicate of what the mission content already implies.
                effective_rtl_flag = (
                    return_to_launch_after_mission
                    if return_to_launch_after_mission is not None
                    else converted.return_to_launch_after_mission
                )
                await drone.mission.set_return_to_launch_after_mission(effective_rtl_flag)
            except MissionError as exc:
                restored = await self._attempt_restore_previous_mission(
                    drone, previous_remaining_mission, home_latitude_deg, home_longitude_deg
                )
                mission_state_manager.fail_replan(f"upload_mission() failed: {exc}")
                return MissionUpdateResult(
                    accepted=False, stage="upload", error=str(exc),
                    flight_controller_mission_restored=restored,
                )

            # -- Responsibility 9: resume execution. Per research point 4/5: no
            # set_current_mission_item() call -- the freshly uploaded plan already
            # defaults to its own item 0, and start_mission() is the documented resume call. --
            try:
                await drone.mission.start_mission()
            except MissionError as exc:
                restored = await self._attempt_restore_previous_mission(
                    drone, previous_remaining_mission, home_latitude_deg, home_longitude_deg
                )
                mission_state_manager.fail_replan(f"start_mission() (resume) failed: {exc}")
                return MissionUpdateResult(
                    accepted=False, stage="resume", error=str(exc),
                    flight_controller_mission_restored=restored,
                )

            # -- Responsibility 10: verify mission progress after update --
            expected_total = len(converted.plan.mission_items)
            verified, observed = await self._verify_progress(drone, expected_total, progress_verify_timeout_s)
            if not verified:
                # The new mission IS active on the flight controller at this point (start_mission()
                # succeeded) -- there is no old plan left to restore to, and un-starting an active
                # mission is not something this module attempts. Report failure honestly; the
                # vehicle is flying the new mission but this module could not CONFIRM it via telemetry.
                mission_state_manager.fail_replan(
                    f"Could not verify mission progress after update within {progress_verify_timeout_s}s "
                    f"(expected total={expected_total}, observed={observed})."
                )
                return MissionUpdateResult(
                    accepted=False, stage="verify",
                    error=f"Mission progress verification failed (expected total={expected_total}, observed={observed}).",
                    verified_progress=observed,
                )

            # -- Complete: commit the update to MissionState (Phase 1) --
            mission_state_manager.apply_replan(approved_mission)
            return MissionUpdateResult(accepted=True, stage="complete", error=None, verified_progress=observed)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _starts_with_takeoff(mission: Mission) -> bool:
        items = mission.mission_items
        return bool(items) and items[0].command == MissionCommand.TAKEOFF

    @staticmethod
    async def _verify_progress(
        drone, expected_total: int, timeout_s: float
    ) -> tuple[bool, tuple[int, int] | None]:
        """
        Reads `mission_progress()` (bounded) and confirms the first
        observed reading's `total` matches the freshly uploaded plan's
        item count -- i.e. telemetry is reporting against the NEW plan,
        per research point 6.
        """
        observed: tuple[int, int] | None = None

        async def _watch() -> None:
            nonlocal observed
            async for progress in drone.mission.mission_progress():
                observed = (progress.current, progress.total)
                return

        try:
            await asyncio.wait_for(_watch(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return False, observed

        if observed is None:
            return False, None
        return observed[1] == expected_total, observed

    @staticmethod
    async def _attempt_restore_previous_mission(
        drone, previous_remaining_mission: Mission, home_latitude_deg: float, home_longitude_deg: float
    ) -> bool:
        """
        Best-effort recovery: re-upload and resume the PRE-UPDATE
        remaining mission. Returns True only if upload AND resume both
        succeeded. Never raises -- a failure here just means the vehicle
        is left paused/hovering (this module's safe worst case), which
        the caller learns via `MissionUpdateResult
        .flight_controller_mission_restored=False`.

        RAMR novelty Phase 10 integration-testing fix: also sets the RTL
        flag for the RESTORED mission (previously missing here -- a real
        gap Phase 10's integration testing surfaced: without this, a
        restore could leave PX4 with whatever RTL flag the FAILED update
        attempt last set, which may not match the restored mission's own
        ending). Always uses the value the converter derives from
        `previous_remaining_mission` itself, not an override -- a restore
        has no caller-supplied override to honor.
        """
        from mavsdk.mission import MissionError

        try:
            converted = to_mavsdk_mission_plan(
                previous_remaining_mission,
                home_latitude_deg=home_latitude_deg,
                home_longitude_deg=home_longitude_deg,
            )
            await drone.mission.upload_mission(converted.plan)
            await drone.mission.set_return_to_launch_after_mission(converted.return_to_launch_after_mission)
            await drone.mission.start_mission()
            return True
        except (MissionError, MissionConversionError) as exc:
            log.error("Failed to restore the previous remaining mission after a failed update: %s", exc)
            return False


def _demo_cli() -> None:
    """
    Manual SITL entry point -- reproduces the spec's own TEST SCENARIO
    exactly:
        python -m app.mavlink.mission_update_manager
    Needs PX4 SITL already running and reachable, AND a mission already
    uploaded, armed, and RUNNING up to (at least) WP3 -- this script does
    NOT fly the drone to WP3 for you; it only exercises the update itself,
    so you can drive the original mission via QGroundControl or your own
    script first, then run this once the vehicle is mid-mission.

    THIS IS THE SCRIPT THIS MODULE'S DOCSTRING ASKS YOU TO RUN AGAINST A
    REAL PX4 SITL INSTANCE before relying on this module -- nothing in
    this cloud sandbox can do that for you.
    """
    import asyncio as _asyncio
    import sys

    from app.config import settings
    from app.mission.schema import MissionCommand, MissionItem

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    async def _run() -> None:
        from mavsdk import System

        address = settings.px4_connection_url
        print(f"Connecting to PX4 SITL at {address} ...")
        drone = System()
        await drone.connect(system_address=address)
        async for state in drone.core.connection_state():
            if state.is_connected:
                break
        async for health in drone.telemetry.health():
            if health.is_global_position_ok and health.is_home_position_ok:
                break
        home_lat, home_lon = None, None
        async for position in drone.telemetry.home():
            home_lat, home_lon = position.latitude_deg, position.longitude_deg
            break
        print(f"Connected. Home position: {home_lat}, {home_lon}")
        print(
            "Make sure a mission (WP1..WP5) is already uploaded, armed, and RUNNING, "
            "at or near WP3, before continuing.\n"
        )

        # The TEST SCENARIO's "Updated Mission": CURRENT POSITION -> NEW WAYPOINT -> WP4 -> WP5 -> RTH.
        # WP4/WP5 coordinates below are placeholders -- edit to match your own original mission.
        approved_mission = Mission(
            mission_name="Mid-flight update demo",
            mission_items=[
                MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=home_lat, longitude=home_lon, altitude=30),  # "NEW WAYPOINT"
                MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=home_lat, longitude=home_lon, altitude=30),  # "WP4"
                MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=home_lat, longitude=home_lon, altitude=30),  # "WP5"
                MissionItem(sequence=4, command=MissionCommand.RETURN_TO_LAUNCH),
            ],
        )

        manager = MissionStateManager()
        manager.start(approved_mission)  # standalone demo: not wired to a real prior MissionState here
        manager.mark_validated()
        manager.mark_uploaded()
        manager.mark_running()

        update_manager = MissionUpdateManager()
        result = await update_manager.apply_update(
            drone=drone,
            mission_state_manager=manager,
            approved_mission=approved_mission,
            home_latitude_deg=home_lat,
            home_longitude_deg=home_lon,
            return_to_launch_after_mission=True,
        )
        print(f"\nStage: {result.stage}")
        print(f"Accepted: {result.accepted}")
        if result.error:
            print(f"Error: {result.error}")
        print(f"Verified progress: {result.verified_progress}")
        print(f"Flight controller mission restored (if a failure occurred): {result.flight_controller_mission_restored}")
        if not result.accepted:
            sys.exit(1)

    _asyncio.run(_run())


if __name__ == "__main__":
    _demo_cli()
