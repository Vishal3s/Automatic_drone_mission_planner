"""
Phase 15 -- the mission MAP VIEW MODEL: one JSON document describing what
the map must show, derived ONLY from the real objects the existing
pipeline already produces. Nothing here plans, edits or invents anything.

Sources (all existing, none modified):
- NaturalLanguageMissionResult  (app.mission.location_aware_generator) -> target
  location, footprint, inspection ring, the generated Mission, status/message.
- MissionStateManager           (app.mission.state, Phase 1)  -> the ACTIVE mission,
  which RAMR replaces on every applied replan (apply_replan), plus
  completed / current / remaining waypoints and the replanning status.
- a DroneState provider          (app.mavlink.drone_state, Phase 2) -> live position.

Because the map is drawn from MissionStateManager's active mission, a RAMR
update (CV quality failure -> corrective waypoint -> apply_replan) shows up
on the next poll with no change to RAMR/CV/MissionUpdateManager and no page
refresh -- spec step 13. The drawn waypoints and route are exactly the
mission's own items (spec step 12): nothing is smoothed, added or reordered.

Colours (spec step 10; there is no existing dashboard design system to follow):
  RED     target building footprint / inspection region
  BLUE    planned flight path + planned waypoints
  GREEN   completed waypoints + flown track
  YELLOW  region + new waypoints while RAMR is replanning / just replanned
  drone   current drone position
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable

from app.location.geofence import haversine_km

_TRACK_MIN_STEP_M = 2.0
_TRACK_MAX_POINTS = 2000

COLORS = {
    "target": "#D93025",       # red
    "route": "#1A73E8",        # blue
    "completed": "#188038",    # green
    "replanning": "#F9AB00",   # yellow
    "drone": "#202124",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item_key(item) -> tuple:
    return (item.command.value, round(item.latitude, 7), round(item.longitude, 7))


def _waypoint(item, state: str, corrective: bool) -> dict:
    return {
        "sequence": item.sequence,
        "command": item.command.value,
        "latitude": item.latitude,
        "longitude": item.longitude,
        "altitude_m": item.altitude,
        "state": state,            # planned | current | completed
        "corrective": corrective,  # True = added by RAMR, not in the original plan
    }


class MissionViewStore:
    """
    Thread-safe holder the map server reads from.

        store = MissionViewStore()
        store.publish_plan(result, home=(lat, lon))               # after generate_mission_from_natural_language()
        store.attach(mission_state_manager, drone_state_provider)  # optional: live mission / telemetry
        store.snapshot()                                           # -> dict served as /api/mission-view
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plan: dict | None = None
        self._plan_id = 0
        self._mission_state_manager = None
        self._drone_state_provider: Callable | None = None
        self._home: tuple[float, float] | None = None
        self._completed_seen: dict[tuple, dict] = {}
        self._drone_track: list[list[float]] = []
        self._pick: dict | None = None            # Phase 15.4 -- "click the building" request shown on the map
        self._pick_handler: Callable | None = None
        self._pick_id = 0

    # -----------------------------------------------------------------
    # Inputs
    # -----------------------------------------------------------------

    def publish_plan(self, result, *, home: tuple[float, float] | None = None) -> None:
        """Record a NaturalLanguageMissionResult (accepted OR stopped -- a stopped plan is shown as an error state)."""
        location = result.location_result.location if (result.location_result and result.location_result.location) else None
        if result.status.value == "LOCATION_LOW_CONFIDENCE":
            location = None  # not sufficiently verified -> not drawn as a target (spec step 16: no unverified coordinates)
        mission = result.mission_result.mission if result.mission_result else None
        ring = result.inspection_ring
        validation = result.location_validation
        rule_validation = result.mission_result.validation if result.mission_result else None

        plan = {
            "status": result.status.value,
            "accepted": result.accepted,
            "message": result.message,
            "request": getattr(result.intent, "raw_location_phrase", None),
            "target": _target(location),
            "inspection_region": _region(ring),
            "mission": mission,
            "mission_name": mission.mission_name if mission else None,
            "errors": (list(rule_validation.errors) if rule_validation and getattr(rule_validation, "errors", None) else [])
                      + (list(validation.errors) if validation else []),
            "warnings": list(validation.warnings) if validation else [],
            "building_facts": result.building_facts.as_dict() if result.building_facts else None,
        }
        with self._lock:
            self._plan = plan
            self._pick = None
            self._plan_id += 1
            self._home = home
            self._completed_seen = {}
            self._drone_track = []

    def publish_pick_request(
        self, *, building: str, campus: str, campus_bbox, campus_center, message: str,
        footprints: list[list[list[float]]] | None = None, footprints_note: str | None = None,
    ) -> None:
        """
        Phase 15.4: the building could not be found automatically -- ask the
        user to click it once. `footprints` are REAL OpenStreetMap outlines
        inside the campus (lat/lon rings), drawn only as clickable hints.
        """
        with self._lock:
            self._plan = None
            self._pick_id += 1
            self._pick = {
                "pick_id": self._pick_id,
                "building": building,
                "campus": campus,
                "campus_bbox": list(campus_bbox) if campus_bbox else None,
                "campus_center": list(campus_center) if campus_center else None,
                "message": message,
                "footprints": footprints or [],
                "footprints_note": footprints_note,
                "state": "waiting",           # waiting -> locating -> (plan published) | error
                "error": None,
            }

    def set_pick_handler(self, handler: Callable | None) -> None:
        """`handler(latitude, longitude) -> (ok: bool, message: str)`; runs when the map reports a click."""
        with self._lock:
            self._pick_handler = handler

    def handle_pick(self, latitude: float, longitude: float) -> dict:
        with self._lock:
            handler, pick = self._pick_handler, self._pick
            if handler is None or pick is None:
                return {"ok": False, "message": "No building is waiting to be picked."}
            pick["state"], pick["error"] = "locating", None
        try:
            ok, message = handler(latitude, longitude)
        except Exception as exc:  # noqa: BLE001 -- report on the map instead of failing silently
            ok, message = False, f"Could not use that point: {exc}"
        with self._lock:
            if not ok and self._pick is not None:
                self._pick["state"], self._pick["error"] = "waiting", message
        return {"ok": ok, "message": message}

    def attach(self, mission_state_manager=None, drone_state_provider: Callable | None = None) -> None:
        """Read the live mission (incl. RAMR replans) and telemetry from the EXISTING managers on every snapshot."""
        with self._lock:
            self._mission_state_manager = mission_state_manager
            self._drone_state_provider = drone_state_provider

    def set_home(self, home: tuple[float, float] | None) -> None:
        with self._lock:
            self._home = home

    # -----------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            plan = self._plan
            if plan is None:
                # No plan yet: still show the connected drone (web UI, before planning).
                return {"has_plan": False, "pick": dict(self._pick) if self._pick else None,
                        "drone": self._update_drone(), "colors": COLORS, "updated_at": _now()}

            mission = plan["mission"]
            original = mission
            mission_version = 1
            mission_status = None
            replanning_status = "NONE"
            adaptation_count = 0
            completed_seq: set[int] = set()
            current_seq = None

            live = self._live_state()
            if live is not None:
                mission = live.active_mission
                original = live.original_mission
                mission_version = live.mission_version
                mission_status = live.mission_status.value
                replanning_status = live.replanning_status.value
                adaptation_count = live.adaptation_count
                completed_seq = {i.sequence for i in live.completed_waypoints}
                current_seq = live.current_waypoint.sequence if live.current_waypoint else None

            original_keys = (
                {_item_key(i) for i in original.mission_items if i.latitude is not None and i.longitude is not None}
                if original is not None else set()
            )

            waypoints: list[dict] = []
            if mission is not None:
                for item in sorted(mission.mission_items, key=lambda i: i.sequence):
                    if item.latitude is None or item.longitude is None:
                        continue
                    state = "completed" if item.sequence in completed_seq else ("current" if item.sequence == current_seq else "planned")
                    wp = _waypoint(item, state, corrective=_item_key(item) not in original_keys)
                    waypoints.append(wp)
                    if state == "completed":
                        self._completed_seen[_item_key(item)] = wp

            # Completed waypoints survive a replan (apply_replan resets the
            # live completed list), so the green "done" area stays on the map.
            current_keys = {(w["command"], round(w["latitude"], 7), round(w["longitude"], 7)) for w in waypoints}
            history = [w for k, w in self._completed_seen.items() if k not in current_keys]

            route = [[w["latitude"], w["longitude"]] for w in waypoints if w["state"] != "completed"]
            ends_rtl = mission is not None and any(i.command.value == "RETURN_TO_LAUNCH" for i in mission.mission_items)
            if self._home is not None:
                if live is None or not completed_seq:
                    route = [list(self._home)] + route
                if ends_rtl and route:
                    route = route + [list(self._home)]

            drone = self._update_drone()
            if drone is not None and live is not None and route and completed_seq:
                route = [[drone["latitude"], drone["longitude"]]] + route  # the rest of the route starts where the drone is

            return {
                "has_plan": True,
                "plan_id": self._plan_id,
                "fit_key": f"{self._plan_id}:{mission_version}",  # frontend re-fits bounds only when this changes
                "status": plan["status"],
                "accepted": plan["accepted"],
                "message": plan["message"],
                "errors": plan["errors"],
                "warnings": plan["warnings"],
                "mission_name": plan["mission_name"],
                "target": plan["target"],
                "inspection_region": plan["inspection_region"],
                "building_facts": plan["building_facts"],
                "home": list(self._home) if self._home else None,
                "waypoints": waypoints,
                "completed_history": history,
                "route": route,
                "drone": drone,
                "drone_track": list(self._drone_track),
                "mission_version": mission_version,
                "mission_status": mission_status,
                "replanning_status": replanning_status,
                "replanning_active": replanning_status in ("TRIGGERED", "IN_PROGRESS") or mission_status in ("REPLANNING", "UPDATING"),
                "adaptation_count": adaptation_count,
                "colors": COLORS,
                "updated_at": _now(),
            }

    # -----------------------------------------------------------------
    # Helpers (caller holds the lock)
    # -----------------------------------------------------------------

    def _live_state(self):
        manager = self._mission_state_manager
        if manager is None:
            return None
        try:
            return manager.snapshot()
        except Exception:  # noqa: BLE001 -- not started yet (MissionStateError) -> show the plan only
            return None

    def _update_drone(self) -> dict | None:
        if self._drone_state_provider is None:
            return None
        try:
            ds = self._drone_state_provider()
        except Exception:  # noqa: BLE001 -- telemetry hiccup must not break the map
            return None
        if ds is None or ds.latitude_deg is None or ds.longitude_deg is None:
            return None
        point = [ds.latitude_deg, ds.longitude_deg]
        if not self._drone_track or haversine_km(*self._drone_track[-1], *point) * 1000.0 >= _TRACK_MIN_STEP_M:
            self._drone_track.append(point)
            del self._drone_track[:-_TRACK_MAX_POINTS]
        return {
            "latitude": ds.latitude_deg,
            "longitude": ds.longitude_deg,
            "altitude_m": ds.relative_altitude_m,
            "heading_deg": ds.heading_deg,
            "battery_percent": ds.battery_remaining_percent,
            "flight_mode": ds.flight_mode,
        }


