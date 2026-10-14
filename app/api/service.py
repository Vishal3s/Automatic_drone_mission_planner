"""
UI phase -- everything the web UI can do, as plain Python (no web
framework), so it is fully testable on its own. `app/api/main.py` only
maps HTTP routes onto these methods.

    plan(request)        -> the EXISTING pipeline: intent -> location -> RAG
                            -> LLM planner -> Phase 5 rules -> location safety
    pick(lat, lon)       -> the user's one click on an unnamed building
    connect_drone()      -> one long-lived MAVSDK link (PX4 SITL by default)
    send_to_drone(id)    -> fly an ACCEPTED plan, only with confirm=True
    return_to_launch(), hold()

Safety rules enforced here, not in the browser:
  - only the current, accepted plan can be sent; never a rejected or old one;
  - an explicit confirm flag is required;
  - one flight at a time;
  - the mission must be near PX4's own home (PX4 refuses a first waypoint
    farther than MIS_DIST_1WP, 900 m by default -- with SITL that usually
    means its home was not set to the mission area, see the message).
The LLM never talks to the drone: it only produces a Mission that the
existing validators accepted, and DroneLink flies exactly that Mission.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone

from app.config import settings
from app.location.geofence import haversine_km
from app.mavlink.drone_link import DroneLink
from app.mission.state import MissionStateManager
from app.ui.mission_view import MissionViewStore
from app.ui.pick import ask_for_map_pick

log = logging.getLogger(__name__)

FLIGHT_IDLE = "idle"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wants_file(request: str) -> bool:
    from app.mission.intent_parser import wants_waypoints_file

    return wants_waypoints_file(request)


def mission_file_name(result) -> str:
    """'<timestamp>_<building or mission>.waypoints' -- the same name whether saved or downloaded."""
    import re
    import time

    mission = result.mission_result.mission
    building = ((result.inspection_summary() or {}).get("building") or {}).get("name")
    label = building or mission.mission_name or "mission"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")[:40] or "mission"
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{slug}.waypoints"


def mission_file_text(result, home: tuple[float, float] | None) -> str:
    """The accepted mission as Mission Planner .waypoints text (ArduCopter)."""
    from app.config import settings
    from app.sim.ardupilot_sitl import ardupilot_rows, default_home, waypoints_text

    mission = result.mission_result.mission
    lat, lon = home if home else default_home(mission)
    return waypoints_text(ardupilot_rows(mission, (lat, lon, settings.sitl_home_alt_m)))


def save_mission_file(result, home: tuple[float, float] | None) -> str:
    """Write the accepted mission as a .waypoints file in the project's missions/ folder. Returns its path."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]  # the project folder, whatever the working directory
    target = root / "missions" / mission_file_name(result)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(mission_file_text(result, home), encoding="utf-8")
    return str(target)

