"""
Terminal test: plan a mission, then check whether the flight controller
ACCEPTS it -- without arming or flying.

    # Pixhawk on USB (close QGroundControl first -- it holds the port):
    python scripts/test_upload.py "Inspect Ripon Building, Chennai at 40 meters" --drone serial://COM5:57600

    # Same mission without the camera fields (heading / gimbal / photo), to compare:
    python scripts/test_upload.py "Inspect Ripon Building, Chennai at 40 meters" --drone serial://COM5:57600 --no-camera

    # PX4 SITL: upload, then actually fly it (refused for serial:// -- use the web UI for a real drone):
    python scripts/test_upload.py "Inspect Ripon Building, Chennai at 40 meters" --fly

What it does:
1. Plans the mission with the normal pipeline (location -> outline -> ring -> LLM -> validation -> camera).
2. Connects, prints the PX4 firmware version and the parameters that matter
   (MIS_YAWMODE, MIS_DIST_1WP), the GPS fix and the home position if known.
3. Uploads the mission. PX4 either accepts it or returns a rejection reason.
4. Downloads it back and checks the item count -- proof it is stored on the board.
Nothing is armed unless --fly is given (SITL only). The uploaded mission
stays on the board until replaced; pass --clear to remove it afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable from any cwd

from app.config import settings  # noqa: E402
from app.location.geofence import haversine_km  # noqa: E402
from app.llm.llm_client import LLMGenerationError  # noqa: E402
from app.mission.location_aware_generator import generate_mission_from_natural_language  # noqa: E402


def _strip_camera(mission):
    items = [i.model_copy(update={"yaw_deg": None, "gimbal_pitch_deg": None, "take_photo": None}) for i in mission.mission_items]
    return mission.model_copy(update={"mission_items": items})


async def _first(stream, timeout_s: float):
    async def _get():
        async for value in stream:
            return value
    try:
        return await asyncio.wait_for(_get(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None


async def _run(mission, address: str, *, fly: bool, clear: bool) -> int:
    from mavsdk import System
    from mavsdk.mission import MissionError

    from app.mavlink.px4_client import PX4ConnectionError, _wait_for_connection, execute_mission_on_system
    from app.mission.converter import to_mavsdk_mission_plan

    print(f"\nConnecting to {address} ...")
    drone = System()
    await drone.connect(system_address=address)
    try:
        await _wait_for_connection(drone, settings.px4_connect_timeout_s, address)
    except PX4ConnectionError as exc:
        print(f"NOT CONNECTED: {exc}")
        return 2
    print("Connected.")

    # --- what is on the other end -----------------------------------------
    try:
        v = await drone.info.get_version()
        print(f"Firmware: PX4 {v.flight_sw_major}.{v.flight_sw_minor}.{v.flight_sw_patch}")
    except Exception as exc:  # noqa: BLE001 -- info is optional
        print(f"Firmware: unknown ({exc})")
    for name, kind in (("MIS_YAWMODE", "int"), ("MIS_DIST_1WP", "float")):
        try:
            value = await (drone.param.get_int_param(name) if kind == "int" else drone.param.get_float_param(name))
            print(f"{name} = {value}")
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: not readable ({exc})")
    gps = await _first(drone.telemetry.gps_info(), 5)
    print(f"GPS: {gps.fix_type} with {gps.num_satellites} satellites" if gps else "GPS: no data")
    home = None
    pos = await _first(drone.telemetry.home(), 5)
    if pos is not None and abs(pos.latitude_deg) > 1e-6:
        home = (pos.latitude_deg, pos.longitude_deg)
        print(f"Home: {home[0]:.6f}, {home[1]:.6f}")
        first = next((i for i in mission.mission_items if i.latitude is not None), None)
        if first is not None:
            d = haversine_km(home[0], home[1], first.latitude, first.longitude) * 1000
            print(f"First waypoint is {d:.0f} m from home (PX4 refuses more than MIS_DIST_1WP).")
    else:
        print("Home: not set (no GPS fix yet) -- PX4 can't check the first-waypoint distance until it has one.")

    if fly:
        print("\nFlying (SITL) ...")
        result = await execute_mission_on_system(drone, mission)
        print(f"Stage: {result.stage}  Accepted: {result.accepted}  Error: {result.error or '-'}")
        return 0 if result.accepted else 1

    # --- upload only ---------------------------------------------------------
    converted = to_mavsdk_mission_plan(
        mission, home_latitude_deg=home[0] if home else None, home_longitude_deg=home[1] if home else None
    )
    sent = len(converted.plan.mission_items)
    print(f"\nUploading {sent} mission items (not arming) ...")
    try:
        await drone.mission.upload_mission(converted.plan)
    except MissionError as exc:
        print(f"REJECTED by the flight controller: {exc}")
        return 1
    stored = await drone.mission.download_mission()
    got = len(stored.mission_items)
    print(f"ACCEPTED: the flight controller stored {got} of {sent} items." if got == sent
          else f"UPLOADED but read back {got} of {sent} items -- check the mission in QGroundControl.")
    if clear:
        await drone.mission.clear_mission()
        print("Mission cleared from the flight controller.")
    return 0 if got == sent else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="+")
    parser.add_argument("--drone", default=settings.px4_connection_url,
                        help="e.g. serial://COM5:57600 (Pixhawk USB/radio) or udpin://0.0.0.0:14540 (SITL, default)")
    parser.add_argument("--home", nargs=2, type=float, metavar=("LAT", "LON"), help="home for planning checks")
    parser.add_argument("--no-camera", action="store_true", help="drop heading/gimbal/photo before uploading")
    parser.add_argument("--fly", action="store_true", help="SITL only: arm and fly after planning")
    parser.add_argument("--clear", action="store_true", help="remove the mission from the board after the check")
    parser.add_argument("--save", metavar="FILE", help="also write the planned mission + inspection data as JSON")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if args.fly and args.drone.lower().startswith("serial"):
        print("--fly is for SITL only. For a real drone use the web UI (python -m app.api.main), which asks for confirmation.")
        return 2

    request = " ".join(args.request)
    print(f"Planning: {request}")
    try:
        result = generate_mission_from_natural_language(request, home_position=tuple(args.home) if args.home else None)
    except LLMGenerationError as exc:
        print(f"LLM unavailable: {exc}")
        return 2

    summary = result.inspection_summary() or {}
    b = summary.get("building") or {}
    print(f"Status: {result.status.value}  accepted={result.accepted}")
    if b:
        print(f"Building: {b.get('name')}  outline: {b.get('outline_source') or 'none (point only)'}"
              f"{'  id ' + str(b.get('outline_id')) if b.get('outline_id') else ''}"
              f"{'  confidence %.2f' % b['outline_confidence'] if b.get('outline_confidence') is not None else ''}")
    if result.location_validation:
        for e in result.location_validation.errors:
            print(f"  ERROR: {e}")
        for w in result.location_validation.warnings:
            print(f"  warning: {w}")
    if not result.accepted:
        print(f"Not uploading: {result.message}")
        return 1

    mission = result.mission_result.mission
    if args.no_camera:
        mission = _strip_camera(mission)
    for i in mission.mission_items:
        cam = f"  yaw {i.yaw_deg:g}  gimbal {i.gimbal_pitch_deg:g}  photo" if i.yaw_deg is not None else ""
        pos = f"{i.latitude:.6f}, {i.longitude:.6f}" if i.latitude is not None else "-"
        print(f"  #{i.sequence:<3} {i.command.value:<17} {pos:<24} {i.altitude if i.altitude is not None else '-'} m{cam}")
    if args.save:
        Path(args.save).write_text(json.dumps({"mission": mission.model_dump(), "inspection": summary}, indent=2, default=str))
        print(f"Saved: {args.save}")

    return asyncio.run(_run(mission, args.drone, fly=args.fly, clear=args.clear))


if __name__ == "__main__":
    raise SystemExit(main())
