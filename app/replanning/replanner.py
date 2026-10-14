"""
RAMR novelty Phase 4 -- Basic Telemetry-Based RAMR.

    OBJECTIVE: Implement the basic Real-Time Adaptive Mission Replanner
    without using LLM reasoning initially.
    CREATE: AdaptiveMissionReplanner
    START WITH SIMPLE TRIGGERS: 1. Battery deviation  2. Missed waypoint
    3. Mission deviation
    REQUIREMENT: Use deterministic or rule-based replanning first.
    SUCCESS CRITERIA: Telemetry event -> RAMR -> Modified remaining mission
    DO NOT USE THE LLM YET.

This module implements exactly the worked example in your spec:

    Original Mission: WP1 -> WP2 -> WP3 -> WP4 -> WP5
    Drone is currently at: WP3
    Battery becomes insufficient.
    RAMR should evaluate: current drone position, completed waypoints,
    remaining waypoints, battery, minimum battery reserve.
    Then generate: Modified Remaining Mission, e.g. WP4 -> RTH or
    WP4 -> WP5 -> RTH depending on safety constraints.

Design notes:

- `AdaptiveMissionReplanner.replan()` takes a `ReplanningEvent` (Phase 3),
  the current `MissionState` (Phase 1: gives original/active mission,
  completed/remaining waypoints, current sequence), and `DroneState`
  (Phase 2: gives live position/battery) -- every input your spec lists
  ("current drone position, completed waypoints, remaining waypoints,
  battery, minimum battery reserve") comes from data structures already
  built in earlier phases. Nothing new is invented to represent them.
- Deterministic, NOT LLM-based, exactly as instructed: the battery
  handler is a distance-budget calculation (haversine great-circle
  distance between consecutive waypoints, a flat per-km battery-cost
  estimate, and `settings.min_battery_reserve_percent` as the floor it
  won't plan below). This is a genuinely simple placeholder model --
  documented as such below and in `app/config.py` -- not a validated
  flight-energy model. Good enough to make a real, testable decision
  today; refining it (wind, payload, climb cost, a real endurance curve)
  is explicitly out of scope for "basic telemetry-based RAMR."
- Home position (needed to estimate the cost of returning) is NOT
  currently tracked by `DroneStateManager` (Phase 2 didn't ask for it,
  and PX4 only reports it via `telemetry.home()`, a stream Phase 2 does
  not subscribe to) -- rather than silently assume a value, `replan()`
  takes `home_latitude_deg`/`home_longitude_deg` as explicit parameters.
  `app/mavlink/px4_client.py`'s `_get_home_position()` already knows how
  to fetch this from PX4 SITL and is the natural source once this is
  wired into a live run.
- The resulting "Modified Remaining Mission" is validated with the new
  `app.mission.validator.validate_remaining_mission()` (an additive
  extension of the existing Phase 5 validator -- see that file) before
  being returned, so a structurally broken replan is caught the same way
  a structurally broken LLM-generated mission already is.
- Per "DO NOT USE THE LLM YET" and "START WITH SIMPLE TRIGGERS" (exactly
  these three), any other `EventType` was originally left deliberately
  unhandled here -- `replan()` returned an `UNHANDLED_EVENT_TYPE` result
  rather than guessing. SAFETY_EVENT/OPERATOR_REQUEST/COVERAGE_GAP_DETECTED
  still work that way (no coverage-gap detector exists in this project at
  all, so that one stays out of scope). The three CV quality-failure event
  types are now handled below -- see the Phase 14 notes immediately after
  this paragraph.

RAMR/CV novelty Phase 14 -- CV-Triggered Adaptive Replanning:

    OBJECTIVE: Connect Idea 5 (CV inspection quality) to Idea 1 (RAMR).
    "Computer Vision is NOT a separate mission planning system. It must
    become a trigger source for RAMR."
    FLOW: IMAGE_BLUR_DETECTED -> TriggerEngine -> AdaptiveMissionReplanner
    -> [current drone state + mission state + remaining waypoints +
    battery + safety constraints] -> Replanning Decision.
    RAMR should determine: (1) does the area need revisiting, (2) can the
    mission continue without correction, (3) is battery sufficient for
    the correction, (4) where should the corrective waypoint be inserted,
    (5) should the drone revisit immediately, (6) should the drone
    revisit later, (7) should the mission return home.
    SUCCESS CRITERIA: A CV quality failure can cause safe mission
    adaptation.

- "CV is a trigger source for RAMR, not a separate planning system" is
  implemented literally: `_handle_cv_quality_event()` below is just a
  THIRD deterministic handler alongside `_handle_battery_deviation()` and
  `_handle_missed_waypoint()`, dispatched from the exact same `replan()`
  entry point, taking the exact same `ReplanningEvent`/`MissionState`/
  `DroneState` inputs. `RAMROrchestrator` (Phase 10) already calls
  `replanner.replan(event, ...)` for every event type generically -- it
  required NO changes for Phase 14, because it was never CV-specific to
  begin with (see app/replanning/orchestrator.py). A CV event that
  reaches here has therefore already gone through the identical
  FINAL FLOW every telemetry event goes through: TriggerEngine (severity
  gating) -> AdaptiveMissionReplanner (this file) -> [if UNSUPPORTED/
  UNHANDLED] RAG+LLM -> Safety Validator -> Battery Checker ->
  MissionUpdateManager -> MAVSDK -- so "safety-critical actions must not
  depend solely on the LLM" and "no revised mission reaches MAVSDK
  without validation" both already hold for CV-triggered replans exactly
  as they do for telemetry-triggered ones, with zero new code in those
  later stages.
- Handles all three CV quality event types from Phase 13
  (`IMAGE_BLUR_DETECTED`, `BAD_EXPOSURE_DETECTED`,
  `INSPECTION_QUALITY_FAILURE`) the same way: the corrective action is
  "go look at that spot again," regardless of which specific quality
  check failed.
- How each of the spec's 7 questions is answered, deterministically:
    1. "Does the area need revisiting?" -- yes, whenever this handler
       runs at all. This is implicit rather than a separate branch: the
       `TriggerEngine` (Phase 3) already gates on
       `DEFAULT_ACTIVATION_THRESHOLDS[EventType.IMAGE_BLUR_DETECTED/...]
       = MEDIUM` before `RAMROrchestrator` ever calls `replan()` -- a
       LOW-severity CV event never reaches this method. So "does it need
       revisiting" was already answered upstream by the same
       significance gate every other event type uses; this method's job
       starts at question 2.
    2. "Can the mission continue without correction?" -- yes, ONLY if
       question 3 says battery can't afford the correction (see below):
       the mission is never abandoned or blocked on an inspection gap,
       it just proceeds without revisiting that spot, and that's stated
       plainly in the returned `reason`. Otherwise, no -- a correction
       (revisit waypoint) is planned.
    3. "Is battery sufficient for correction?" -- computed exactly like
       `_handle_battery_deviation()`'s existing budget math (reused, not
       reinvented): `usable_battery / battery_consumption_percent_per_km`
       is the flight-distance budget; the cost of flying the ORIGINAL
       remaining route + RTH is checked first (if that alone is already
       infeasible, see question 7), then the cost of ALSO detouring to
       revisit the flagged waypoint is added and checked against the same
       budget.
    4. "Where should the corrective waypoint be inserted?" -- either
       right after the drone's current position (before any other
       remaining waypoint) for an immediate revisit, or just before the
       mission's closing RETURN_TO_LAUNCH (after every other remaining
       waypoint) for a deferred one. See question 5/6.
    5/6. "Revisit immediately, or later?" -- `event.severity` (already
       the project's standing signal for "how urgent is this," used the
       same way by `_handle_mission_deviation()` below) decides: HIGH/
       CRITICAL inserts the revisit immediately; MEDIUM (the lowest
       severity that can reach this method, per question 1's gating)
       defers it to the end of the remaining route.
    7. "Should the mission return home?" -- yes, immediately (RTH-only,
       no revisit attempted) if the battery can't even complete the
       ALREADY-PLANNED remaining route -- a genuine feasibility problem
       that pre-exists and is unrelated to the inspection gap, so it
       takes priority over any corrective detour.
- The corrective waypoint itself is a `LOITER` `MissionItem` at the
  flagged waypoint's exact coordinates/altitude, holding for
  `settings.cv_revisit_hold_time_s` -- LOITER is the only "hover in
  place" command this project's schema already defines (see
  `app/mission/schema.py`), reused rather than inventing a new command
  or a new field on `MissionItem` (which has `extra="forbid"`) to mark a
  waypoint as "corrective."
- The detour-cost estimate (`extra_km` below) is a simple, documented
  placeholder like the rest of this module's battery model: distance from
  the drone's current position to the revisit point, plus from the
  revisit point back to wherever the route would otherwise have gone
  next (the next remaining waypoint, or home if none), minus the direct
  leg that detour replaces. Not a real path-planner, consistent with
  this phase's own "simple, rule-based" requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import atan2, cos, radians, sin, sqrt

from app.config import settings
from app.mavlink.drone_state import DroneState
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionState
from app.mission.validator import ValidationResult, validate_remaining_mission
from app.trigger.events import EventSeverity, EventType, ReplanningEvent

_EARTH_RADIUS_KM = 6371.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points. Generic, well-known formula."""
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * atan2(sqrt(a), sqrt(1 - a))


