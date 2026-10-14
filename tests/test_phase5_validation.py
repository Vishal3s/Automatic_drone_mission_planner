"""
Phase 5 tests -- run with:  pytest tests/test_phase5_validation.py -v

All pure Pydantic + dataclass logic, no LLM/RAG/network involved -- these
run anywhere, fast. They exercise `app/mission/validator.py` directly by
constructing `Mission` objects by hand (bypassing the LLM entirely), which
is the point: `validate_mission()` must behave identically no matter
where the `Mission` came from.
"""

import pytest
from pydantic import ValidationError

from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.validator import validate_mission, validate_remaining_mission


def _mission(*items: MissionItem, name: str = "Test Mission") -> Mission:
    return Mission(mission_name=name, mission_items=list(items))


# ---------------------------------------------------------------------
# Valid missions
# ---------------------------------------------------------------------


def test_simple_valid_mission_passes_with_no_errors_or_warnings():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=12.97, longitude=77.59, altitude=50),
        MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
    )
    result = validate_mission(mission)
    assert result.valid is True
    assert result.errors == []
    assert result.warnings == []


def test_mission_ending_in_land_is_valid():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
        MissionItem(sequence=2, command=MissionCommand.LAND),
    )
    assert validate_mission(mission).valid is True


def test_loiter_with_all_required_fields_is_valid():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=40),
        MissionItem(
            sequence=2, command=MissionCommand.LOITER,
            latitude=10.0, longitude=20.0, altitude=40, hold_time_s=30,
        ),
        MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
    )
    assert validate_mission(mission).valid is True


# ---------------------------------------------------------------------
# Sequencing errors
# ---------------------------------------------------------------------


def test_duplicate_sequence_numbers_are_rejected():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=1, command=MissionCommand.LAND),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("Duplicate sequence" in e for e in result.errors)


def test_out_of_order_sequence_numbers_are_rejected():
    mission = _mission(
        MissionItem(sequence=2, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=1, command=MissionCommand.LAND),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("ascending sequence order" in e for e in result.errors)


# ---------------------------------------------------------------------
# Per-command required/forbidden fields
# ---------------------------------------------------------------------


def test_waypoint_missing_coordinates_is_rejected():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.WAYPOINT, altitude=50),
        MissionItem(sequence=3, command=MissionCommand.LAND),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("missing required field" in e and "#2" in e for e in result.errors)


def test_takeoff_with_forbidden_latitude_is_rejected():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50, latitude=1.0),
        MissionItem(sequence=2, command=MissionCommand.LAND),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("should not set field" in e and "#1" in e for e in result.errors)


def test_land_with_forbidden_altitude_is_rejected():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.LAND, altitude=10),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("should not set field" in e and "#2" in e for e in result.errors)


def test_loiter_missing_hold_time_is_rejected():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.LOITER, latitude=1, longitude=1, altitude=50),
        MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("hold_time_s" in e for e in result.errors)


# ---------------------------------------------------------------------
# Mission-level ordering rules
# ---------------------------------------------------------------------


def test_mission_not_starting_with_takeoff_is_rejected():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1, longitude=1, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.LAND),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("must start with TAKEOFF" in e for e in result.errors)


def test_mission_with_two_takeoffs_is_rejected():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.TAKEOFF, altitude=60),
        MissionItem(sequence=3, command=MissionCommand.LAND),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("only one is allowed" in e for e in result.errors)


def test_mission_not_ending_in_terminator_is_rejected():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1, longitude=1, altitude=50),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("must end with RETURN_TO_LAUNCH or LAND" in e for e in result.errors)


def test_command_after_terminating_command_is_rejected():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.LAND),
        MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1, longitude=1, altitude=50),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert any("follows a terminating command" in e for e in result.errors)


# ---------------------------------------------------------------------
# Warnings (non-fatal)
# ---------------------------------------------------------------------


def test_null_island_coordinates_produce_a_warning_not_an_error():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=0, longitude=0, altitude=50),
        MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
    )
    result = validate_mission(mission)
    assert result.valid is True
    assert result.errors == []
    assert any("Null Island" in w for w in result.warnings)


# ---------------------------------------------------------------------
# ValidationResult shape
# ---------------------------------------------------------------------


def test_as_dict_matches_the_spec_shape():
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.LAND),
    )
    result_dict = validate_mission(mission).as_dict()
    assert set(result_dict.keys()) == {"valid", "errors", "warnings"}
    assert result_dict["valid"] is True
    assert result_dict["errors"] == []


def test_multiple_errors_are_all_collected_not_just_the_first():
    # Two TAKEOFFs *and* doesn't end in a terminator *and* a WAYPOINT
    # missing coordinates -- all three should show up together.
    mission = _mission(
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
        MissionItem(sequence=2, command=MissionCommand.TAKEOFF, altitude=60),
        MissionItem(sequence=3, command=MissionCommand.WAYPOINT, altitude=50),
    )
    result = validate_mission(mission)
    assert result.valid is False
    assert len(result.errors) >= 3


# ---------------------------------------------------------------------
# validate_remaining_mission() -- RAMR novelty Phase 4 addition.
# Same rule set as validate_mission() EXCEPT no TAKEOFF-start requirement
# (a remaining/mid-flight mission starts with the drone already airborne).
# ---------------------------------------------------------------------


def test_remaining_mission_without_takeoff_is_valid():
    items = [
        MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=2.0, altitude=30),
        MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH),
    ]
    result = validate_remaining_mission(items)
    assert result.valid is True
    assert result.errors == []


def test_remaining_mission_with_a_takeoff_item_is_rejected():
    items = [
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
        MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH),
    ]
    result = validate_remaining_mission(items)
    assert result.valid is False
    assert any("TAKEOFF" in e for e in result.errors)


def test_remaining_mission_still_requires_a_terminating_command():
    items = [MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=2.0, altitude=30)]
    result = validate_remaining_mission(items)
    assert result.valid is False
    assert any("must end with" in e for e in result.errors)


def test_remaining_mission_reuses_field_requirement_checks():
    # A WAYPOINT missing coordinates should still be caught, exactly like validate_mission().
    items = [
        MissionItem(sequence=1, command=MissionCommand.WAYPOINT, altitude=30),
        MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH),
    ]
    result = validate_remaining_mission(items)
    assert result.valid is False
    assert any("missing required" in e for e in result.errors)


def test_remaining_mission_of_just_rth_is_valid():
    items = [MissionItem(sequence=1, command=MissionCommand.RETURN_TO_LAUNCH)]
    result = validate_remaining_mission(items)
    assert result.valid is True