class MissionControlService:
    def __init__(self, *, resolver=None, planner=None, link_factory=None) -> None:
        self.store = MissionViewStore()
        self._resolver = resolver
        self._planner = planner
        self._link_factory = link_factory or (lambda address=None: DroneLink(address=address))
        self.link: DroneLink | None = None
        self._lock = threading.Lock()
        self._results: dict[int, object] = {}
        self._history: list[dict] = []
        self._next_id = 0
        self._current_id: int | None = None
        self._home: tuple[float, float] | None = None
        self.flight = {"state": FLIGHT_IDLE, "stage": None, "plan_id": None, "message": "", "progress": None,
                       "started_at": None, "finished_at": None}
        self._flight_task: asyncio.Task | None = None
        self._mission_state: MissionStateManager | None = None
        self.planning = {"active": False, "request": None, "started_at": None, "error": None}

    # -----------------------------------------------------------------
    # Planning
    # -----------------------------------------------------------------

    def _get_resolver(self):
        if self._resolver is None:
            from app.location.location_resolver import LocationResolver

            self._resolver = LocationResolver()
        return self._resolver

    def _plan_fn(self, home, battery):
        planner = self._planner
        if planner is None:
            from app.mission.location_aware_generator import generate_mission_from_natural_language as planner

        def run(text: str):
            return planner(text, location_resolver=self._get_resolver(), home_position=home, battery_percent=battery)

        return run

    def plan(self, request: str, *, home: tuple[float, float] | None = None, battery: float | None = None) -> dict:
        """Run the existing pipeline for one request. Blocking (LLM + map calls) -- call from a worker thread."""
        request = (request or "").strip()
        if not request:
            raise ValueError("Mission request is empty.")
        if self.flight["state"] in ("starting", "flying"):
            raise RuntimeError("A mission is in flight -- wait for it to finish (or Return to Launch) before planning a new one.")
        with self._lock:
            if self.planning["active"]:
                raise RuntimeError("Already planning a mission -- wait for it to finish.")
            self.planning = {"active": True, "request": request, "started_at": _now(), "error": None}
        try:
            run = self._plan_fn(home, battery)
            result = run(request)
        except Exception as exc:
            self.planning = {"active": False, "request": request, "started_at": None, "error": str(exc)}
            raise
        self.planning = {"active": False, "request": request, "started_at": None, "error": None}
        self._home = home
        plan_id = self._record(request, result)
        waypoints_file = None
        if result.accepted and _wants_file(request):
            try:
                waypoints_file = save_mission_file(result, home)
            except Exception as exc:  # the plan itself is still valid; say why no file was written
                log.warning("Waypoints file not written: %s", exc)

        def on_planned(new_result):  # after a building pick, the re-planned result replaces this one
            self._record(request, new_result, replace_id=plan_id)

        if not ask_for_map_pick(self.store, self._get_resolver(), result, request, run, home, on_planned=on_planned):
            self.store.publish_plan(result, home=home)
        summary = self.plan_summary(plan_id)
        summary["waypoints_file"] = waypoints_file
        if _wants_file(request) and waypoints_file is None:
            summary["waypoints_file_error"] = (
                "No file was written because the plan was not accepted." if not result.accepted
                else "The waypoints file could not be written -- see the server log."
            )
        return summary

    def pick(self, latitude: float, longitude: float) -> dict:
        """The user's click on the building (pick mode). Blocking -- plans the mission after the click."""
        return self.store.handle_pick(latitude, longitude)

    def _record(self, request: str, result, *, replace_id: int | None = None) -> int:
        with self._lock:
            if replace_id is None:
                self._next_id += 1
                plan_id = self._next_id
            else:
                plan_id = replace_id
            self._results[plan_id] = result
            self._current_id = plan_id
            entry = {"id": plan_id, "request": request, "created_at": _now(), "status": result.status.value,
                     "accepted": bool(result.accepted), "message": result.message}
            self._history = [h for h in self._history if h["id"] != plan_id] + [entry]
        return plan_id

    def plan_summary(self, plan_id: int) -> dict:
        result = self._results.get(plan_id)
        if result is None:
            raise KeyError(plan_id)
        view = self.store.snapshot()
        return {
            "plan_id": plan_id,
            "current": plan_id == self._current_id,
            "status": result.status.value,
            "accepted": bool(result.accepted),
            "message": result.message,
            "pick_required": bool(view.get("pick")) and not view.get("has_plan"),
            "result": result.as_dict(),
            "waypoints_download": f"/api/plans/{plan_id}/waypoints" if result.accepted else None,
            # Building point + DETECTED outline polygon + final waypoints (with camera yaw/gimbal), as uploaded to PX4.
            "inspection": result.inspection_summary() if hasattr(result, "inspection_summary") else None,
        }

    def waypoints_download(self, plan_id: int) -> tuple[str, str]:
        """(file name, .waypoints text) for an accepted plan. KeyError if no such plan; PermissionError if not accepted."""
        result = self._results.get(plan_id)
        if result is None:
            raise KeyError(plan_id)
        if not result.accepted:
            raise PermissionError("Only an accepted plan can be downloaded as a Mission Planner file.")
        return mission_file_name(result), mission_file_text(result, self._home)

    def qgc_plan_json(self, plan_id: int) -> str:
        """
        The accepted plan as a QGroundControl .plan file (app/mission/qgc_plan.py).
        Only writes text -- nothing is sent to a drone; QGC uploads it.
        """
        from app.mission.qgc_plan import qgc_plan_json

        result = self._results.get(plan_id)
        if result is None or result.mission_result is None:
            raise KeyError(plan_id)
        if not result.accepted:
            raise PermissionError("Only an accepted plan can be exported to QGroundControl.")
        return qgc_plan_json(result.mission_result.mission, name=f"plan {plan_id}")

    def plans(self) -> list[dict]:
        with self._lock:
            return list(reversed(self._history))

    def view(self) -> dict:
        v = self.store.snapshot()
        v["current_plan_id"] = self._current_id
        return v

    # -----------------------------------------------------------------
    # Drone
    # -----------------------------------------------------------------

    async def live_context(self) -> tuple[tuple[float, float] | None, float | None]:
        """
        (home, battery 0-1) from the connected drone, for planning: the
        route is drawn from the drone's real home and the battery/RTH check
        uses its real charge. (None, None) when no drone is connected.
        MAVSDK 3 reports battery as 0-100; the planner uses 0-1.
        """
        if self.link is None or not self.link.connected:
            return None, None
        home = await self.link.home()
        raw = self.link.state.snapshot().battery_remaining_percent
        battery = None if raw is None else (raw / 100.0 if raw > 1.0 else raw)
        return home, battery

    def state(self) -> dict:
        """Everything the web page polls, in one response."""
        return {
            "view": self.view(),
            "drone": self.drone_status(),
            "plans": self.plans(),
            "planning": dict(self.planning),
            "current_plan": self.plan_summary(self._current_id) if self._current_id in self._results else None,
        }

    async def connect_drone(self, address: str | None = None) -> dict:
        address = (address or "").strip() or None
        if self.link is not None and address and address != self.link.address:
            if self.flight["state"] in ("starting", "flying"):
                raise RuntimeError("Cannot change the drone address during a flight.")
            await self.link.close()
            self.link = None
        if self.link is None:
            self.link = self._link_factory(address) if address else self._link_factory()
        ok = await self.link.connect()
        if ok:
            self.store.attach(self._mission_state, self.link.state.snapshot)
        return self.drone_status()

    def drone_status(self) -> dict:
        link = self.link
        state = link.state.snapshot().as_dict() if (link and link.connected) else None
        return {
            "address": link.address if link else settings.px4_connection_url,
            "connected": bool(link and link.connected),
            "error": link.last_error if link else None,
            "telemetry": state,
            "flight": dict(self.flight),
        }

    async def send_to_drone(self, plan_id: int, *, confirm: bool) -> dict:
        if not confirm:
            raise PermissionError("Sending a mission requires explicit confirmation (confirm=true).")
        if self.flight["state"] in ("starting", "flying"):
            raise RuntimeError("A mission is already in flight.")
        result = self._results.get(plan_id)
        if result is None:
            raise KeyError(plan_id)
        if plan_id != self._current_id:
            raise RuntimeError("Only the most recent plan can be sent -- re-plan if you want to fly an older request.")
        if not result.accepted or result.mission_result is None:
            raise RuntimeError(f"This plan was not accepted ({result.status.value}) -- it cannot be sent to a drone.")
        if self.link is None or not self.link.connected:
            status = await self.connect_drone()
            if not status["connected"]:
                raise ConnectionError(f"Could not connect to the drone at {status['address']}: {status['error']}")

        mission = result.mission_result.mission
        manager = MissionStateManager()
        manager.start(mission)
        manager.mark_validated()
        self._mission_state = manager
        self.store.attach(manager, self.link.state.snapshot)
        self.flight = {"state": "starting", "stage": "preflight", "plan_id": plan_id, "message": "Connecting to PX4…",
                       "progress": None, "started_at": _now(), "finished_at": None}
        self._flight_task = asyncio.create_task(self._fly(mission, manager))
        self._flight_task.add_done_callback(self._flight_task_done)
        return self.drone_status()

    def _flight_task_done(self, task: asyncio.Task) -> None:
        """Safety net: a crash inside the flight task must show as 'Stopped', never leave the UI stuck on 'starting'."""
        if task.cancelled():
            exc = "flight task cancelled"
        else:
            exc = task.exception()
        if exc is not None and self.flight["state"] in ("starting", "flying"):
            log.error("Flight task ended unexpectedly: %s", exc)
            self.flight.update(state="failed", message=f"Stopped: unexpected error ({exc}). Check the drone; use Return to Launch if it is airborne.",
                               finished_at=_now())

    def _first_waypoint_check(self, mission):
        """Refuse before upload when the mission is far from PX4's home (PX4's own MIS_DIST_1WP is 900 m by default)."""
        coords = [(i.latitude, i.longitude) for i in mission.mission_items if i.latitude is not None and i.longitude is not None]

        def check(home_lat: float, home_lon: float):
            if not coords:
                return None
            d_km = haversine_km(home_lat, home_lon, *coords[0])
            if d_km * 1000.0 > settings.max_first_waypoint_distance_m:
                return (
                    f"The first waypoint is {d_km:.1f} km from the drone's home ({home_lat:.5f}, {home_lon:.5f}); "
                    f"PX4 refuses missions that start more than {settings.max_first_waypoint_distance_m:.0f} m away. "
                    f"For PX4 SITL, start it with its home at the mission area, e.g. "
                    f"PX4_HOME_LAT={coords[0][0]:.6f} PX4_HOME_LON={coords[0][1]:.6f} make px4_sitl gz_x500"
                )
            return None

        return check

    async def _fly(self, mission, manager: MissionStateManager) -> None:
        def on_stage(stage: str) -> None:
            self.flight.update(stage=stage, message={
                "preflight": "Waiting for PX4 to be ready (GPS/EKF)…", "upload": "Uploading mission…",
                "arm": "Arming…", "start": "Starting mission…", "monitor": "Flying the mission",
            }.get(stage, stage))
            if stage == "monitor":
                manager.mark_uploaded()
                manager.mark_running()
                self.flight["state"] = "flying"

        def on_progress(current: int, total: int) -> None:
            self.flight["progress"] = [current, total]
            try:
                manager.record_progress(current=current, total=total)
            except Exception as exc:  # noqa: BLE001 -- progress bookkeeping must not abort the flight
                log.warning("record_progress failed: %s", exc)

        try:
            outcome = await self.link.fly(mission, on_progress=on_progress, on_stage=on_stage,
                                          preupload_check=self._first_waypoint_check(mission))
        except Exception as exc:  # noqa: BLE001
            outcome = None
            error = f"Unexpected error: {exc}"
        else:
            error = outcome.error
        now = _now()
        if outcome is not None and outcome.accepted:
            manager.complete()
            self.flight.update(state="completed", stage="complete", message="Mission completed.", finished_at=now)
        else:
            if not manager.is_terminal:
                manager.fail(error or "failed")
            stage = outcome.stage if outcome is not None else "unknown"
            self.flight.update(state="failed", stage=stage, message=f"Stopped at '{stage}': {error}", finished_at=now)

    async def return_to_launch(self) -> dict:
        if self.link is None or not self.link.connected:
            raise ConnectionError("Drone is not connected.")
        await self.link.return_to_launch()
        self.flight["message"] = "Return to Launch commanded."
        return self.drone_status()

    async def hold(self) -> dict:
        if self.link is None or not self.link.connected:
            raise ConnectionError("Drone is not connected.")
        await self.link.hold()
        self.flight["message"] = "Hold commanded."
        return self.drone_status()

    async def disconnect_drone(self) -> dict:
        if self.flight["state"] in ("starting", "flying"):
            raise RuntimeError("A mission is in flight -- Return to Launch first.")
        if self.link is not None:
            await self.link.close()
        self.store.attach(self._mission_state, None)
        return self.drone_status()

    async def shutdown(self) -> None:
        if self.link is not None:
            await self.link.close()
