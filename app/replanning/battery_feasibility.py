"""
RAMR novelty Phase 8 -- Battery Feasibility for Replanning.

    OBJECTIVE: Ensure the drone can safely complete the revised mission.
    CREATE OR EXTEND: BatteryFeasibilityChecker
    INPUT: Current battery, current drone position, remaining mission
    distance, corrective waypoint distance, return-to-home distance,
    battery reserve.
    OUTPUT: FEASIBLE or NOT_FEASIBLE.
    IMPORTANT RULE: The system must calculate battery feasibility for
    Revised Mission + Return To Home.
    Example: Current Battery = 30%, Required Remaining Mission = 12%,
    Required RTH = 10%, Reserve = 5% -> Total Required = 27% -> FEASIBLE.
    If Total Required > Available Battery -> NOT_FEASIBLE.
    SUCCESS CRITERIA: The system rejects mission adaptations that leave
    insufficient battery for safe return.

Design notes:

- "CREATE OR EXTEND": Phase 7's `app.replanning.safety_validator` already
  had an inline, private battery-feasibility calculation
  (`_check_battery_feasibility()`) built from the SAME model this module
  formalizes. Rather than leave two independent implementations of the
  same math to drift apart, this phase EXTRACTS that logic into this
  dedicated, standalone, directly-testable `BatteryFeasibilityChecker`
  -- and Phase 7's `safety_validator.py` is updated (see that file) to
  call it instead of recomputing the same sums itself. Nothing about
  `safety_validator.py`'s PUBLIC result shape (`BatteryFeasibility`,
  `SafetyValidationResult`) changed -- Phase 7's own tests all still
  pass unmodified -- only its internals now delegate here.
- The literal INPUT list is "distance", not "required percent" -- so the
  primary `check()` method takes three DISTANCES in km (remaining
  mission, corrective waypoint, return-to-home) plus the current battery
  percent and the battery reserve percent, and does the
  distance-to-percent conversion itself using the SAME flat per-km model
  `app.replanning.replanner` (Phase 4) already established
  (`settings.battery_consumption_percent_per_km`) -- not a new,
  competing energy model. `battery_reserve_percent` stays a flat
  percentage (not a distance), matching how `settings
  .min_battery_reserve_percent` is already used everywhere else in this
  project (Phase 4, Phase 7): a floor, not a distance-based cost.
- "current drone position" (also listed as an INPUT) is used by the
  convenience method `check_from_positions()`, which walks actual
  `MissionItem` coordinates (via the SAME haversine great-circle formula
  `app.replanning.replanner` uses, imported directly rather than
  duplicated) to derive the three distances `check()` needs, in the SAME
  order Phase 6/7 already established for a proposed route: corrective
  waypoints first (flown before the remaining route), then the
  remaining mission, then the leg home from wherever that route ends.
  This is the natural real-world entry point (Phase 7's safety
  validator uses it); `check()` itself stays a pure, dependency-free
  function of numbers, easy to unit-test directly against the spec's
  own worked example.
- "The system must calculate battery feasibility for Revised Mission +
  Return To Home" -- literally what `total_required_percent` sums:
  remaining-mission + corrective-waypoint + return-to-home + reserve,
  compared against `current_battery_percent`. Corrective waypoint
  distance is its own term (not folded into "remaining mission") because
  the spec lists it separately, and because a proposal can have
  corrective waypoints with no remaining waypoints at all (or vice
  versa) -- keeping them separate makes the breakdown legible in
  `BatteryFeasibilityResult.as_dict()` rather than an opaque single sum.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.config import settings
from app.mission.schema import MissionItem
from app.replanning.replanner import _haversine_km


class BatteryFeasibilityStatus(str, Enum):
    FEASIBLE = "FEASIBLE"
    NOT_FEASIBLE = "NOT_FEASIBLE"


@dataclass
class BatteryFeasibilityResult:
    """What `BatteryFeasibilityChecker.check()` / `.check_from_positions()` always return."""

    status: BatteryFeasibilityStatus
    current_battery_percent: float
    remaining_mission_distance_km: float
    corrective_waypoint_distance_km: float
    return_to_home_distance_km: float
    remaining_mission_required_percent: float
    corrective_waypoint_required_percent: float
    return_to_home_required_percent: float
    battery_reserve_percent: float
    total_required_percent: float
    reason: str

    @property
    def feasible(self) -> bool:
        return self.status == BatteryFeasibilityStatus.FEASIBLE

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "current_battery_percent": self.current_battery_percent,
            "remaining_mission_distance_km": self.remaining_mission_distance_km,
            "corrective_waypoint_distance_km": self.corrective_waypoint_distance_km,
            "return_to_home_distance_km": self.return_to_home_distance_km,
            "remaining_mission_required_percent": self.remaining_mission_required_percent,
            "corrective_waypoint_required_percent": self.corrective_waypoint_required_percent,
            "return_to_home_required_percent": self.return_to_home_required_percent,
            "battery_reserve_percent": self.battery_reserve_percent,
            "total_required_percent": self.total_required_percent,
            "reason": self.reason,
        }


class BatteryFeasibilityChecker:
    """
    Usage (matching the spec's own worked example):
        checker = BatteryFeasibilityChecker()
        result = checker.check(
            current_battery_percent=0.30,
            remaining_mission_distance_km=6.0,   # 6.0 km * 0.02 %/km = 12%
            corrective_waypoint_distance_km=0.0,
            return_to_home_distance_km=5.0,      # 5.0 km * 0.02 %/km = 10%
            battery_reserve_percent=0.05,
        )
        assert result.status == BatteryFeasibilityStatus.FEASIBLE  # 12% + 10% + 5% = 27% <= 30%

    Or, from real mission/drone data:
        result = checker.check_from_positions(
            current_battery_percent=drone_state.battery_remaining_percent,
            current_latitude_deg=drone_state.latitude_deg,
            current_longitude_deg=drone_state.longitude_deg,
            remaining_waypoints=proposal.remaining_waypoints,
            corrective_waypoints=proposal.corrective_waypoints,
            home_latitude_deg=home_lat, home_longitude_deg=home_lon,
        )
    """

    def check(
        self,
        *,
        current_battery_percent: float,
        remaining_mission_distance_km: float,
        corrective_waypoint_distance_km: float,
        return_to_home_distance_km: float,
        battery_reserve_percent: float | None = None,
        consumption_rate_percent_per_km: float | None = None,
    ) -> BatteryFeasibilityResult:
        """
        Pure percent math -- no MissionItem/DroneState dependency. This is
        the function to test directly against the spec's worked example.
        """
        reserve = battery_reserve_percent if battery_reserve_percent is not None else settings.min_battery_reserve_percent
        rate = consumption_rate_percent_per_km if consumption_rate_percent_per_km is not None else settings.battery_consumption_percent_per_km

        remaining_required = remaining_mission_distance_km * rate
        corrective_required = corrective_waypoint_distance_km * rate
        rth_required = return_to_home_distance_km * rate
        total_required = remaining_required + corrective_required + rth_required + reserve

        status = (
            BatteryFeasibilityStatus.FEASIBLE
            if total_required <= current_battery_percent
            else BatteryFeasibilityStatus.NOT_FEASIBLE
        )

        if status == BatteryFeasibilityStatus.FEASIBLE:
            reason = (
                f"Total required battery {total_required:.4f} "
                f"(remaining mission {remaining_required:.4f} + corrective {corrective_required:.4f} + "
                f"RTH {rth_required:.4f} + reserve {reserve:.4f}) is within available battery "
                f"{current_battery_percent:.4f} -- FEASIBLE."
            )
        else:
            reason = (
                f"Total required battery {total_required:.4f} "
                f"(remaining mission {remaining_required:.4f} + corrective {corrective_required:.4f} + "
                f"RTH {rth_required:.4f} + reserve {reserve:.4f}) exceeds available battery "
                f"{current_battery_percent:.4f} -- NOT_FEASIBLE."
            )

        return BatteryFeasibilityResult(
            status=status,
            current_battery_percent=current_battery_percent,
            remaining_mission_distance_km=remaining_mission_distance_km,
            corrective_waypoint_distance_km=corrective_waypoint_distance_km,
            return_to_home_distance_km=return_to_home_distance_km,
            remaining_mission_required_percent=remaining_required,
            corrective_waypoint_required_percent=corrective_required,
            return_to_home_required_percent=rth_required,
            battery_reserve_percent=reserve,
            total_required_percent=total_required,
            reason=reason,
        )

    def check_from_positions(
        self,
        *,
        current_battery_percent: float,
        current_latitude_deg: float,
        current_longitude_deg: float,
        remaining_waypoints: list[MissionItem],
        corrective_waypoints: list[MissionItem],
        home_latitude_deg: float,
        home_longitude_deg: float,
        battery_reserve_percent: float | None = None,
        consumption_rate_percent_per_km: float | None = None,
    ) -> BatteryFeasibilityResult:
        """
        Derives the three distances `check()` needs from real coordinates,
        walking corrective waypoints first, then the remaining mission,
        then the leg home from wherever that route ends -- the same
        order `RevisedMissionProposal.proposed_route()` (Phase 6) already
        concatenates a proposal's route in.
        """
        position = (current_latitude_deg, current_longitude_deg)

        corrective_distance_km, position = self._walk_distance(position, corrective_waypoints)
        remaining_distance_km, position = self._walk_distance(position, remaining_waypoints)
        return_to_home_distance_km = _haversine_km(position[0], position[1], home_latitude_deg, home_longitude_deg)

        return self.check(
            current_battery_percent=current_battery_percent,
            remaining_mission_distance_km=remaining_distance_km,
            corrective_waypoint_distance_km=corrective_distance_km,
            return_to_home_distance_km=return_to_home_distance_km,
            battery_reserve_percent=battery_reserve_percent,
            consumption_rate_percent_per_km=consumption_rate_percent_per_km,
        )

    @staticmethod
    def _walk_distance(
        start: tuple[float, float], waypoints: list[MissionItem]
    ) -> tuple[float, tuple[float, float]]:
        """
        Sums great-circle distance across `waypoints` that carry
        coordinates (WAYPOINT/LOITER -- TAKEOFF/LAND/RETURN_TO_LAUNCH
        don't per `app/mission/schema.py`), starting from `start`.
        Returns (distance_km, last_known_position) -- `start` itself if
        no waypoint carried coordinates, so callers can chain calls.
        """
        distance_km = 0.0
        position = start
        for item in waypoints:
            if item.latitude is None or item.longitude is None:
                continue
            distance_km += _haversine_km(position[0], position[1], item.latitude, item.longitude)
            position = (item.latitude, item.longitude)
        return distance_km, position


def _demo_cli() -> None:
    """
    Manual test entry point -- reproduces the spec's own worked example
    exactly:
        python -m app.replanning.battery_feasibility
    """
    import json

    checker = BatteryFeasibilityChecker()
    result = checker.check(
        current_battery_percent=0.30,
        remaining_mission_distance_km=6.0,  # 6.0 km * 0.02 %/km = 12%
        corrective_waypoint_distance_km=0.0,
        return_to_home_distance_km=5.0,  # 5.0 km * 0.02 %/km = 10%
        battery_reserve_percent=0.05,
    )
    print(json.dumps(result.as_dict(), indent=2))
    assert result.status == BatteryFeasibilityStatus.FEASIBLE

    # Same shape, but battery too low -- must come back NOT_FEASIBLE.
    unsafe_result = checker.check(
        current_battery_percent=0.20,
        remaining_mission_distance_km=6.0,
        corrective_waypoint_distance_km=0.0,
        return_to_home_distance_km=5.0,
        battery_reserve_percent=0.05,
    )
    print(json.dumps(unsafe_result.as_dict(), indent=2))
    assert unsafe_result.status == BatteryFeasibilityStatus.NOT_FEASIBLE


if __name__ == "__main__":
    _demo_cli()
