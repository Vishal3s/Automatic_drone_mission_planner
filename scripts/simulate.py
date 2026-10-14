"""
One command: plain-English request -> plan -> ArduCopter simulator flies it.

    python scripts/simulate.py "Inspect Ripon Building, Chennai at 40 meters"

What happens, automatically:
1. The planner plans the mission (location, building outline, ring, LLM, safety checks, camera).
2. Mission Planner's ArduCopter simulator starts with a quadcopter at the first
   waypoint (or --home LAT LON), and Mission Planner opens.
3. The planner uploads the mission, arms the copter, switches to AUTO and starts it,
   then prints progress until the copter has landed.

To WATCH it in Mission Planner (it has no command-line option for this), once:
top-right connection list -> TCP -> Connect -> host 127.0.0.1 -> port 5762.

Options:
    --home LAT LON          start position (default: under the first waypoint)
    --no-mission-planner    don't open Mission Planner
    --plan-only             STEP 1: plan, print, and save a Mission Planner file
                            (missions\<name>.waypoints) -- no simulator
Ctrl+C during the flight switches the copter to RTL (return to launch).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable from any cwd

from app.config import settings  # noqa: E402
from app.llm.llm_client import LLMGenerationError  # noqa: E402
from app.mission.location_aware_generator import generate_mission_from_natural_language  # noqa: E402
from app.sim.ardupilot_sitl import (  # noqa: E402
    CopterLink,
    SimulatorError,
    ardupilot_rows,
    default_home,
    open_mission_planner,
    save_waypoints,
    start_simulator,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="+")
    parser.add_argument("--home", nargs=2, type=float, metavar=("LAT", "LON"))
    parser.add_argument("--no-mission-planner", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    # 1. Plan (the brain)
    request = " ".join(args.request)
    print(f"Planning: {request}")
    try:
        result = generate_mission_from_natural_language(request, home_position=tuple(args.home) if args.home else None)
    except LLMGenerationError as exc:
        print(f"LLM unavailable: {exc}")
        return 2
    b = (result.inspection_summary() or {}).get("building") or {}
    print(f"Status: {result.status.value}  accepted={result.accepted}")
    if result.message:
        print(f"Note: {result.message}")
    if b:
        print(f"Building: {b.get('name')}  outline: {b.get('outline_source') or 'none (point only)'}")
    for line in (result.location_validation.errors if result.location_validation else []):
        print(f"  ERROR: {line}")
    if not result.accepted:
        print(f"Not flying: {result.message}")
        return 1
    mission = result.mission_result.mission
    for i in mission.mission_items:
        pos = f"{i.latitude:.6f}, {i.longitude:.6f}" if i.latitude is not None else "-"
        cam = f"  yaw {i.yaw_deg:g}" if i.yaw_deg is not None else ""
        print(f"  #{i.sequence:<3} {i.command.value:<17} {pos:<24} {i.altitude if i.altitude is not None else '-'} m{cam}")
    lat, lon = tuple(args.home) if args.home else default_home(mission)
    home = (lat, lon, settings.sitl_home_alt_m)
    rows = ardupilot_rows(mission, home)
    slug = "".join(ch if ch.isalnum() else "_" for ch in (b.get("name") or mission.mission_name or "mission"))[:40].strip("_")
    project_root = Path(__file__).resolve().parent.parent  # always the project's own missions/ folder, whatever the working directory
    path = save_waypoints(rows, project_root / "missions" / f"{time.strftime('%Y%m%d_%H%M%S')}_{slug}.waypoints")
    print(f"\nSaved for Mission Planner: {path.resolve()}  ({len(rows) - 1} items + home at {lat:.6f}, {lon:.6f})")
    if args.plan_only:
        return 0

    # 2. Simulator + Mission Planner
    try:
        sim = start_simulator(home)
    except SimulatorError as exc:
        print(exc)
        return 2
    print(f"\nSimulator started (quadcopter at {lat:.6f}, {lon:.6f}).")
    if not args.no_mission_planner:
        if open_mission_planner():
            print("Mission Planner opened. To watch: connection list -> TCP -> Connect -> 127.0.0.1 -> 5762.")
        else:
            print("Mission Planner not found (set MISSION_PLANNER_EXE in .env) -- continuing without it.")

    # 3. Upload, arm, fly (the simulator only starts once the planner is connected)
    link = CopterLink()
    try:
        print("Connecting to the simulator ...")
        link.connect()
        print("Waiting for GPS ...")
        link.wait_gps()
        print(f"Uploading {len(rows) - 1} mission items ...")
        link.upload(rows)
        print("Mission accepted. Arming and starting ...")
        link.arm_and_start()
        print("Flying.")
        finished = link.monitor(len(rows), settings.sitl_mission_timeout_s)
        print("Done." if finished else "Time limit reached -- check Mission Planner.")
    except SimulatorError as exc:
        print(f"Simulation stopped: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nCtrl+C -- switching to RTL.")
        link.return_to_launch()
        time.sleep(1)
    finally:
        try:
            input("\nPress Enter to close the simulator ...")
        except (EOFError, KeyboardInterrupt):
            pass
        sim.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
