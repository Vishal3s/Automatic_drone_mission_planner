"""
RAMR novelty Phase 1 -- Mission State Management.

This is a REWRITE of the first version of this file. That version was
built against my own inferred design (Step 7 of docs/RAMR_CV_Audit.md)
rather than your actual Phase 1 spec, and was missing several required
fields/states and the getter API you specified. This version replaces it
completely, matching your Phase 1 spec field-for-field:

    OBJECTIVE: Create a centralized Mission State Manager. The adaptive
    replanner must know the current state of the mission.

    TRACK: mission_id, original mission, active mission, completed
    waypoints, current waypoint, remaining waypoints, mission status,
    replanning status, adaptation count, mission version.

    MISSION STATES: CREATED, VALIDATED, UPLOADED, RUNNING, REPLANNING,
    UPDATING, COMPLETED, FAILED, ABORTED, RETURNING_HOME.

    REQUIREMENTS:
      1. Reuse existing mission data structures.
      2. Do not duplicate the existing mission database unnecessarily.
      3. Provide APIs to retrieve: current waypoint, completed waypoints,
         remaining waypoints, active mission, original mission.

Design notes:

- "Reuse existing mission data structures" (requirement 1): every
  mission tracked here IS an `app.mission.schema.Mission` /
  `MissionItem` -- the exact same Pydantic models the LLM generator,
  validator, and converter already use. No new mission schema is
  introduced.
- "Do not duplicate the existing mission database unnecessarily"
  (requirement 2): there is no mission database in this project yet
  (that's separate, later scope -- the audit's Step 7 "Mission
  Learning/History Storage" / your plan's persistence phase). This
  manager is in-memory state for ONE currently-tracked mission, not a
  history store, so there's nothing to duplicate yet -- noted here so
  it's clear this was considered, not overlooked.
- `original_mission` vs. `active_mission`: this is the field your spec
  explicitly asks this version to add. `original_mission` never changes
  once `start()` is called. `active_mission` starts out identical to it,
  and is REPLACED (not mutated) by `apply_replan()` once a future
  Adaptive Mission Replanner phase produces a corrective route --
  `mission_version` and `adaptation_count` track how many times that has
  happened.
- No MAVSDK/asyncio/I/O dependency -- pure in-memory bookkeeping over
  `Mission`/`MissionItem`, same isolation property as
  `app/mission/validator.py`, which is what makes this trivially unit
  testable without a live PX4 SITL connection.
- `record_progress(current, total)` keeps the exact same MAVSDK
  `MissionProgress` semantics as the previous version and as
  `app/mavlink/px4_client.py`'s `_monitor_mission()` already consumes
  them (verified against mavsdk-python 3.17.2's generated
  `MissionProgress`: `current` is the 1-based index of the mission item
  currently being executed / just reached). It remains wired to
  `run_mission_on_px4()`'s optional `on_progress` callback added in the
  first version of this phase -- that hook's signature
  (`(current: int, total: int) -> None`) is unchanged, so no change was
  needed in `app/mavlink/px4_client.py` for this rewrite.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from app.mission.schema import Mission, MissionItem


class MissionStatus(str, Enum):
    """The exact 10 states specified for Phase 1."""

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    UPLOADED = "UPLOADED"
    RUNNING = "RUNNING"
    REPLANNING = "REPLANNING"
    UPDATING = "UPDATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    RETURNING_HOME = "RETURNING_HOME"


class ReplanningStatus(str, Enum):
    """
    Tracks a replanning attempt independently of `MissionStatus` --
    your spec lists `replanning_status` as its own tracked field,
    separate from `mission status` (which also has REPLANNING/UPDATING
    values for the overall mission lifecycle). Keeping them separate
    means "the mission is currently in the REPLANNING state" and "the
    most recent replan attempt succeeded/failed" can both be answered
    without overloading one enum.
    """

    NONE = "NONE"              # no replan in progress or requested
    TRIGGERED = "TRIGGERED"    # a trigger fired, replan not yet started
    IN_PROGRESS = "IN_PROGRESS"  # actively computing/uploading a corrective route
    APPLIED = "APPLIED"        # most recent replan succeeded and is now active
    FAILED = "FAILED"          # most recent replan attempt failed; old active_mission kept


class MissionStateError(Exception):
    """Raised for an invalid state transition or use-before-start."""


_TERMINAL_STATUSES = (MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.ABORTED)

# Allowed forward transitions for mark_*() calls that move `mission_status`.
# Not exhaustive of every real-world path (fail()/abort() are allowed from
# any non-terminal status and handled separately below) -- this covers the
# normal lifecycle plus the replanning loop.
_ALLOWED_TRANSITIONS: dict[MissionStatus, set[MissionStatus]] = {
    MissionStatus.CREATED: {MissionStatus.VALIDATED},
    MissionStatus.VALIDATED: {MissionStatus.UPLOADED},
    MissionStatus.UPLOADED: {MissionStatus.RUNNING},
    MissionStatus.RUNNING: {
        MissionStatus.REPLANNING,
        MissionStatus.RETURNING_HOME,
        MissionStatus.COMPLETED,
    },
    MissionStatus.REPLANNING: {MissionStatus.UPDATING, MissionStatus.RUNNING},  # RUNNING = replan failed, rollback
    MissionStatus.UPDATING: {MissionStatus.RUNNING},  # replan applied, resume flying
    MissionStatus.RETURNING_HOME: {MissionStatus.COMPLETED},
}


@dataclass
class MissionState:
    """
    Snapshot of everything Phase 1 requires this manager to track.
    Field names deliberately match your spec's own vocabulary
    (`mission_id`, `original_mission`, `active_mission`,
    `completed_waypoints`, `current_waypoint`, `remaining_waypoints`,
    `mission_status`, `replanning_status`, `adaptation_count`,
    `mission_version`) rather than being renamed to something else.
    """

    mission_id: str
    original_mission: Mission
    active_mission: Mission
    mission_status: MissionStatus
    replanning_status: ReplanningStatus
    adaptation_count: int
    mission_version: int

    current_sequence: int = 0  # 1-based; 0 = not yet started moving through active_mission
    current_waypoint: MissionItem | None = None
    completed_waypoints: list[MissionItem] = field(default_factory=list)
    remaining_waypoints: list[MissionItem] = field(default_factory=list)

    last_progress: tuple[int, int] | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def as_dict(self) -> dict:
        """Flat, JSON-serializable summary -- for logging/printing/future persistence."""
        return {
            "mission_id": self.mission_id,
            "mission_name": self.original_mission.mission_name,
            "mission_status": self.mission_status.value,
            "replanning_status": self.replanning_status.value,
            "adaptation_count": self.adaptation_count,
            "mission_version": self.mission_version,
            "current_sequence": self.current_sequence,
            "current_waypoint": (
                self.current_waypoint.model_dump() if self.current_waypoint else None
            ),
            "completed_count": len(self.completed_waypoints),
            "remaining_count": len(self.remaining_waypoints),
            "last_progress": self.last_progress,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class MissionStateManager:
    """
    Tracks the state of ONE mission at a time -- matches the reality that
    `run_mission_on_px4()` also only ever runs one mission at a time
    today.

    Typical lifecycle:
        manager = MissionStateManager()
        manager.start(mission)                          # CREATED
        manager.mark_validated()                         # VALIDATED
        manager.mark_uploaded()                           # UPLOADED
        manager.mark_running()                             # RUNNING
        manager.record_progress(current, total)  # called repeatedly while RUNNING
        ...
        # optional replanning loop:
        manager.start_replanning()                         # REPLANNING
        manager.begin_update()                              # UPDATING
        manager.apply_replan(new_active_mission)  # -> RUNNING, version+1, adaptation_count+1
        ...
        manager.complete()                                    # COMPLETED
    """

    def __init__(self) -> None:
        self._state: MissionState | None = None

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def start(self, mission: Mission, mission_id: str | None = None) -> MissionState:
        """Begin tracking a new mission. Status starts at CREATED."""
        now = datetime.now(timezone.utc)
        self._state = MissionState(
            mission_id=mission_id or str(uuid.uuid4()),
            original_mission=mission,
            active_mission=mission,
            mission_status=MissionStatus.CREATED,
            replanning_status=ReplanningStatus.NONE,
            adaptation_count=0,
            mission_version=1,
            current_sequence=0,
            current_waypoint=None,
            completed_waypoints=[],
            remaining_waypoints=list(sorted(mission.mission_items, key=lambda i: i.sequence)),
            created_at=now,
            updated_at=now,
        )
        return self.snapshot()

    def mark_validated(self) -> MissionState:
        return self._transition(MissionStatus.VALIDATED)

    def mark_uploaded(self) -> MissionState:
        return self._transition(MissionStatus.UPLOADED)

    def mark_running(self) -> MissionState:
        return self._transition(MissionStatus.RUNNING)

    def mark_returning_home(self) -> MissionState:
        return self._transition(MissionStatus.RETURNING_HOME)

    def complete(self) -> MissionState:
        state = self._transition(MissionStatus.COMPLETED)
        state.remaining_waypoints = []
        return state

    def fail(self, error: str) -> MissionState:
        """Allowed from any non-terminal status -- an unexpected failure can happen at any stage."""
        state = self._require_started()
        if state.mission_status in _TERMINAL_STATUSES:
            raise MissionStateError(f"Cannot fail from terminal status {state.mission_status.value}.")
        state.mission_status = MissionStatus.FAILED
        state.last_error = error
        state.updated_at = datetime.now(timezone.utc)
        return self.snapshot()

    def abort(self, reason: str | None = None) -> MissionState:
        """Allowed from any non-terminal status -- an operator can cancel at any stage."""
        state = self._require_started()
        if state.mission_status in _TERMINAL_STATUSES:
            raise MissionStateError(f"Cannot abort from terminal status {state.mission_status.value}.")
        state.mission_status = MissionStatus.ABORTED
        if reason:
            state.last_error = reason
        state.updated_at = datetime.now(timezone.utc)
        return self.snapshot()

    # -----------------------------------------------------------------
    # Progress tracking (while RUNNING or RETURNING_HOME)
    # -----------------------------------------------------------------

    def record_progress(self, current: int, total: int) -> MissionState:
        """
        Update tracked state from one MAVSDK `MissionProgress` reading
        against `active_mission` (NOT `original_mission` -- after a
        replan, progress is tracked against whatever is actually
        flying). Recomputes completed/remaining/current_waypoint from
        scratch each call, so repeated or idempotent calls are harmless.
        """
        state = self._require_started()
        if state.mission_status not in (MissionStatus.RUNNING, MissionStatus.RETURNING_HOME):
            raise MissionStateError(
                f"Cannot record progress while mission_status is {state.mission_status.value} "
                f"(must be RUNNING or RETURNING_HOME)."
            )

        items = sorted(state.active_mission.mission_items, key=lambda i: i.sequence)
        completed = [item for item in items if item.sequence < current]
        remaining = [item for item in items if item.sequence >= current]

        state.current_sequence = current
        state.completed_waypoints = completed
        state.remaining_waypoints = remaining
        state.current_waypoint = remaining[0] if remaining else None
        state.last_progress = (current, total)
        state.updated_at = datetime.now(timezone.utc)
        return self.snapshot()

    # -----------------------------------------------------------------
    # Replanning loop
    # -----------------------------------------------------------------

    def start_replanning(self) -> MissionState:
        """RUNNING -> REPLANNING. A trigger fired; a corrective route is being computed."""
        state = self._transition(MissionStatus.REPLANNING)
        state.replanning_status = ReplanningStatus.IN_PROGRESS
        return state

    def begin_update(self) -> MissionState:
        """REPLANNING -> UPDATING. The corrective route is computed; now uploading it to PX4."""
        return self._transition(MissionStatus.UPDATING)

    def apply_replan(self, new_active_mission: Mission) -> MissionState:
        """
        UPDATING -> RUNNING. The corrective route was uploaded and
        accepted: `active_mission` is REPLACED (not merged) with
        `new_active_mission`, `mission_version` increments, and
        `adaptation_count` increments. `original_mission` is untouched.
        Progress tracking (`current_sequence`/completed/remaining) resets
        against the new active mission -- the caller should follow this
        with `record_progress()` once PX4 reports progress against it.
        """
        state = self._require_started()
        if state.mission_status != MissionStatus.UPDATING:
            raise MissionStateError(
                f"Cannot apply a replan from status {state.mission_status.value} (must be UPDATING)."
            )
        state.active_mission = new_active_mission
        state.mission_version += 1
        state.adaptation_count += 1
        state.replanning_status = ReplanningStatus.APPLIED
        state.current_sequence = 0
        state.current_waypoint = None
        state.completed_waypoints = []
        state.remaining_waypoints = list(
            sorted(new_active_mission.mission_items, key=lambda i: i.sequence)
        )
        state.mission_status = MissionStatus.RUNNING
        state.updated_at = datetime.now(timezone.utc)
        return self.snapshot()

    def fail_replan(self, error: str) -> MissionState:
        """
        Allowed from REPLANNING or UPDATING. The replan attempt failed --
        roll back to RUNNING on the UNCHANGED `active_mission` (the drone
        keeps flying its old plan; nothing about mission_version/
        adaptation_count changes since nothing was actually applied).
        """
        state = self._require_started()
        if state.mission_status not in (MissionStatus.REPLANNING, MissionStatus.UPDATING):
            raise MissionStateError(
                f"Cannot fail a replan from status {state.mission_status.value} "
                f"(must be REPLANNING or UPDATING)."
            )
        state.mission_status = MissionStatus.RUNNING
        state.replanning_status = ReplanningStatus.FAILED
        state.last_error = error
        state.updated_at = datetime.now(timezone.utc)
        return self.snapshot()

    # -----------------------------------------------------------------
    # Required getter API (spec requirement 3)
    # -----------------------------------------------------------------

    def get_current_waypoint(self) -> MissionItem | None:
        return self._require_started().current_waypoint

    def get_completed_waypoints(self) -> list[MissionItem]:
        return list(self._require_started().completed_waypoints)

    def get_remaining_waypoints(self) -> list[MissionItem]:
        return list(self._require_started().remaining_waypoints)

    def get_active_mission(self) -> Mission:
        return self._require_started().active_mission

    def get_original_mission(self) -> Mission:
        return self._require_started().original_mission

    def snapshot(self) -> MissionState:
        """Full state snapshot -- for logging, tests, or a future UI/API layer."""
        return self._require_started()

    @property
    def is_terminal(self) -> bool:
        return self._require_started().mission_status in _TERMINAL_STATUSES

    # -----------------------------------------------------------------
    # Forward-looking helper (not part of the spec, kept for the
    # Adaptive Mission Replanner phase that will build on this one)
    # -----------------------------------------------------------------

    def remaining_route(self, mission_name: str | None = None) -> Mission:
        """
        Build a new, independently-valid `Mission` out of whatever is
        still `remaining_waypoints`, with sequence numbers renumbered
        from 1. Not used by anything yet -- scaffolding for the future
        Adaptive Mission Replanner phase, which needs exactly this as
        the starting point for a corrective route (see the audit's
        Step 7, "Mission State Manager ... Outputs: 'Remaining route'
        data structure").
        """
        state = self._require_started()
        renumbered = [
            item.model_copy(update={"sequence": i})
            for i, item in enumerate(state.remaining_waypoints, start=1)
        ]
        return Mission(
            mission_name=mission_name or f"{state.active_mission.mission_name} (remaining)",
            mission_items=renumbered,
        )

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    def _transition(self, new_status: MissionStatus) -> MissionState:
        state = self._require_started()
        allowed = _ALLOWED_TRANSITIONS.get(state.mission_status, set())
        if new_status not in allowed:
            raise MissionStateError(
                f"Invalid transition: {state.mission_status.value} -> {new_status.value}."
            )
        state.mission_status = new_status
        state.updated_at = datetime.now(timezone.utc)
        return self.snapshot()

    def _require_started(self) -> MissionState:
        if self._state is None:
            raise MissionStateError("start() must be called before using the state manager.")
        return self._state
