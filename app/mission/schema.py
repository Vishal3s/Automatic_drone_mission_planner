"""
Phase 3 (minimal) / Phase 4 (hardened) -- the mission data model.

This file has to exist earlier than the phase plan originally scoped it,
for a concrete reason: Ollama's structured-outputs feature (`format=` on
`chat()`) needs an actual JSON schema to constrain the LLM's generation
against, and the natural source of that schema is a Pydantic model of
the mission shape. Building that model is literally what Phase 4 asked
for ("Use Pydantic to define a strict mission schema... reject malformed
JSON or invalid schema structures"), so rather than write a throwaway
ad-hoc schema in the LLM layer and a "real" one later, this Phase 3 work
defines it here, in its rightful location in the architecture, and Phase
4 will hardening-pass it (this docstring will be updated then).

What THIS version already enforces (via Pydantic, and therefore also via
Ollama's schema-constrained generation, so the LLM cannot violate these
even before Phase 5's rule validator ever runs):
  - `command` may ONLY be one of the five allowed commands -- the LLM
    cannot invent a new command name or use a real MAVLink command this
    project hasn't enabled.
  - No extra/unexpected fields are accepted (`extra="forbid"`).
  - Numeric fields are the right type and within their configured range
    (latitude/longitude in valid degree ranges; altitude within
    MIN_ALTITUDE_M/MAX_ALTITUDE_M from `.env`).
  - A mission has at least one item and no more than MAX_WAYPOINTS.

What this version deliberately does NOT enforce -- this is Phase 5's job,
now implemented in `app/mission/validator.py` (`validate_mission()`),
listed here so it's clear this isn't an oversight:
  - Per-command required fields (e.g. WAYPOINT needs lat/lon/altitude,
    RETURN_TO_LAUNCH needs none) -- cross-field/conditional rules like
    this are awkward to express as a JSON Schema that a local LLM can
    reliably satisfy, so they're checked deterministically in Python
    after generation instead.
  - Sequence-number ordering/uniqueness, mission start/end command
    rules (must start with TAKEOFF, must end with LAND/RETURN_TO_LAUNCH,
    nothing after a terminating command), a (0, 0) "Null Island"
    coordinate sanity warning.
  - Still NOT covered anywhere yet (left for a later phase, or for
    PX4/MAVSDK itself to enforce at upload/execution time): geofence
    radius against a real boundary, battery-reserve/endurance estimates,
    distance-based feasibility.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings


class MissionCommand(str, Enum):
    """
    The only commands this project's pipeline will ever accept, matching
    `data/documents/mavlink_commands.md`'s "included" list exactly.
    Because this is a Pydantic/JSON-Schema enum, an LLM using
    schema-constrained generation is structurally prevented from
    producing anything outside this set -- there is no prompt-only
    "please don't invent commands" reliance for this particular rule.
    """

    TAKEOFF = "TAKEOFF"
    WAYPOINT = "WAYPOINT"
    LAND = "LAND"
    RETURN_TO_LAUNCH = "RETURN_TO_LAUNCH"
    LOITER = "LOITER"


class MissionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(..., ge=1, description="1-based position of this item in the mission.")
    command: MissionCommand

    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    altitude: Optional[float] = Field(
        None,
        ge=settings.min_altitude_m,
        le=settings.max_altitude_m,
        description="Meters, relative to the home/takeoff position.",
    )
    hold_time_s: Optional[float] = Field(
        None, ge=0, description="Seconds to hold position -- only meaningful for LOITER."
    )

    # Camera orientation for building inspection -- PLANNER-ONLY: never
    # asked of the LLM (stripped from its schema by
    # app/llm/llm_client._strict_json_schema); set deterministically by
    # app/mission/inspection_camera.py from the real building outline.
    yaw_deg: Optional[float] = Field(
        None, ge=0, lt=360, description="Vehicle heading at this waypoint (0 = north, clockwise).",
        json_schema_extra={"planner_only": True},
    )
    gimbal_pitch_deg: Optional[float] = Field(
        None, ge=-90, le=30, description="Camera tilt (0 = level, -90 = straight down).",
        json_schema_extra={"planner_only": True},
    )
    take_photo: Optional[bool] = Field(
        None, description="Take a photo on reaching this waypoint.",
        json_schema_extra={"planner_only": True},
    )


class Mission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_name: str = Field(..., min_length=1, max_length=200)
    mission_items: list[MissionItem] = Field(
        ..., min_length=1, max_length=settings.max_waypoints
    )
