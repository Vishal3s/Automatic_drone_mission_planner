"""
Phase 5 -- deterministic mission rule validation.

This is a SEPARATE layer from `app/mission/schema.py`'s Pydantic model.
The schema already guarantees a `Mission` object is *structurally* sound
(only the 5 allowed commands, correct types, numeric fields in range) --
but a structurally valid mission can still be a nonsensical or unsafe
one: a mission that never takes off, one with two TAKEOFFs, one that
keeps issuing commands after landing, or a WAYPOINT with no coordinates.
None of that is expressible (or at least not reliably enforceable on a
small LLM) as a JSON Schema, so this module re-checks the *already
schema-valid* `Mission` object against rules that are independent of the
LLM entirely -- this function's output does not change based on which
LLM provider produced the mission, and it would produce the exact same
result given a mission a human typed in by hand.

Two severities:
  - `errors`   -- the mission is unsafe/invalid and MUST NOT be sent to
                  PX4. `valid` is False whenever `errors` is non-empty.
  - `warnings` -- suspicious but not fatal (e.g. a (0, 0) "Null Island"
                  coordinate, which is almost always a placeholder/bug
                  rather than an intentional destination). These do not
                  affect `valid`.

Deliberately NOT checked here (documented so it's clear this is a scope
decision, not an oversight) -- these need information this function
doesn't have (a real geofence polygon, a battery model, a takeoff
location to measure distance from) and are left for a later phase or
left to PX4/MAVSDK itself to enforce at upload/execution time:
  - Geofence radius against a real boundary.
  - Battery-reserve / endurance estimates.
  - Distance-based feasibility (e.g. waypoint too far to reach).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.mission.schema import Mission, MissionCommand, MissionItem

# Commands that end a mission -- nothing may follow one of these.
_TERMINATING_COMMANDS = {MissionCommand.RETURN_TO_LAUNCH, MissionCommand.LAND}

# Per-command field requirements. A field listed in "required" must be
# present (not None) on that command; a field listed in "forbidden" must
# be absent (None) -- e.g. TAKEOFF has no business carrying a hold_time_s.
_REQUIRED_FIELDS: dict[MissionCommand, tuple[str, ...]] = {
    MissionCommand.TAKEOFF: ("altitude",),
    MissionCommand.WAYPOINT: ("latitude", "longitude", "altitude"),
    MissionCommand.LAND: (),
    MissionCommand.RETURN_TO_LAUNCH: (),
    MissionCommand.LOITER: ("latitude", "longitude", "altitude", "hold_time_s"),
}

_FORBIDDEN_FIELDS: dict[MissionCommand, tuple[str, ...]] = {
    MissionCommand.TAKEOFF: ("latitude", "longitude", "hold_time_s"),
    MissionCommand.WAYPOINT: ("hold_time_s",),
    MissionCommand.LAND: ("latitude", "longitude", "altitude", "hold_time_s"),
    MissionCommand.RETURN_TO_LAUNCH: ("latitude", "longitude", "altitude", "hold_time_s"),
    MissionCommand.LOITER: (),
}


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


def _check_sequence_numbers(items: list[MissionItem], errors: list[str]) -> None:
    seen = set()
    for item in items:
        if item.sequence in seen:
            errors.append(f"Duplicate sequence number: {item.sequence}.")
        seen.add(item.sequence)

    sequences = [item.sequence for item in items]
    if sequences != sorted(sequences):
        errors.append(
            f"Mission items are not in ascending sequence order: {sequences}."
        )


def _check_field_requirements(items: list[MissionItem], errors: list[str]) -> None:
    for item in items:
        required = _REQUIRED_FIELDS.get(item.command, ())
        forbidden = _FORBIDDEN_FIELDS.get(item.command, ())

        missing = [f for f in required if getattr(item, f) is None]
        if missing:
            errors.append(
                f"Item #{item.sequence} ({item.command.value}) is missing required "
                f"field(s): {', '.join(missing)}."
            )

        present = [f for f in forbidden if getattr(item, f) is not None]
        if present:
            errors.append(
                f"Item #{item.sequence} ({item.command.value}) should not set "
                f"field(s): {', '.join(present)}."
            )


def _check_mission_ordering(items: list[MissionItem], errors: list[str]) -> None:
    if not items:
        # Pydantic's min_length=1 already prevents this, but keep the
        # validator safe to call on its own without relying on that.
        errors.append("Mission has no items.")
        return

    if items[0].command != MissionCommand.TAKEOFF:
        errors.append(
            f"Mission must start with TAKEOFF, but the first item "
            f"(#{items[0].sequence}) is {items[0].command.value}."
        )

    takeoff_count = sum(1 for item in items if item.command == MissionCommand.TAKEOFF)
    if takeoff_count > 1:
        errors.append(f"Mission contains {takeoff_count} TAKEOFF items; only one is allowed.")

    if items[-1].command not in _TERMINATING_COMMANDS:
        errors.append(
            f"Mission must end with RETURN_TO_LAUNCH or LAND, but the last item "
            f"(#{items[-1].sequence}) is {items[-1].command.value}."
        )

    for earlier, later in zip(items, items[1:]):
        if earlier.command in _TERMINATING_COMMANDS:
            errors.append(
                f"Item #{later.sequence} ({later.command.value}) follows a terminating "
                f"command at #{earlier.sequence} ({earlier.command.value}); nothing may "
                f"come after RETURN_TO_LAUNCH or LAND."
            )


def _check_null_island(items: list[MissionItem], warnings: list[str]) -> None:
    for item in items:
        if item.latitude == 0 and item.longitude == 0:
            warnings.append(
                f"Item #{item.sequence} ({item.command.value}) has coordinates (0, 0) "
                f'("Null Island") -- this is almost always a placeholder rather than a '
                f"real destination. Double-check this waypoint."
            )


def validate_mission(mission: Mission) -> ValidationResult:
    """
    Run every deterministic rule check against an already schema-valid
    `Mission`. Independent of the LLM -- same result for any Mission
    object with the same content, regardless of how it was produced.
    """
    errors: list[str] = []
    warnings: list[str] = []
    items = mission.mission_items

    _check_sequence_numbers(items, errors)
    _check_field_requirements(items, errors)
    _check_mission_ordering(items, errors)
    _check_null_island(items, warnings)

    return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)


def _check_remaining_mission_ordering(items: list[MissionItem], errors: list[str]) -> None:
    """
    Same as `_check_mission_ordering()` EXCEPT it does not require the
    mission to start with TAKEOFF -- see `validate_remaining_mission()`
    below for why.
    """
    if not items:
        errors.append("Remaining mission has no items.")
        return

    takeoff_count = sum(1 for item in items if item.command == MissionCommand.TAKEOFF)
    if takeoff_count > 0:
        errors.append(
            f"Remaining/mid-flight mission should not contain a TAKEOFF item "
            f"(found {takeoff_count}) -- the drone is already airborne."
        )

    if items[-1].command not in _TERMINATING_COMMANDS:
        errors.append(
            f"Remaining mission must end with RETURN_TO_LAUNCH or LAND, but the last "
            f"item (#{items[-1].sequence}) is {items[-1].command.value}."
        )

    for earlier, later in zip(items, items[1:]):
        if earlier.command in _TERMINATING_COMMANDS:
            errors.append(
                f"Item #{later.sequence} ({later.command.value}) follows a terminating "
                f"command at #{earlier.sequence} ({earlier.command.value}); nothing may "
                f"come after RETURN_TO_LAUNCH or LAND."
            )


def validate_remaining_mission(items: list[MissionItem]) -> ValidationResult:
    """
    RAMR novelty Phase 4 addition -- validates a REMAINING/mid-flight
    sub-mission (e.g. produced by `app.replanning.replanner
    .AdaptiveMissionReplanner`), which by definition does not start with
    TAKEOFF -- the drone is already airborne when a remaining mission is
    generated. This is an ADDITIVE extension: `validate_mission()` above
    is completely unchanged and still requires TAKEOFF-first for a full,
    ground-launch mission; this function reuses the exact same
    sequence-number, per-command required/forbidden field, and Null
    Island checks, and only swaps out the ordering rule (no TAKEOFF
    requirement, but still: must end in LAND/RETURN_TO_LAUNCH, nothing
    after a terminator).

    Takes a plain `list[MissionItem]` rather than a `Mission` because a
    remaining mission's *name* (if any) isn't meaningful to validate --
    callers that already have a `Mission` object can pass
    `mission.mission_items`.
    """
    errors: list[str] = []
    warnings: list[str] = []

    _check_sequence_numbers(items, errors)
    _check_field_requirements(items, errors)
    _check_remaining_mission_ordering(items, errors)
    _check_null_island(items, warnings)

    return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)
