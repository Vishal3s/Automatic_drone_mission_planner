"""
Phase 15 -- submit natural-language missions; the mission map opens and
updates by itself.

    python scripts/plan_mission.py "Inspect AB1 of VIT Chennai at 30 meters"
    python scripts/plan_mission.py                 # prompts for missions one after another

Each request runs the EXISTING pipeline unchanged:
  intent parser -> LocationResolver (Google or OSM) -> RAG -> LLM planner ->
  Phase 5 validation -> location safety checks
and the result is published to the map (http://127.0.0.1:8765/ by default).
A stopped request (LOCATION_NOT_RESOLVED / AMBIGUOUS / low confidence)
is shown on the map panel as an error -- nothing is drawn at a guessed spot.

Options:
  --home LAT LON     launch point (enables the route-from-home line and RTL leg)
  --battery 0.85     current battery fraction (enables the battery/RTH check together with --home)
  --no-browser       don't open a browser tab (just print the URL)
  --port 8765        map server port

This script does not fly anything: it plans and visualizes. Upload/flight
stays with the existing PX4/MAVSDK code (app/mavlink/px4_client.py), which
should be given the SAME MissionStateManager via MissionViewStore.attach()
for live telemetry and RAMR updates on the map.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable from any cwd

from app.llm.llm_client import LLMGenerationError  # noqa: E402
from app.mission.location_aware_generator import generate_mission_from_natural_language  # noqa: E402
from app.ui.map_server import open_mission_map  # noqa: E402
from app.location.location_resolver import LocationResolver  # noqa: E402
from app.ui.mission_view import MissionViewStore  # noqa: E402
from app.ui.pick import ask_for_map_pick as _ask_for_map_pick  # noqa: E402


def _summary(result) -> dict:
    d = result.as_dict()
    loc = (d.get("location_result") or {}).get("location") or {}
    mission = d.get("mission") or {}
    return {
        "status": d["status"],
        "accepted": d["accepted"],
        "message": d["message"],
        "location": {k: loc.get(k) for k in ("building", "campus", "latitude", "longitude", "geometry_type", "source", "confidence", "place_id")} if loc else None,
        "waypoints": [
            (i["sequence"], i["command"], i.get("latitude"), i.get("longitude"), i.get("altitude"))
            for i in mission.get("mission_items", [])
        ],
        "outline": {"source": loc.get("geometry_source"), "how": loc.get("geometry_note")} if loc.get("geometry_source") else None,
        "resolution_warnings": (d.get("location_result") or {}).get("warnings"),
        # What the map provider returned for the building, and why each was accepted/rejected
        "candidates_seen": [
            f"{c['name']} -> {'ACCEPTED' if c['accepted'] else 'rejected: ' + c['reason']}"
            for c in ((d.get("location_result") or {}).get("details") or {}).get("provider_candidates", [])
        ][:12],
        "location_errors": (d.get("location_validation") or {}).get("errors"),
        "location_warnings": (d.get("location_validation") or {}).get("warnings"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="*", help="e.g. Inspect AB1 of VIT Chennai at 30 meters")
    parser.add_argument("--home", nargs=2, type=float, metavar=("LAT", "LON"))
    parser.add_argument("--battery", type=float, help="battery fraction 0-1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    home = tuple(args.home) if args.home else None

    store = MissionViewStore()
    resolver = LocationResolver()  # one resolver for the session, so a map pick is reused immediately
    url = None
    request = " ".join(args.request).strip()

    def plan(text: str):
        return generate_mission_from_natural_language(
            text, location_resolver=resolver, home_position=home, battery_percent=args.battery
        )

    while True:
        if not request:
            try:
                request = input("\nMission request (blank to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                request = ""
            if not request:
                break

        print(f"\nPlanning: {request}")
        try:
            result = plan(request)
        except LLMGenerationError as exc:
            print(f"LLM unavailable: {exc}")
            request = ""
            continue

        if not _ask_for_map_pick(store, resolver, result, request, plan, home):
            store.publish_plan(result, home=home)
        if url is None:
            url = open_mission_map(store, open_browser=not args.no_browser, port=args.port)
        print(json.dumps(_summary(result), indent=2, default=str))
        print(f"\nMap: {url}  (updates automatically)")
        request = ""

    if url:
        input("\nMap server still running -- press Enter to stop.")


if __name__ == "__main__":
    main()
