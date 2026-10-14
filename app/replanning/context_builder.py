"""
RAMR novelty Phase 5 -- Replanning Context Builder.

    OBJECTIVE: Create a complete context object for adaptive replanning.
    CREATE: ReplanningContextBuilder
    INPUT: Trigger event, current drone state, mission state, remaining
    waypoints, battery, safety constraints, original mission, active
    mission, relevant mission history.
    OUTPUT: ReplanningContext (current drone position, current waypoint,
    completed waypoints, remaining waypoints, trigger reason, battery
    status, safety constraints, mission priority, relevant historical
    information).
    IMPORTANT: Do not rebuild the entire original mission context
    unnecessarily. Build a focused replanning context based on:
    CURRENT STATE + REMAINING MISSION + TRIGGER EVENT.
    SUCCESS CRITERIA: When a trigger occurs, the system can create a
    complete and accurate snapshot of the current mission situation.

Design notes:

- Every INPUT your spec lists is satisfied, but NOT all of them end up
  copied onto the OUTPUT `ReplanningContext` -- that's the point of "do
  not rebuild the entire original mission context unnecessarily."
  Specifically: `original_mission` and `active_mission` (both full
  `Mission` objects, potentially many waypoints) are accepted only
  implicitly, via `mission_state` (Phase 1's `MissionState`, which
  already holds both) -- the builder reads `mission_state.mission_id`
  for identification and `mission_state.remaining_waypoints`/
  `.completed_waypoints`/`.current_waypoint` (the FOCUSED slice your
  OUTPUT list actually asks for), but never copies the full
  `original_mission`/`active_mission` objects onto `ReplanningContext`
  itself. This matches "CURRENT STATE + REMAINING MISSION + TRIGGER
  EVENT," not "CURRENT STATE + REMAINING MISSION + TRIGGER EVENT +
  everything else."
- "Reuse existing data structures" (standing project rule): `current_waypoint`/
  `completed_waypoints`/`remaining_waypoints` are read directly off
  `MissionState` (Phase 1) rather than recomputed; `battery_status` is
  built from `DroneState` (Phase 2); `trigger_reason`/`trigger_event`
  come from `ReplanningEvent` (Phase 3); `safety_constraints` defaults
  to the SAME config values `app/mission/validator.py` and
  `app/replanning/replanner.py` already use (`settings.min_altitude_m`,
  `.max_altitude_m`, `.max_waypoints`, `.min_battery_reserve_percent`) --
  no new, separate set of safety numbers was invented.
- `mission_priority` and `relevant_history`: nothing earlier in this
  project tracks a mission's priority, and no mission-history store
  exists yet (that's the audit's Step 7 "Mission Learning/History
  Storage," separate later scope, not built). Rather than fabricate
  either, `build()` accepts them as OPTIONAL caller-supplied inputs
  (`mission_priority` defaults to `"NORMAL"`, `mission_history` defaults
  to `None`/empty) -- documented honestly here and in the README rather
  than silently invented, so a future phase that adds real mission
  priority or a real history store can supply real values without this
  builder's shape needing to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import settings
from app.mavlink.drone_state import DroneState
from app.mission.schema import MissionItem
from app.mission.state import MissionState
from app.trigger.events import ReplanningEvent


@dataclass
class DronePosition:
    """Focused position snapshot -- just what a replanner needs, not the whole DroneState."""

    latitude_deg: float | None
    longitude_deg: float | None
    altitude_m: float | None  # relative altitude -- same convention as DroneState.relative_altitude_m

    def as_dict(self) -> dict:
        return {"latitude_deg": self.latitude_deg, "longitude_deg": self.longitude_deg, "altitude_m": self.altitude_m}


@dataclass
class BatteryStatus:
    """
    Battery status distilled for a replanning decision: not just the raw
    percentage, but whether it's `sufficient` against the reserve floor
    (the same `min_battery_reserve_percent` the Phase 4 replanner uses),
    so a downstream consumer doesn't have to re-derive that comparison
    itself.
    """

    remaining_percent: float | None
    voltage_v: float | None
    minimum_reserve_percent: float
    sufficient: bool | None  # None when remaining_percent is unknown -- never guessed

    def as_dict(self) -> dict:
        return {
            "remaining_percent": self.remaining_percent,
            "voltage_v": self.voltage_v,
            "minimum_reserve_percent": self.minimum_reserve_percent,
            "sufficient": self.sufficient,
        }


@dataclass
class CircularGeofence:
    """
    RAMR novelty Phase 7 addition -- the concrete shape `SafetyConstraints
    .geofence` (and `.no_fly_zones`, below) can now hold. A simple circle
    (center + radius), NOT a real polygon boundary -- Phase 5 left
    `geofence` as `None` because no geofence data existed anywhere in
    this project yet; Phase 7's safety validator is the first thing that
    actually needs to check against one, so this is the simplest
    honestly-implementable shape (reuses the same haversine great-circle
    distance formula `app/replanning/replanner.py` already uses for
    battery-feasibility distance, rather than a real GIS polygon engine
    this project has no dependency for). Still `None`/empty by default --
    nothing fabricates a real boundary; a caller who has one (or a real
    building-boundary/no-fly database in a later phase) supplies it via
    `SafetyConstraints(geofence=..., no_fly_zones=[...])`.
    """

    center_latitude_deg: float
    center_longitude_deg: float
    radius_km: float

    def as_dict(self) -> dict:
        return {
            "center_latitude_deg": self.center_latitude_deg,
            "center_longitude_deg": self.center_longitude_deg,
            "radius_km": self.radius_km,
        }


@dataclass
class SafetyConstraints:
    """
    The safety envelope a replanned route must respect. Reuses the SAME
    config values the existing validator (`app/mission/validator.py`)
    and RAMR (`app/replanning/replanner.py`) already enforce -- not a
    separate, parallel set of numbers.

    `geofence` -- an allowed-flight-area circle a route must stay INSIDE
    -- and `no_fly_zones` -- forbidden circles (e.g. standing in for
    "building boundaries") a route must stay OUTSIDE -- both default to
    "no constraint configured" (`None` / `[]`) rather than a fabricated
    boundary; no geofence polygon or building-boundary database exists
    anywhere in this project (a gap the Phase 0 audit already flagged).
    RAMR novelty Phase 7's `app.replanning.safety_validator` is what
    actually checks a proposed route against these when they ARE
    supplied.
    """

    min_altitude_m: float
    max_altitude_m: float
    min_battery_reserve_percent: float
    max_waypoints: int
    geofence: CircularGeofence | None = None
    no_fly_zones: list[CircularGeofence] = field(default_factory=list)

    @classmethod
    def from_settings(cls) -> "SafetyConstraints":
        return cls(
            min_altitude_m=settings.min_altitude_m,
            max_altitude_m=settings.max_altitude_m,
            min_battery_reserve_percent=settings.min_battery_reserve_percent,
            max_waypoints=settings.max_waypoints,
            geofence=None,
            no_fly_zones=[],
        )

    def as_dict(self) -> dict:
        return {
            "min_altitude_m": self.min_altitude_m,
            "max_altitude_m": self.max_altitude_m,
            "min_battery_reserve_percent": self.min_battery_reserve_percent,
            "max_waypoints": self.max_waypoints,
            "geofence": self.geofence.as_dict() if self.geofence else None,
            "no_fly_zones": [z.as_dict() for z in self.no_fly_zones],
        }


@dataclass
class ReplanningContext:
    """
    The complete, FOCUSED snapshot `ReplanningContextBuilder.build()`
    produces -- exactly the fields your OUTPUT example lists, plus
    `mission_id` (for identification/logging) and `trigger_event` (the
    full event, kept alongside the derived `trigger_reason` string for
    traceability -- a downstream Adaptive Mission Replanner may want the
    raw event, e.g. its `metadata`, not just a human-readable summary).
    """

    mission_id: str
    current_drone_position: DronePosition
    current_waypoint: MissionItem | None
    completed_waypoints: list[MissionItem]
    remaining_waypoints: list[MissionItem]
    trigger_reason: str
    trigger_event: ReplanningEvent
    battery_status: BatteryStatus
    safety_constraints: SafetyConstraints
    mission_priority: str
    relevant_history: list[dict] = field(default_factory=list)
    built_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        """Flat, JSON-serializable summary -- for logging and for a future Replanner/LLM prompt builder."""
        return {
            "mission_id": self.mission_id,
            "current_drone_position": self.current_drone_position.as_dict(),
            "current_waypoint": self.current_waypoint.model_dump() if self.current_waypoint else None,
            "completed_waypoints": [i.model_dump() for i in self.completed_waypoints],
            "remaining_waypoints": [i.model_dump() for i in self.remaining_waypoints],
            "trigger_reason": self.trigger_reason,
            "trigger_event_id": self.trigger_event.event_id,
            "battery_status": self.battery_status.as_dict(),
            "safety_constraints": self.safety_constraints.as_dict(),
            "mission_priority": self.mission_priority,
            "relevant_history": self.relevant_history,
            "built_at": self.built_at.isoformat(),
        }


class ReplanningContextBuilder:
    """
    Usage:
        builder = ReplanningContextBuilder()
        context = builder.build(event, mission_state, drone_state)
        # or with overrides:
        context = builder.build(
            event, mission_state, drone_state,
            safety_constraints=my_constraints,
            mission_priority="HIGH",
            mission_history=past_missions,
        )
    """

    def build(
        self,
        event: ReplanningEvent,
        mission_state: MissionState,
        drone_state: DroneState,
        *,
        safety_constraints: SafetyConstraints | None = None,
        mission_priority: str = "NORMAL",
        mission_history: list[dict] | None = None,
    ) -> ReplanningContext:
        resolved_constraints = safety_constraints or SafetyConstraints.from_settings()

        battery_status = self._build_battery_status(drone_state, resolved_constraints)
        trigger_reason = self._build_trigger_reason(event)
        relevant_history = self._filter_relevant_history(mission_history, mission_state)

        return ReplanningContext(
            mission_id=mission_state.mission_id,
            current_drone_position=DronePosition(
                latitude_deg=drone_state.latitude_deg,
                longitude_deg=drone_state.longitude_deg,
                altitude_m=drone_state.relative_altitude_m,
            ),
            current_waypoint=mission_state.current_waypoint,
            completed_waypoints=list(mission_state.completed_waypoints),
            remaining_waypoints=list(mission_state.remaining_waypoints),
            trigger_reason=trigger_reason,
            trigger_event=event,
            battery_status=battery_status,
            safety_constraints=resolved_constraints,
            mission_priority=mission_priority,
            relevant_history=relevant_history,
        )

    @staticmethod
    def _build_battery_status(drone_state: DroneState, constraints: SafetyConstraints) -> BatteryStatus:
        remaining = drone_state.battery_remaining_percent
        sufficient = None if remaining is None else remaining > constraints.min_battery_reserve_percent
        return BatteryStatus(
            remaining_percent=remaining,
            voltage_v=drone_state.battery_voltage_v,
            minimum_reserve_percent=constraints.min_battery_reserve_percent,
            sufficient=sufficient,
        )

    @staticmethod
    def _build_trigger_reason(event: ReplanningEvent) -> str:
        reason = f"{event.event_type.value} (severity={event.severity.value}, source={event.source.value})"
        if event.metadata:
            details = ", ".join(f"{k}={v!r}" for k, v in sorted(event.metadata.items()))
            reason += f" -- {details}"
        return reason

    @staticmethod
    def _filter_relevant_history(
        mission_history: list[dict] | None, mission_state: MissionState
    ) -> list[dict]:
        """
        No mission-history store exists yet anywhere in this project
        (see this module's docstring), so there is nothing to actually
        query here today -- this only shapes whatever the CALLER already
        supplies (e.g. a future history-store query result) into the
        "relevant to THIS mission" subset, rather than passing through
        unrelated history unfiltered. Entries without a `mission_id` key
        are treated as globally relevant (e.g. a general lessons-learned
        note) and kept as-is.
        """
        if not mission_history:
            return []
        return [
            entry for entry in mission_history
            if "mission_id" not in entry or entry["mission_id"] == mission_state.mission_id
        ]
