# Mission Safety Constraints

Safety-focused rules layered on top of the structural sequencing rules in
`waypoints_and_sequencing.md`. These exist specifically to catch missions
that are *structurally* valid (correct sequence numbers, correct fields)
but operationally unsafe.

## Coordinate sanity relative to a mission's stated location

When a mission request names a specific place (e.g. "inspect the
Mechanical Block"), every waypoint's latitude/longitude should fall
within a small radius of that location's known coordinates (looked up
via the RAG knowledge base's location/building records, not invented by
the LLM). A waypoint many kilometers from the named location is a strong
signal of an LLM coordinate hallucination and should be rejected, not
merely flagged, because executing it would send the vehicle somewhere
the operator never asked for.

## Battery reserve

A mission should be evaluated against an estimated flight time budget:
approximate distance/altitude changes across all waypoints, converted to
an estimated flight duration, should leave a configured reserve (this
project's design targets at least a 20-25% battery margin, consistent
with common industry guidance for multirotor operations) beyond what the
mission is estimated to consume. This project's Phase 5 implementation
starts with a simplified duration/distance estimate rather than a full
aerodynamic model — the goal in this phase is to catch obviously
unreasonable missions (e.g. 40 waypoints spread across kilometers on a
platform with ~20 minutes of flight time), not to certify exact
real-world endurance.

## Return-to-home / termination requirement

Every mission must have a defined, safe way for the flight to end (see
`waypoints_and_sequencing.md` for the structural rule). From a safety
perspective specifically: a mission that does not return home or land
deterministically leaves the vehicle's final disposition to PX4's own
failsafes (typically low-battery RTL) rather than to the plan the
operator approved, which this project treats as a validation failure,
not just a style issue.

## Altitude and geofence

Covered in detail in `px4_mission_requirements.md`. Restated as a safety
rule: no mission item may specify an altitude outside the configured
`MIN_ALTITUDE_M`–`MAX_ALTITUDE_M` band, and no waypoint may fall outside
the configured maximum radius from the mission's home/reference point.
Both are hard rejections, not warnings, because both represent the
vehicle leaving the operating envelope the system was configured to
allow.

## Command allowlist

Only the commands listed in `mavlink_commands.md`'s "included" section
(`TAKEOFF`, `WAYPOINT`, `LAND`, `RETURN_TO_LAUNCH`, `LOITER`) are
accepted. Any other command name the LLM outputs — whether a real
MAVLink command this project hasn't enabled yet, or an invented one — is
rejected by schema validation before rule validation even runs. This is
the project's primary defense against the LLM inventing or misusing a
flight command it does not fully understand the consequences of.

## Validation is deterministic, not LLM-judged

None of the checks above are performed by asking the LLM whether a
mission "looks safe." They are plain Python comparisons against
configured numeric limits and structural rules (Phase 5). The LLM's only
role is to propose a structured mission; whether that mission is valid
is decided entirely outside the LLM, so that validation results are
reproducible and cannot be talked out of a rule by clever prompting.