class RAMRStatus(str, Enum):
    MODIFIED = "MODIFIED"                    # a new remaining mission was produced
    UNCHANGED = "UNCHANGED"                    # deterministic engine chose not to modify the plan
    UNSUPPORTED = "UNSUPPORTED"                # missing telemetry/mission data needed to decide
    UNHANDLED_EVENT_TYPE = "UNHANDLED_EVENT_TYPE"  # not one of the 3 simple triggers this phase handles


@dataclass
class RAMRResult:
    """What `AdaptiveMissionReplanner.replan()` always returns."""

    status: RAMRStatus
    event: ReplanningEvent
    reason: str
    modified_mission: Mission | None = None
    validation: ValidationResult | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "event_id": self.event.event_id,
            "event_type": self.event.event_type.value,
            "reason": self.reason,
            "modified_mission": self.modified_mission.model_dump() if self.modified_mission else None,
            "validation": self.validation.as_dict() if self.validation else None,
        }


class AdaptiveMissionReplanner:
    """
    Usage:
        replanner = AdaptiveMissionReplanner()
        result = replanner.replan(
            event, mission_state, drone_state,
            home_latitude_deg=home_lat, home_longitude_deg=home_lon,
        )
        if result.status == RAMRStatus.MODIFIED:
            ... hand result.modified_mission to a future Safety
                Revalidator / MAVSDK Mission Update Manager phase ...
    """

    def __init__(
        self,
        min_battery_reserve_percent: float | None = None,
        battery_consumption_percent_per_km: float | None = None,
    ) -> None:
        self.min_battery_reserve_percent = (
            min_battery_reserve_percent
            if min_battery_reserve_percent is not None
            else settings.min_battery_reserve_percent
        )
        self.battery_consumption_percent_per_km = (
            battery_consumption_percent_per_km
            if battery_consumption_percent_per_km is not None
            else settings.battery_consumption_percent_per_km
        )

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def replan(
        self,
        event: ReplanningEvent,
        mission_state: MissionState,
        drone_state: DroneState,
        *,
        home_latitude_deg: float | None = None,
        home_longitude_deg: float | None = None,
    ) -> RAMRResult:
        """
        Dispatches to the deterministic handler for `event.event_type`.
        Never raises -- every branch (missing data, unhandled event type,
        a genuinely infeasible battery budget) returns an `RAMRResult`
        describing what happened, same "don't raise for an expected
        outcome" pattern used throughout this project.
        """
        if event.event_type == EventType.BATTERY_DEVIATION:
            return self._handle_battery_deviation(
                event, mission_state, drone_state, home_latitude_deg, home_longitude_deg
            )
        if event.event_type == EventType.MISSED_WAYPOINT:
            return self._handle_missed_waypoint(event, mission_state)
        if event.event_type == EventType.MISSION_DEVIATION:
            return self._handle_mission_deviation(event, mission_state)
        if event.event_type in (
            EventType.IMAGE_BLUR_DETECTED,
            EventType.BAD_EXPOSURE_DETECTED,
            EventType.INSPECTION_QUALITY_FAILURE,
        ):
            return self._handle_cv_quality_event(
                event, mission_state, drone_state, home_latitude_deg, home_longitude_deg
            )

        return RAMRResult(
            status=RAMRStatus.UNHANDLED_EVENT_TYPE,
            event=event,
            reason=(
                f"AdaptiveMissionReplanner only handles BATTERY_DEVIATION, MISSED_WAYPOINT, "
                f"MISSION_DEVIATION, and (RAMR/CV novelty Phase 14) the CV quality-failure "
                f"events (IMAGE_BLUR_DETECTED, BAD_EXPOSURE_DETECTED, INSPECTION_QUALITY_FAILURE) "
                f"deterministically -- {event.event_type.value} is out of scope for this phase."
            ),
        )

    # -----------------------------------------------------------------
    # Trigger 1: Battery deviation
    # -----------------------------------------------------------------

    def _handle_battery_deviation(
        self,
        event: ReplanningEvent,
        mission_state: MissionState,
        drone_state: DroneState,
        home_latitude_deg: float | None,
        home_longitude_deg: float | None,
    ) -> RAMRResult:
        if drone_state.latitude_deg is None or drone_state.longitude_deg is None:
            return RAMRResult(
                status=RAMRStatus.UNSUPPORTED, event=event,
                reason="Cannot evaluate a battery deviation without a known drone position.",
            )
        if drone_state.battery_remaining_percent is None:
            return RAMRResult(
                status=RAMRStatus.UNSUPPORTED, event=event,
                reason="Cannot evaluate a battery deviation without a known battery percentage.",
            )
        if home_latitude_deg is None or home_longitude_deg is None:
            return RAMRResult(
                status=RAMRStatus.UNSUPPORTED, event=event,
                reason=(
                    "Cannot evaluate a battery deviation without a home position to estimate "
                    "RTH cost from -- pass home_latitude_deg/home_longitude_deg (e.g. from "
                    "px4_client._get_home_position())."
                ),
            )

        # "Remaining waypoints" strictly AHEAD of where the drone is right
        # now (mission_state.remaining_waypoints includes the just-reached
        # current waypoint itself -- see app/mission/state.py -- but the
        # spec's own example starts the modified plan at WP4, one past
        # the drone's current position at WP3).
        waypoints_ahead = [
            item for item in mission_state.remaining_waypoints
            if item.sequence > mission_state.current_sequence
        ]
        # Only items with real coordinates participate in the distance
        # walk -- this naturally excludes a coordinate-less trailing
        # LAND/RETURN_TO_LAUNCH from the original plan, since this
        # handler always appends its OWN fresh RETURN_TO_LAUNCH below
        # regardless of what the original mission ended with.
        waypoints_with_coords = [
            item for item in waypoints_ahead
            if item.latitude is not None and item.longitude is not None
        ]

        usable_battery = drone_state.battery_remaining_percent - self.min_battery_reserve_percent
        budget_km = (
            max(0.0, usable_battery) / self.battery_consumption_percent_per_km
            if self.battery_consumption_percent_per_km > 0
            else 0.0
        )

        path = [(drone_state.latitude_deg, drone_state.longitude_deg)] + [
            (item.latitude, item.longitude) for item in waypoints_with_coords
        ]

        selected: list[MissionItem] = []
        cumulative_km = 0.0
        for i, item in enumerate(waypoints_with_coords):
            leg_km = _haversine_km(*path[i], *path[i + 1])
            candidate_cumulative_km = cumulative_km + leg_km
            home_leg_km = _haversine_km(*path[i + 1], home_latitude_deg, home_longitude_deg)
            if candidate_cumulative_km + home_leg_km <= budget_km:
                selected.append(item)
                cumulative_km = candidate_cumulative_km
            else:
                break

        modified_items = [
            item.model_copy(update={"sequence": i})
            for i, item in enumerate(selected, start=1)
        ]
        modified_items.append(
            MissionItem(sequence=len(modified_items) + 1, command=MissionCommand.RETURN_TO_LAUNCH)
        )

        validation = validate_remaining_mission(modified_items)
        if not validation.valid:
            # Defensive fallback -- should not happen given how
            # modified_items is constructed, but never hand back a
            # mission this project's own validator rejects.
            rth_only = [MissionItem(sequence=1, command=MissionCommand.RETURN_TO_LAUNCH)]
            return RAMRResult(
                status=RAMRStatus.MODIFIED, event=event,
                reason=(
                    "Computed battery-feasibility replan failed validation "
                    f"({validation.errors}) -- falling back to immediate RTH."
                ),
                modified_mission=Mission(
                    mission_name=f"{mission_state.active_mission.mission_name} (RTH fallback)",
                    mission_items=rth_only,
                ),
                validation=validate_remaining_mission(rth_only),
            )

        if not selected:
            reason = (
                f"Battery at {drone_state.battery_remaining_percent:.0%} leaves no safe margin "
                f"above the {self.min_battery_reserve_percent:.0%} reserve for any further "
                f"waypoint -- commanding immediate RTH."
            )
        elif len(selected) == len(waypoints_with_coords):
            reason = (
                f"Battery at {drone_state.battery_remaining_percent:.0%} is sufficient for all "
                f"{len(selected)} remaining waypoint(s) plus RTH with the "
                f"{self.min_battery_reserve_percent:.0%} reserve intact -- keeping full remaining "
                f"route, then RTH."
            )
        else:
            reason = (
                f"Battery at {drone_state.battery_remaining_percent:.0%} only covers "
                f"{len(selected)} of {len(waypoints_with_coords)} remaining waypoint(s) within the "
                f"{self.min_battery_reserve_percent:.0%} reserve -- trimming the remaining route "
                f"and returning to launch early."
            )

        modified_mission = Mission(
            mission_name=f"{mission_state.active_mission.mission_name} (battery-deviation replan)",
            mission_items=modified_items,
        )
        return RAMRResult(
            status=RAMRStatus.MODIFIED, event=event, reason=reason,
            modified_mission=modified_mission, validation=validation,
        )

    # -----------------------------------------------------------------
    # Trigger 2: Missed waypoint
    # -----------------------------------------------------------------

    def _handle_missed_waypoint(self, event: ReplanningEvent, mission_state: MissionState) -> RAMRResult:
        missed_sequence = event.metadata.get("missed_sequence")
        if missed_sequence is None:
            return RAMRResult(
                status=RAMRStatus.UNSUPPORTED, event=event,
                reason=(
                    "MISSED_WAYPOINT event is missing metadata['missed_sequence'] -- cannot "
                    "determine which waypoint to drop from the remaining route."
                ),
            )

        waypoints_ahead = [
            item for item in mission_state.remaining_waypoints
            if item.sequence > mission_state.current_sequence
        ]
        filtered = [item for item in waypoints_ahead if item.sequence != missed_sequence]

        if not filtered or filtered[-1].command not in (
            MissionCommand.LAND, MissionCommand.RETURN_TO_LAUNCH
        ):
            # The missed item was the mission's own terminator (or
            # nothing is left at all) -- ensure the remaining route still
            # safely ends the mission.
            filtered = [item for item in filtered if item.command not in (
                MissionCommand.LAND, MissionCommand.RETURN_TO_LAUNCH
            )]
            # sequence=1 here is a placeholder -- every item in `filtered`
            # is renumbered from 1 immediately below regardless.
            filtered.append(MissionItem(sequence=1, command=MissionCommand.RETURN_TO_LAUNCH))

        modified_items = [
            item.model_copy(update={"sequence": i}) for i, item in enumerate(filtered, start=1)
        ]

        validation = validate_remaining_mission(modified_items)
        modified_mission = Mission(
            mission_name=f"{mission_state.active_mission.mission_name} (missed-waypoint replan)",
            mission_items=modified_items,
        )
        return RAMRResult(
            status=RAMRStatus.MODIFIED, event=event,
            reason=(
                f"Dropped missed waypoint #{missed_sequence} from the remaining route and "
                f"continued with the rest of the plan unchanged."
            ),
            modified_mission=modified_mission, validation=validation,
        )

    # -----------------------------------------------------------------
    # Trigger 3: Mission deviation
    # -----------------------------------------------------------------

    def _handle_mission_deviation(self, event: ReplanningEvent, mission_state: MissionState) -> RAMRResult:
        """
        Deliberately conservative: without CV/LLM reasoning to understand
        WHY the drone deviated, this deterministic phase cannot safely
        compute an intelligent corrective route. HIGH/CRITICAL severity
        gets an immediate RTH (safety-first); LOW/MEDIUM makes no change
        -- both are legitimate, documented decisions, not missing
        functionality. A future LLM-assisted replanning phase is where
        nuanced course-correction belongs.
        """
        if event.severity in (EventSeverity.HIGH, EventSeverity.CRITICAL):
            rth_only = [MissionItem(sequence=1, command=MissionCommand.RETURN_TO_LAUNCH)]
            validation = validate_remaining_mission(rth_only)
            modified_mission = Mission(
                mission_name=f"{mission_state.active_mission.mission_name} (deviation RTH)",
                mission_items=rth_only,
            )
            return RAMRResult(
                status=RAMRStatus.MODIFIED, event=event,
                reason=(
                    f"MISSION_DEVIATION severity {event.severity.value} -- without CV/LLM "
                    f"reasoning available, the safe deterministic response is immediate RTH "
                    f"rather than attempting to guess a corrective route."
                ),
                modified_mission=modified_mission, validation=validation,
            )

        return RAMRResult(
            status=RAMRStatus.UNCHANGED, event=event,
            reason=(
                f"MISSION_DEVIATION severity {event.severity.value} is below the safety-abort "
                f"threshold, and this deterministic phase has no LLM/CV reasoning available to "
                f"compute an intelligent correction -- taking no action; the active mission is "
                f"kept as-is."
            ),
        )

    # -----------------------------------------------------------------
    # RAMR/CV novelty Phase 14: CV-triggered adaptive replanning
    # -----------------------------------------------------------------

    def _handle_cv_quality_event(
        self,
        event: ReplanningEvent,
        mission_state: MissionState,
        drone_state: DroneState,
        home_latitude_deg: float | None,
        home_longitude_deg: float | None,
    ) -> RAMRResult:
        """
        See this module's docstring ("RAMR/CV novelty Phase 14") for the
        full design and how each of the spec's 7 questions maps to a
        branch below.
        """
        if event.waypoint_id is None:
            return RAMRResult(
                status=RAMRStatus.UNSUPPORTED, event=event,
                reason=(
                    f"{event.event_type.value} event carries no waypoint_id -- cannot determine "
                    f"which location needs revisiting."
                ),
            )

        target = next(
            (item for item in mission_state.active_mission.mission_items if item.sequence == event.waypoint_id),
            None,
        )
        if target is None or target.latitude is None or target.longitude is None:
            return RAMRResult(
                status=RAMRStatus.UNSUPPORTED, event=event,
                reason=(
                    f"Waypoint #{event.waypoint_id} (flagged by {event.event_type.value}) was not "
                    f"found in the active mission, or has no coordinates -- cannot plan a revisit."
                ),
            )
        if drone_state.latitude_deg is None or drone_state.longitude_deg is None:
            return RAMRResult(
                status=RAMRStatus.UNSUPPORTED, event=event,
                reason="Cannot evaluate a CV-triggered replan without a known drone position.",
            )
        if drone_state.battery_remaining_percent is None:
            return RAMRResult(
                status=RAMRStatus.UNSUPPORTED, event=event,
                reason="Cannot evaluate a CV-triggered replan without a known battery percentage.",
            )
        if home_latitude_deg is None or home_longitude_deg is None:
            return RAMRResult(
                status=RAMRStatus.UNSUPPORTED, event=event,
                reason=(
                    "Cannot evaluate a CV-triggered replan without a home position to estimate "
                    "RTH cost from -- pass home_latitude_deg/home_longitude_deg."
                ),
            )

        waypoints_ahead = [
            item for item in mission_state.remaining_waypoints
            if item.sequence > mission_state.current_sequence
        ]
        waypoints_with_coords = [
            item for item in waypoints_ahead
            if item.latitude is not None and item.longitude is not None
        ]

        usable_battery = drone_state.battery_remaining_percent - self.min_battery_reserve_percent
        budget_km = (
            max(0.0, usable_battery) / self.battery_consumption_percent_per_km
            if self.battery_consumption_percent_per_km > 0
            else 0.0
        )

        drone_point = (drone_state.latitude_deg, drone_state.longitude_deg)
        home_point = (home_latitude_deg, home_longitude_deg)
        target_point = (target.latitude, target.longitude)

        route_points = [drone_point] + [(item.latitude, item.longitude) for item in waypoints_with_coords]
        original_route_km = sum(
            _haversine_km(*route_points[i], *route_points[i + 1]) for i in range(len(route_points) - 1)
        )
        home_leg_km = _haversine_km(*route_points[-1], *home_point)

        # Question 7: is the battery even enough to finish the ALREADY-PLANNED
        # route (no correction at all)? A pre-existing infeasibility takes
        # priority over the inspection gap.
        if original_route_km + home_leg_km > budget_km:
            rth_only = [MissionItem(sequence=1, command=MissionCommand.RETURN_TO_LAUNCH)]
            validation = validate_remaining_mission(rth_only)
            modified_mission = Mission(
                mission_name=f"{mission_state.active_mission.mission_name} (CV-triggered RTH)",
                mission_items=rth_only,
            )
            return RAMRResult(
                status=RAMRStatus.MODIFIED, event=event,
                reason=(
                    f"{event.event_type.value} at waypoint #{event.waypoint_id} -- but battery at "
                    f"{drone_state.battery_remaining_percent:.0%} cannot even complete the "
                    f"already-planned remaining route ({original_route_km + home_leg_km:.2f}km "
                    f"needed vs {budget_km:.2f}km budget) -- returning home now; the inspection "
                    f"gap is not addressed."
                ),
                modified_mission=modified_mission, validation=validation,
            )

        # Question 3: is battery sufficient for the correction (a detour to
        # revisit the flagged waypoint) on top of the already-affordable
        # original route?
        rejoin_point = route_points[1] if waypoints_with_coords else home_point
        detour_km = _haversine_km(*drone_point, *target_point) + _haversine_km(*target_point, *rejoin_point)
        direct_leg_km = _haversine_km(*drone_point, *rejoin_point)
        extra_km = max(0.0, detour_km - direct_leg_km)
        total_km_with_correction = original_route_km + home_leg_km + extra_km

        if total_km_with_correction > budget_km:
            # Question 3: no. Question 2: yes -- continue without correction
            # rather than abandon or stall the mission over an inspection gap.
            return RAMRResult(
                status=RAMRStatus.UNCHANGED, event=event,
                reason=(
                    f"{event.event_type.value} at waypoint #{event.waypoint_id} -- area needs "
                    f"revisiting, but battery at {drone_state.battery_remaining_percent:.0%} is not "
                    f"sufficient for the correction ({total_km_with_correction:.2f}km needed vs "
                    f"{budget_km:.2f}km budget, {extra_km:.2f}km of that being the detour) -- "
                    f"continuing the original mission without a revisit; the inspection gap at "
                    f"waypoint #{event.waypoint_id} is accepted."
                ),
            )

        # Questions 4/5/6: battery affords the correction -- decide
        # immediate vs. deferred insertion from event severity (same
        # urgency signal _handle_mission_deviation() already uses).
        revisit_item = MissionItem(
            sequence=1,  # placeholder -- every item below is renumbered from 1
            command=MissionCommand.LOITER,
            latitude=target.latitude,
            longitude=target.longitude,
            altitude=target.altitude,
            hold_time_s=settings.cv_revisit_hold_time_s,
        )

        if event.severity in (EventSeverity.HIGH, EventSeverity.CRITICAL):
            ordered_items = [revisit_item] + waypoints_with_coords
            timing = (
                f"immediately, right after the drone's current position (severity "
                f"{event.severity.value} warrants prompt correction)"
            )
        else:
            ordered_items = waypoints_with_coords + [revisit_item]
            timing = (
                f"after completing the rest of the remaining route, just before returning home "
                f"(severity {event.severity.value} does not require an immediate detour)"
            )

        modified_items = [
            item.model_copy(update={"sequence": i}) for i, item in enumerate(ordered_items, start=1)
        ]
        modified_items.append(
            MissionItem(sequence=len(modified_items) + 1, command=MissionCommand.RETURN_TO_LAUNCH)
        )

        validation = validate_remaining_mission(modified_items)
        if not validation.valid:
            # Defensive fallback -- should not happen given how
            # modified_items is constructed, but never hand back a
            # mission this project's own validator rejects.
            rth_only = [MissionItem(sequence=1, command=MissionCommand.RETURN_TO_LAUNCH)]
            return RAMRResult(
                status=RAMRStatus.MODIFIED, event=event,
                reason=(
                    "Computed CV-triggered corrective replan failed validation "
                    f"({validation.errors}) -- falling back to immediate RTH."
                ),
                modified_mission=Mission(
                    mission_name=f"{mission_state.active_mission.mission_name} (RTH fallback)",
                    mission_items=rth_only,
                ),
                validation=validate_remaining_mission(rth_only),
            )

        modified_mission = Mission(
            mission_name=f"{mission_state.active_mission.mission_name} (CV-triggered revisit)",
            mission_items=modified_items,
        )
        return RAMRResult(
            status=RAMRStatus.MODIFIED, event=event,
            reason=(
                f"{event.event_type.value} at waypoint #{event.waypoint_id} -- area needs "
                f"revisiting; battery at {drone_state.battery_remaining_percent:.0%} is sufficient "
                f"for the correction ({total_km_with_correction:.2f}km within the {budget_km:.2f}km "
                f"budget) -- inserting a corrective LOITER revisit waypoint {timing}."
            ),
            modified_mission=modified_mission, validation=validation,
        )


