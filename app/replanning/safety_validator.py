"""
RAMR novelty Phase 7 -- Replanning Safety Validation.

    OBJECTIVE: Reuse the existing Safety Validator to validate every
    adaptive mission change.
    NEW ARCHITECTURE: Replanning Event -> Revised Mission Proposal ->
    Waypoint Processing -> Existing Safety Validator -> Battery
    Feasibility Check -> Approved / Rejected.
    VALIDATE: Altitude, Speed, Geofence, Coordinates, Safety boundaries,
    Building boundaries, Battery reserve, Return-to-home capability,
    Corrective waypoint safety.
    IMPORTANT: No revised mission can reach MAVSDK without validation.
    IF INVALID: Reject adaptation. Fallback action: Continue original
    mission if safe, OR Return to Home, OR Abort safely.
    SUCCESS CRITERIA: Unsafe revised missions are always rejected.

Design notes:

- "Reuse the existing Safety Validator" -- taken literally.
  `app.mission.validator.validate_remaining_mission()` (Phase 4/5) is
  called UNCHANGED against the proposed route -- it already covers
  sequencing, per-command required/forbidden fields, mission-ordering
  (no TAKEOFF mid-flight, must end in a terminator, nothing after a
  terminator), and the Null Island warning. Altitude and Coordinates are
  ALSO already enforced -- not re-implemented here -- because every
  `MissionItem` on a `RevisedMissionProposal` is the SAME Pydantic model
  Phase 3/4 defined (`app/mission/schema.py`), which range-checks
  latitude (-90..90), longitude (-180..180), and altitude
  (`settings.min_altitude_m`..`settings.max_altitude_m`) at construction
  time -- an out-of-range value can't even exist as a `MissionItem`
  object by the time it reaches this module. This module adds ONLY the
  checks nothing earlier in the project performs: Speed, Geofence,
  Building boundaries (`no_fly_zones`), Battery reserve /
  return-to-home capability, and Corrective waypoint safety.
- "Waypoint Processing" in the new architecture diagram is
  `RevisedMissionProposal.proposed_route()` (Phase 6) -- the SAME
  corrective-then-remaining concatenation Phase 6 already defined and
  rule-validates against. Not reimplemented here either.
- Geofence / building boundaries: see `app.replanning.context_builder
  .CircularGeofence` (this phase's addition to Phase 5's previously-`None`
  `SafetyConstraints.geofence` placeholder) -- a simple circle, not a
  real polygon engine. `SafetyConstraints.geofence` is the single
  allowed-flight-area circle a route must stay INSIDE;
  `SafetyConstraints.no_fly_zones` is a list of forbidden circles a route
  must stay OUTSIDE, standing in for "building boundaries" (there is no
  real building-footprint database anywhere in this project). Both
  default to unset, so by default these checks are a documented no-op --
  a caller who supplies a real `SafetyConstraints` (e.g. from a future
  phase with real geofence/building data) gets them enforced for free,
  with no change to this module.
- Battery Feasibility Check / Return-to-home capability: as of RAMR
  novelty Phase 8, this is delegated to the dedicated
  `app.replanning.battery_feasibility.BatteryFeasibilityChecker` rather
  than computed inline here (Phase 8 extracted what was originally an
  inline calculation in this file into that standalone, directly-tested
  component -- see its docstring). It, in turn, reuses the SAME
  haversine great-circle distance formula and flat per-km battery cost
  model `app.replanning.replanner` (Phase 4) already established --
  `settings.min_battery_reserve_percent` / `.battery_consumption_percent_per_km`,
  unchanged. This module asks a different question than Phase 4's
  `AdaptiveMissionReplanner` does, though: Phase 4 *constructs* a
  battery-feasible route; this module *checks* whether an
  already-proposed route (from Phase 6's LLM reasoning, or anywhere
  else) can actually be flown and still get home, given the SAME model.
- Corrective waypoint safety: two independent checks. (1)
  `recommended_action` must be internally consistent with which lists
  are populated (e.g. `INSERT_CORRECTIVE_WAYPOINTS` requires a non-empty
  `corrective_waypoints`; `SKIP_WAYPOINTS` requires a non-empty
  `skipped_waypoints`) -- catches an LLM proposal whose structured
  fields contradict its own stated action. (2) every corrective waypoint
  must be geographically GROUNDED -- within
  `_CORRECTIVE_GROUNDING_TOLERANCE_KM` of an already-known mission
  waypoint (completed or remaining) -- enforcing, independently of
  whether the LLM actually followed it, the exact grounding rule Phase
  6's system prompt already asks for ("reuse that existing waypoint's
  coordinates" -- `app/llm/prompts.py`, `REPLANNING_SYSTEM_PROMPT` rule
  4). Defense in depth: a prompt asking nicely is not a safety
  guarantee, so this module re-checks it deterministically.
- "No revised mission can reach MAVSDK without validation": enforced
  structurally two ways. First, this module never imports
  `app.mission.converter` or `app.mavlink.px4_client` -- it cannot itself
  reach MAVSDK. Second, `enforce_before_mavsdk()` below is the single,
  explicit choke point a future phase MUST call before ever handing a
  revised mission to `app.mission.converter.to_mavsdk_mission_plan()`:
  it raises `UnsafeMissionError` on anything not `APPROVED`, so
  "forgetting" to check `.approved` is not silently possible for a
  caller that uses it.
- Fallback action (only decided when REJECTED, matching your IF INVALID
  branch exactly): `CONTINUE_ORIGINAL_MISSION` if the mission's ORIGINAL
  remaining route (from `MissionState`, before this proposal) is itself
  still rule-valid and battery-feasible; else `RETURN_TO_HOME` if a
  direct RTH from the drone's CURRENT position is battery-feasible; else
  `ABORT` as the last-resort safe fallback. This is a deterministic
  decision -- no LLM involved in choosing the fallback, matching this
  project's standing rule that safety-critical actions must not depend
  solely on the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.config import settings
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionState
from app.mission.validator import ValidationResult, validate_remaining_mission
from app.replanning.adaptive_reasoner import RecommendedAction, RevisedMissionProposal
from app.replanning.battery_feasibility import BatteryFeasibilityChecker
from app.replanning.context_builder import CircularGeofence, ReplanningContext, SafetyConstraints
from app.replanning.replanner import _haversine_km

# How close a proposed corrective waypoint must be to an already-known
# mission waypoint (completed or remaining) to count as "grounded" rather
# than an invented coordinate. Deliberately generous (500 m) -- this is a
# sanity check against hallucinated locations, not a precision survey
# requirement.
_CORRECTIVE_GROUNDING_TOLERANCE_KM = 0.5

# recommended_action values that should never carry NEW corrective
# waypoints -- they represent "stop/return" outcomes, not "insert and
# continue" outcomes.
_TERMINAL_ACTIONS = {
    RecommendedAction.RETURN_TO_LAUNCH,
    RecommendedAction.LAND,
    RecommendedAction.ABORT_MISSION,
    RecommendedAction.HOLD_POSITION,
}


class SafetyValidationStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FallbackAction(str, Enum):
    """Matches your IF INVALID / Fallback action list exactly."""

    CONTINUE_ORIGINAL_MISSION = "CONTINUE_ORIGINAL_MISSION"
    RETURN_TO_HOME = "RETURN_TO_HOME"
    ABORT = "ABORT"


class UnsafeMissionError(Exception):
    """
    Raised by `enforce_before_mavsdk()` for any `SafetyValidationResult`
    that is not `APPROVED`. This is the concrete gate: "no revised
    mission can reach MAVSDK without validation" as code, not a comment.
    """


@dataclass
class BatteryFeasibility:
    """What the Battery Feasibility Check step produces."""

    sufficient_for_proposed_route: bool | None  # None = unknown (missing position/battery data)
    sufficient_for_return_to_home_now: bool | None
    proposed_route_distance_km: float | None
    return_leg_distance_km: float | None
    required_battery_percent: float | None
    available_battery_percent: float | None
    reason: str

    def as_dict(self) -> dict:
        return {
            "sufficient_for_proposed_route": self.sufficient_for_proposed_route,
            "sufficient_for_return_to_home_now": self.sufficient_for_return_to_home_now,
            "proposed_route_distance_km": self.proposed_route_distance_km,
            "return_leg_distance_km": self.return_leg_distance_km,
            "required_battery_percent": self.required_battery_percent,
            "available_battery_percent": self.available_battery_percent,
            "reason": self.reason,
        }


@dataclass
class SafetyValidationResult:
    """What `ReplanningSafetyValidator.validate()` always returns."""

    status: SafetyValidationStatus
    errors: list[str]
    warnings: list[str]
    rule_validation: ValidationResult
    battery_feasibility: BatteryFeasibility
    fallback_action: FallbackAction | None = None  # set only when REJECTED
    fallback_reason: str | None = None
    approved_mission: Mission | None = None  # set only when APPROVED and the route is non-empty

    @property
    def approved(self) -> bool:
        return self.status == SafetyValidationStatus.APPROVED

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "errors": self.errors,
            "warnings": self.warnings,
            "rule_validation": self.rule_validation.as_dict(),
            "battery_feasibility": self.battery_feasibility.as_dict(),
            "fallback_action": self.fallback_action.value if self.fallback_action else None,
            "fallback_reason": self.fallback_reason,
            "approved_mission": self.approved_mission.model_dump() if self.approved_mission else None,
        }


def enforce_before_mavsdk(result: SafetyValidationResult) -> Mission | None:
    """
    The single required choke point before ANY revised mission may be
    handed to `app.mission.converter.to_mavsdk_mission_plan()`. Returns
    `result.approved_mission` (which is `None` for a bare
    action-only proposal with no waypoint items -- e.g. plain
    RETURN_TO_LAUNCH/ABORT_MISSION -- there being nothing to convert into
    a `MissionPlan`; the caller acts on `recommended_action` directly in
    that case). Raises `UnsafeMissionError` for anything not `APPROVED`.
    """
    if not result.approved:
        raise UnsafeMissionError(
            f"Refusing to forward a {result.status.value} revised mission to MAVSDK. "
            f"Errors: {result.errors}. Fallback action: "
            f"{result.fallback_action.value if result.fallback_action else 'n/a'} "
            f"({result.fallback_reason})."
        )
    return result.approved_mission


class ReplanningSafetyValidator:
    """
    Usage:
        validator = ReplanningSafetyValidator()
        result = validator.validate(
            proposal, context, mission_state,
            home_latitude_deg=home_lat, home_longitude_deg=home_lon,
        )
        if result.approved:
            mission = enforce_before_mavsdk(result)
            # ... future phase: to_mavsdk_mission_plan(mission, ...)
        else:
            # act on result.fallback_action
    """

    def validate(
        self,
        proposal: RevisedMissionProposal,
        context: ReplanningContext,
        mission_state: MissionState,
        *,
        home_latitude_deg: float,
        home_longitude_deg: float,
    ) -> SafetyValidationResult:
        # -- Waypoint Processing (reuses Phase 6's own method) --
        route = proposal.proposed_route()

        # -- Existing Safety Validator (Phase 4/5, reused unchanged) --
        rule_result = validate_remaining_mission(route) if route else ValidationResult(valid=True)
        errors: list[str] = list(rule_result.errors)
        warnings: list[str] = list(rule_result.warnings)

        # -- Additional boundary checks not covered by validate_remaining_mission --
        self._check_speed(errors)
        self._check_geofence(route, context.safety_constraints, errors)
        self._check_no_fly_zones(route, context.safety_constraints, errors)
        self._check_corrective_waypoint_safety(proposal, context, errors)

        # -- Battery Feasibility Check (includes return-to-home capability) --
        battery_feasibility = self._check_battery_feasibility(
            route, context, home_latitude_deg, home_longitude_deg
        )
        if battery_feasibility.sufficient_for_proposed_route is False:
            errors.append(battery_feasibility.reason)

        # -- Approved / Rejected --
        if errors:
            fallback_action, fallback_reason = self._choose_fallback(
                context, mission_state, home_latitude_deg, home_longitude_deg
            )
            return SafetyValidationResult(
                status=SafetyValidationStatus.REJECTED,
                errors=errors,
                warnings=warnings,
                rule_validation=rule_result,
                battery_feasibility=battery_feasibility,
                fallback_action=fallback_action,
                fallback_reason=fallback_reason,
                approved_mission=None,
            )

        approved_mission = (
            Mission(mission_name=f"{mission_state.active_mission.mission_name} (adapted)", mission_items=route)
            if route
            else None
        )
        return SafetyValidationResult(
            status=SafetyValidationStatus.APPROVED,
            errors=[],
            warnings=warnings,
            rule_validation=rule_result,
            battery_feasibility=battery_feasibility,
            approved_mission=approved_mission,
        )

    # -- individual checks --------------------------------------------------

    @staticmethod
    def _check_speed(errors: list[str]) -> None:
        """
        No per-waypoint speed exists anywhere in this project's schema --
        every mission flies at the single configured
        `settings.mission_default_speed_ms` (see
        `app/mission/converter.py`). Check THAT configured speed against
        the configured safety ceiling.
        """
        if settings.mission_default_speed_ms > settings.max_safe_speed_ms:
            errors.append(
                f"Configured mission speed {settings.mission_default_speed_ms} m/s exceeds "
                f"the maximum safe speed {settings.max_safe_speed_ms} m/s."
            )

    @staticmethod
    def _check_geofence(items: list[MissionItem], constraints: SafetyConstraints, errors: list[str]) -> None:
        geofence = constraints.geofence
        if geofence is None:
            return
        for item in items:
            if item.latitude is None or item.longitude is None:
                continue
            distance_km = _haversine_km(
                item.latitude, item.longitude, geofence.center_latitude_deg, geofence.center_longitude_deg
            )
            if distance_km > geofence.radius_km:
                errors.append(
                    f"Item #{item.sequence} ({item.command.value}) is {distance_km:.3f} km from "
                    f"the geofence center, outside the {geofence.radius_km} km allowed geofence."
                )

    @staticmethod
    def _check_no_fly_zones(items: list[MissionItem], constraints: SafetyConstraints, errors: list[str]) -> None:
        """Stands in for "building boundaries" -- see this module's docstring."""
        for zone in constraints.no_fly_zones:
            for item in items:
                if item.latitude is None or item.longitude is None:
                    continue
                distance_km = _haversine_km(
                    item.latitude, item.longitude, zone.center_latitude_deg, zone.center_longitude_deg
                )
                if distance_km <= zone.radius_km:
                    errors.append(
                        f"Item #{item.sequence} ({item.command.value}) is {distance_km:.3f} km from "
                        f"a no-fly/building-boundary zone center, inside its {zone.radius_km} km "
                        f"exclusion radius."
                    )

    @staticmethod
    def _check_corrective_waypoint_safety(
        proposal: RevisedMissionProposal, context: ReplanningContext, errors: list[str]
    ) -> None:
        # (1) recommended_action must be consistent with which lists are populated.
        if proposal.recommended_action == RecommendedAction.INSERT_CORRECTIVE_WAYPOINTS and not proposal.corrective_waypoints:
            errors.append("recommended_action is INSERT_CORRECTIVE_WAYPOINTS but corrective_waypoints is empty.")
        if proposal.recommended_action in _TERMINAL_ACTIONS and proposal.corrective_waypoints:
            errors.append(
                f"corrective_waypoints is non-empty but recommended_action is "
                f"{proposal.recommended_action.value}, which should not introduce new waypoints."
            )
        if proposal.recommended_action == RecommendedAction.SKIP_WAYPOINTS and not proposal.skipped_waypoints:
            errors.append("recommended_action is SKIP_WAYPOINTS but skipped_waypoints is empty.")

        # (2) every corrective waypoint must be grounded near an already-known mission waypoint.
        known_points = [
            (item.latitude, item.longitude)
            for item in list(context.completed_waypoints) + list(context.remaining_waypoints)
            if item.latitude is not None and item.longitude is not None
        ]
        for item in proposal.corrective_waypoints:
            if item.latitude is None or item.longitude is None:
                continue
            if not known_points:
                errors.append(
                    f"Corrective waypoint #{item.sequence} cannot be grounded -- the mission has no "
                    f"existing waypoint coordinates to compare against."
                )
                continue
            nearest_km = min(_haversine_km(item.latitude, item.longitude, lat, lon) for lat, lon in known_points)
            if nearest_km > _CORRECTIVE_GROUNDING_TOLERANCE_KM:
                errors.append(
                    f"Corrective waypoint #{item.sequence} is {nearest_km:.3f} km from the nearest "
                    f"existing mission waypoint (tolerance {_CORRECTIVE_GROUNDING_TOLERANCE_KM} km) -- "
                    f"it must reuse/be near an already-known coordinate, not an invented one."
                )

    @staticmethod
    def _check_battery_feasibility(
        route: list[MissionItem],
        context: ReplanningContext,
        home_latitude_deg: float,
        home_longitude_deg: float,
    ) -> BatteryFeasibility:
        """
        RAMR novelty Phase 8: delegates the actual battery-feasibility
        math to the dedicated, standalone `BatteryFeasibilityChecker`
        (`app.replanning.battery_feasibility`) rather than recomputing it
        here -- this method now only adapts Phase 8's `FEASIBLE`/
        `NOT_FEASIBLE` result (computed twice: once for the full
        proposed route + RTH, once for a direct RTH from the drone's
        CURRENT position) into this module's own `BatteryFeasibility`
        shape, which the rest of `safety_validator.py` (and Phase 7's
        existing tests) already depend on.
        """
        position = context.current_drone_position
        battery_pct = context.battery_status.remaining_percent
        reserve_pct = context.battery_status.minimum_reserve_percent

        if position.latitude_deg is None or position.longitude_deg is None or battery_pct is None:
            return BatteryFeasibility(
                sufficient_for_proposed_route=None,
                sufficient_for_return_to_home_now=None,
                proposed_route_distance_km=None,
                return_leg_distance_km=None,
                required_battery_percent=None,
                available_battery_percent=battery_pct,
                reason="Drone position or battery reading unavailable -- cannot compute battery feasibility.",
            )

        checker = BatteryFeasibilityChecker()

        route_check = checker.check_from_positions(
            current_battery_percent=battery_pct,
            current_latitude_deg=position.latitude_deg,
            current_longitude_deg=position.longitude_deg,
            remaining_waypoints=route,
            corrective_waypoints=[],  # `route` already IS corrective+remaining concatenated (Phase 6)
            home_latitude_deg=home_latitude_deg,
            home_longitude_deg=home_longitude_deg,
            battery_reserve_percent=reserve_pct,
        )
        rth_now_check = checker.check_from_positions(
            current_battery_percent=battery_pct,
            current_latitude_deg=position.latitude_deg,
            current_longitude_deg=position.longitude_deg,
            remaining_waypoints=[],
            corrective_waypoints=[],
            home_latitude_deg=home_latitude_deg,
            home_longitude_deg=home_longitude_deg,
            battery_reserve_percent=reserve_pct,
        )

        return BatteryFeasibility(
            sufficient_for_proposed_route=route_check.feasible,
            sufficient_for_return_to_home_now=rth_now_check.feasible,
            proposed_route_distance_km=route_check.remaining_mission_distance_km + route_check.corrective_waypoint_distance_km,
            return_leg_distance_km=route_check.return_to_home_distance_km,
            required_battery_percent=route_check.total_required_percent - route_check.battery_reserve_percent,
            available_battery_percent=battery_pct,
            reason=route_check.reason,
        )

    def _choose_fallback(
        self,
        context: ReplanningContext,
        mission_state: MissionState,
        home_latitude_deg: float,
        home_longitude_deg: float,
    ) -> tuple[FallbackAction, str]:
        """
        Deterministic, non-LLM fallback selection -- matches your IF
        INVALID list exactly: continue the original mission if it's
        still safe; else return to home if that's still feasible; else
        abort. No LLM is consulted for this decision (this project's
        standing rule: safety-critical actions must not depend solely on
        the LLM).
        """
        original_route = list(mission_state.remaining_waypoints)
        original_rule_result = validate_remaining_mission(original_route) if original_route else ValidationResult(valid=True)
        original_battery = self._check_battery_feasibility(
            original_route, context, home_latitude_deg, home_longitude_deg
        )
        original_battery_ok = original_battery.sufficient_for_proposed_route is not False

        if original_rule_result.valid and original_battery_ok:
            return (
                FallbackAction.CONTINUE_ORIGINAL_MISSION,
                "The original (pre-adaptation) remaining mission is still rule-valid and "
                "battery-feasible -- continuing it unchanged is the safest fallback.",
            )

        rth_feasibility = self._check_battery_feasibility([], context, home_latitude_deg, home_longitude_deg)
        if rth_feasibility.sufficient_for_return_to_home_now is not False:
            return (
                FallbackAction.RETURN_TO_HOME,
                "The original mission is no longer safe or feasible, but battery is sufficient "
                "for a direct return to home from the drone's current position.",
            )

        return (
            FallbackAction.ABORT,
            "Neither continuing the original mission nor a direct return to home is battery-"
            "feasible -- abort/land immediately is the only remaining safe fallback.",
        )


