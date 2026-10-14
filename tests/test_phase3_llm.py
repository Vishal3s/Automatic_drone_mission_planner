"""
Phase 3 tests -- run with:  pytest tests/test_phase3_llm.py -v

Two groups:

1. Schema tests -- pure Pydantic, no LLM or network involved. These run
   anywhere and prove the contract described in `app/mission/schema.py`'s
   docstring: only the 5 allowed commands, no extra fields, numeric
   fields within their configured range.
2. `@pytest.mark.live_llm` tests -- these actually call a running Ollama
   server with the configured model. They need `ollama serve` running
   and `ollama pull <LLM_MODEL>` already done (see README Phase 3). Skip
   them with `-m "not live_llm"` if Ollama isn't available where you're
   running tests.
"""

import pytest
from pydantic import ValidationError

from app.mission.schema import Mission, MissionCommand, MissionItem

# ---------------------------------------------------------------------
# Group 1: schema (no LLM, no network)
# ---------------------------------------------------------------------


def test_valid_mission_constructs():
    mission = Mission(
        mission_name="Two Waypoint Mission",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
            MissionItem(
                sequence=2, command=MissionCommand.WAYPOINT,
                latitude=12.9716, longitude=77.5946, altitude=50,
            ),
            MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )
    assert len(mission.mission_items) == 3
    assert mission.mission_items[0].command == MissionCommand.TAKEOFF


def test_unknown_command_is_rejected():
    with pytest.raises(ValidationError):
        MissionItem(sequence=1, command="FLY_TO_THE_MOON", altitude=50)


def test_extra_field_is_rejected():
    with pytest.raises(ValidationError):
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50, thrust_percent=80)


def test_altitude_above_configured_max_is_rejected():
    from app.config import settings

    with pytest.raises(ValidationError):
        MissionItem(
            sequence=1, command=MissionCommand.WAYPOINT,
            latitude=0, longitude=0, altitude=settings.max_altitude_m + 1,
        )


def test_altitude_below_configured_min_is_rejected():
    from app.config import settings

    with pytest.raises(ValidationError):
        MissionItem(
            sequence=1, command=MissionCommand.WAYPOINT,
            latitude=0, longitude=0, altitude=settings.min_altitude_m - 1,
        )


@pytest.mark.parametrize("bad_latitude", [90.1, -90.1, 1000])
def test_invalid_latitude_is_rejected(bad_latitude):
    with pytest.raises(ValidationError):
        MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=bad_latitude, longitude=0, altitude=50)


@pytest.mark.parametrize("bad_longitude", [180.1, -180.1, 1000])
def test_invalid_longitude_is_rejected(bad_longitude):
    with pytest.raises(ValidationError):
        MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=0, longitude=bad_longitude, altitude=50)


def test_mission_requires_at_least_one_item():
    with pytest.raises(ValidationError):
        Mission(mission_name="Empty Mission", mission_items=[])


def test_mission_rejects_malformed_json():
    with pytest.raises(ValidationError):
        Mission.model_validate_json('{"mission_name": "broken", "mission_items": "not a list"}')


def test_json_schema_enum_contains_exactly_the_five_allowed_commands():
    schema = Mission.model_json_schema()
    command_enum = schema["$defs"]["MissionCommand"]["enum"]
    assert set(command_enum) == {"TAKEOFF", "WAYPOINT", "LAND", "RETURN_TO_LAUNCH", "LOITER"}


def test_strict_json_schema_forces_every_field_required():
    """
    Regression test for a real gotcha: OpenAI/Groq-style strict
    structured-output mode requires every property to appear in
    `"required"`, even nullable ones -- plain `Mission.model_json_schema()`
    does NOT do this (Pydantic only requires fields with no default), so
    `llm_client._strict_json_schema()` has to patch it recursively,
    including inside `$defs`. This locks that behavior in.
    """
    from app.llm.llm_client import _strict_json_schema

    schema = _strict_json_schema(Mission)
    assert set(schema["required"]) == {"mission_name", "mission_items"}

    item_schema = schema["$defs"]["MissionItem"]
    assert set(item_schema["required"]) == set(item_schema["properties"].keys())


# ---------------------------------------------------------------------
# Group 2: live LLM calls (Groq by default, or Ollama if LLM_PROVIDER=ollama)
# ---------------------------------------------------------------------


@pytest.mark.live_llm
def test_generate_mission_for_a_simple_request_returns_valid_mission():
    from app.mission.generator import generate_mission

    mission = generate_mission("Take off, fly to two waypoints at 50 meters, and return home.")
    assert isinstance(mission, Mission)
    assert len(mission.mission_items) >= 2
    commands = [item.command for item in mission.mission_items]
    # The schema already guarantees every command is one of the 5 allowed
    # ones -- this just checks the LLM actually used TAKEOFF somewhere,
    # since the request explicitly asked for one.
    assert MissionCommand.TAKEOFF in commands


@pytest.mark.live_llm
def test_generate_mission_never_produces_a_disallowed_command():
    """
    Belt-and-suspenders check: even though the JSON schema passed to
    Ollama structurally forbids any command outside the enum, this test
    exercises the real end-to-end path (including JSON parsing) rather
    than only asserting against the schema in isolation.
    """
    from app.mission.generator import generate_mission

    mission = generate_mission("Survey the perimeter of the building and land when finished.")
    allowed = {c for c in MissionCommand}
    assert all(item.command in allowed for item in mission.mission_items)