def _demo_cli() -> None:
    """
    Manual test entry point, matching your spec's own TEST section:
    simulate a battery event during a mission and watch
    Telemetry event -> RAMR -> Modified remaining mission, using exactly
    your worked example (WP1..WP5, drone at WP3, battery insufficient).
        python -m app.replanning.replanner
    No PX4/LLM/network needed -- everything here is synthetic/offline.
    """
    import json

    from app.mavlink.drone_state import DroneState
    from app.mission.schema import Mission, MissionCommand, MissionItem
    from app.mission.state import MissionStateManager
    from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent

    # Home and waypoints ~1km apart (1 degree latitude =~ 111km, so
    # 0.009 degrees =~ 1km) -- realistic small-survey spacing, unlike an
    # arbitrary large offset, so the battery budget below produces the
    # spec's own "WP4 -> RTH" trimmed example rather than an
    # always-immediate-RTH result regardless of the numbers.
    home_lat, home_lon = 1.0000, 2.0000
    mission = Mission(
        mission_name="Demo 5-waypoint survey",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0000, longitude=2.0000, altitude=30),  # "WP1"
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.0090, longitude=2.0000, altitude=30),  # "WP2" -- drone is here
            MissionItem(sequence=4, command=MissionCommand.WAYPOINT, latitude=1.0180, longitude=2.0000, altitude=30),  # "WP3"
            MissionItem(sequence=5, command=MissionCommand.WAYPOINT, latitude=1.0270, longitude=2.0000, altitude=30),  # "WP4"
            MissionItem(sequence=6, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )
    # (Sequence numbers here include TAKEOFF at #1, so this schema's item
    # #3 plays the role of the spec's "WP3, where the drone currently
    # is" -- the mechanism is what matters, not matching the docstring's
    # WP1..WP5 labels literally.)

    manager = MissionStateManager()
    manager.start(mission)
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=3, total=6)  # drone "currently at" item 3, ~1km from home

    drone_state = DroneState(
        latitude_deg=1.0090, longitude_deg=2.0000, relative_altitude_m=30.0,
        battery_remaining_percent=0.22,
    )

    event = ReplanningEvent(
        event_type=EventType.BATTERY_DEVIATION,
        severity=EventSeverity.HIGH,
        source=EventSource.TELEMETRY,
        mission_id=manager.snapshot().mission_id,
        drone_state=drone_state,
        mission_state=manager.snapshot(),
        metadata={"battery_percent": 0.22, "min_reserve_percent": 0.15},
    )

    replanner = AdaptiveMissionReplanner()
    result = replanner.replan(
        event, manager.snapshot(), drone_state,
        home_latitude_deg=home_lat, home_longitude_deg=home_lon,
    )

    print("Telemetry event:")
    print(json.dumps(event.as_dict(), indent=2))
    print(f"\nRAMR status: {result.status.value}")
    print(f"Reason: {result.reason}")
    print("\nModified remaining mission:")
    print(json.dumps(result.modified_mission.model_dump(), indent=2) if result.modified_mission else "(none)")


if __name__ == "__main__":
    _demo_cli()