def _demo_cli() -> None:
    """
    Manual test entry point -- runs a simulated adaptation through the
    full Phase 7 pipeline (no LLM/network needed -- builds a
    RevisedMissionProposal by hand):
        python -m app.replanning.safety_validator
    """
    import json

    from app.mavlink.drone_state import DroneState
    from app.mission.state import MissionStateManager
    from app.replanning.context_builder import ReplanningContextBuilder
    from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent

    mission = Mission(
        mission_name="Demo survey",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.000, longitude=2.000, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.009, longitude=2.000, altitude=30),
            MissionItem(sequence=4, command=MissionCommand.WAYPOINT, latitude=1.018, longitude=2.000, altitude=30),
            MissionItem(sequence=5, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )
    manager = MissionStateManager()
    manager.start(mission)
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=3, total=5)

    drone_state = DroneState(latitude_deg=1.009, longitude_deg=2.000, relative_altitude_m=30.0, battery_remaining_percent=0.30)
    event = ReplanningEvent(
        event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.HIGH, source=EventSource.TELEMETRY,
        mission_id=manager.snapshot().mission_id, drone_state=drone_state, mission_state=manager.snapshot(),
    )
    context = ReplanningContextBuilder().build(event, manager.snapshot(), drone_state)

    proposal = RevisedMissionProposal(
        remaining_waypoints=[
            MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1.018, longitude=2.000, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
        recommended_action=RecommendedAction.CONTINUE,
        reason="Battery sufficient to reach WP4 then return home.",
        confidence=0.8,
    )

    result = ReplanningSafetyValidator().validate(
        proposal, context, manager.snapshot(), home_latitude_deg=1.000, home_longitude_deg=2.000
    )
    print(json.dumps(result.as_dict(), indent=2, default=str))


if __name__ == "__main__":
    _demo_cli()