def _target(location) -> dict | None:
    if location is None:
        return None
    return {
        "name": location.building or location.campus or location.institution,
        "institution": location.institution,
        "campus": location.campus,
        "building": location.building,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "geometry_type": location.geometry_type.value,
        "geometry_available": location.geometry_available,
        # Real footprint rings only -- empty for a point (never a fabricated polygon).
        "footprint": [[list(p) for p in ring] for ring in location.footprint_rings],
        "place_id": location.place_id,
        "formatted_address": location.formatted_address,
        "source": location.source.value,
        "geometry_source": location.geometry_source,
        "geometry_note": location.geometry_note,
        "geometry_meta": location.geometry_meta,
        "confidence": location.confidence.value,
        "bounding_box": list(location.bounding_box) if location.bounding_box else None,
    }


def _region(ring) -> dict | None:
    """The inspection region = the same ring the planner was given (InspectionRing), not a separate drawing."""
    if ring is None:
        return None
    return {
        "type": getattr(ring, "shape", "circle"),
        "center": [ring.center_latitude, ring.center_longitude],
        "radius_m": ring.radius_m,
        "footprint_radius_m": ring.footprint_radius_m,
        "footprint_measured": ring.footprint_measured,
        "standoff_m": ring.standoff_m,
        # Boundary fix: the ring line itself (follows the outline) + the neighbours it avoids.
        "path": [[round(a, 7), round(b, 7)] for a, b in getattr(ring, "path", [])],
        "neighbours_checked": getattr(ring, "neighbours_checked", False),
        "neighbours": [n.as_dict() for n in getattr(ring, "neighbours", [])],
        "blocked_points": list(getattr(ring, "blocked", [])),
        "notes": list(getattr(ring, "notes", [])),
    }