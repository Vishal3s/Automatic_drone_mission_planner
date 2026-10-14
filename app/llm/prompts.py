"""
Phase 3 -- prompt construction.

RAMR novelty Phase 6 extends this file (additively -- the Phase 3 prompt
below is untouched) with a SECOND prompt pair for replanning, following
the same two-part shape:

- `SYSTEM_PROMPT` / `REPLANNING_SYSTEM_PROMPT` -- the fixed contract for
  that generation task.
- `build_user_prompt()` / `build_replanning_user_prompt()` -- combines
  RAG-retrieved context with the specific request for one generation call.

Two prompts are built here (Phase 3):

- `SYSTEM_PROMPT` -- the fixed contract: allowed commands, required
  fields per command, sequencing rules, and an explicit instruction not
  to invent data. This is a *second* layer of defense on top of the
  schema-constrained generation in `llm_client.py` -- the JSON schema
  already makes an invalid `command` value structurally impossible, but
  it cannot express "TAKEOFF must not include coordinates" or "a mission
  must end in RETURN_TO_LAUNCH or LAND", so the prompt carries those
  rules and Phase 5's rule validator checks them deterministically
  afterward regardless of whether the LLM followed the prompt correctly.
- `build_user_prompt()` -- combines the RAG-retrieved context with the
  user's natural-language request for a specific generation call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a real import cycle risk; only needed for type hints
    from app.replanning.context_builder import ReplanningContext

SYSTEM_PROMPT = """You are a drone mission planning assistant. Your ONLY job is to convert a natural-language mission request into a structured mission plan, using the reference context you are given.

Rules you MUST follow:

1. Only use these five commands, spelled exactly as shown: TAKEOFF, WAYPOINT, LAND, RETURN_TO_LAUNCH, LOITER. Never invent a different command name, and never use a raw MAVLink command name (e.g. "MAV_CMD_NAV_WAYPOINT") -- use only the short names above.
2. TAKEOFF: requires altitude (meters). Do not include latitude/longitude for TAKEOFF.
3. WAYPOINT: requires latitude, longitude, and altitude (meters).
4. LAND: latitude/longitude are optional -- include them only if the mission should land at a specific point rather than its current position.
5. RETURN_TO_LAUNCH: takes no latitude, longitude, altitude, or hold_time_s.
6. LOITER: requires latitude, longitude, altitude, and hold_time_s (seconds to hold).
7. Sequence numbers start at 1 and increase by exactly 1 for each item in order, with no gaps and no repeats.
8. A mission that starts from the ground should begin with TAKEOFF. A mission should end with RETURN_TO_LAUNCH or LAND -- never end on a bare WAYPOINT or an unlimited LOITER.
9. Altitude must be a positive number of meters, reasonable for the mission described (do not output negative altitudes).
10. Use ONLY the reference context and the user's request to decide coordinates and altitudes. If the request does not give you enough information to pick real coordinates (e.g. it names a place with no coordinates in the reference context), do not fabricate precise-looking latitude/longitude values -- keep the mission to the parts you can determine confidently (e.g. TAKEOFF and RETURN_TO_LAUNCH only) rather than inventing a location.
11. Give the mission a short, descriptive mission_name. If you had to make an assumption (e.g. no altitude was specified, so you used a default), say so briefly inside mission_name, e.g. "Two Waypoint Survey (default 50m altitude assumed)".
12. Return ONLY the structured mission object. Do not include explanations, markdown, or commentary outside the structured output.
"""


def build_user_prompt(user_request: str, rag_context: str) -> str:
    return f"""Reference context (retrieved from the drone mission knowledge base -- use it to decide valid commands, altitude limits, and sequencing rules; it is authoritative over your own assumptions):

{rag_context}

---

User's mission request:
"{user_request}"

Generate the structured mission now, following every rule in the system prompt."""


# ---------------------------------------------------------------------
# RAMR novelty Phase 6 -- RAG + LLM Adaptive Reasoning.
#
# The drone is ALREADY AIRBORNE and mid-mission here (unlike Phase 3's
# ground-launch generation above), and the LLM is being asked to REVISE a
# remaining route in response to a trigger event, not invent a mission
# from scratch. Rule 1 below is the load-bearing one: this prompt pair
# only ever produces a *proposal* (`RevisedMissionProposal`, see
# `app/replanning/adaptive_reasoner.py`) -- nothing built from this
# prompt is sent to PX4 directly.
# ---------------------------------------------------------------------

REPLANNING_SYSTEM_PROMPT = """You are the adaptive replanning reasoning component of a drone mission system. A drone is CURRENTLY AIRBORNE and mid-mission. A trigger event has occurred (e.g. a battery deviation, a missed waypoint, a mission deviation, or a computer-vision quality issue such as blurry imagery, bad exposure, or a coverage gap). Your job is to PROPOSE a revised remaining route for a human-reviewed / rule-checked downstream system to consider. You do NOT control the drone. You are not connected to the autopilot. Your output is a recommendation only, and it will be schema-validated and rule-validated before anything is ever sent to the drone.

Rules you MUST follow:

1. You are proposing a change, not commanding the drone. Never claim to have taken an action -- only ever propose one, via the structured fields described below.
2. The drone is already airborne: do NOT include a TAKEOFF item anywhere in `remaining_waypoints` or `corrective_waypoints`.
3. `remaining_waypoints` and `corrective_waypoints` use the SAME five commands as ordinary missions, spelled exactly: WAYPOINT, LAND, RETURN_TO_LAUNCH, LOITER (TAKEOFF is forbidden here -- see rule 2). Follow the same per-command field rules as ordinary mission generation: WAYPOINT needs latitude, longitude, altitude; LOITER additionally needs hold_time_s; RETURN_TO_LAUNCH takes no latitude/longitude/altitude/hold_time_s.
4. Only use waypoint coordinates that are already grounded in the remaining mission you were given or the reference context -- never invent a new, precise-looking latitude/longitude that appears nowhere in your input. If a corrective waypoint is needed near an existing one (e.g. to re-inspect a spot with bad image quality), reuse that existing waypoint's coordinates and altitude.
5. `corrective_waypoints` are new items you are proposing to INSERT before the drone continues along `remaining_waypoints` -- leave this list empty ([]) if no correction is needed. If you populate both `corrective_waypoints` and `remaining_waypoints`, number them as ONE continuous sequence: `corrective_waypoints` first, then `remaining_waypoints` continuing the same ascending count with no gaps or repeats (e.g. corrective items 1-2, then remaining items 3-5) -- they represent a single proposed route, not two independent ones. If `corrective_waypoints` is empty, number `remaining_waypoints` starting at 1.
6. `skipped_waypoints` lists the ORIGINAL sequence numbers (exactly as given to you in the remaining mission you were shown) that you are choosing to drop from the route -- do not renumber these, and do not include a sequence number here that also appears in `remaining_waypoints` or `corrective_waypoints`.
7. `recommended_action` must be exactly one of: CONTINUE, SKIP_WAYPOINTS, INSERT_CORRECTIVE_WAYPOINTS, DIVERT, RETURN_TO_LAUNCH, LAND, ABORT_MISSION, HOLD_POSITION. Pick the one that best matches your `remaining_waypoints`/`corrective_waypoints`/`skipped_waypoints` output (e.g. if you propose only a RETURN_TO_LAUNCH item and nothing else, `recommended_action` should be RETURN_TO_LAUNCH).
8. Respect the battery and safety constraints you are given -- never propose a route with more waypoints than are safe given the reported battery/reserve, and never propose an altitude outside the given min/max altitude bounds. When battery is insufficient or the situation is uncertain, prefer a conservative action (RETURN_TO_LAUNCH or LAND) over an ambitious one.
9. `confidence` is a number from 0.0 to 1.0 reflecting how confident you are in this proposal given the information available. Use a low value (below 0.5) when the trigger reason, RAG context, or historical information is sparse or ambiguous.
10. `reason` is a short, human-readable explanation of WHY you are proposing this action -- reference the specific trigger reason and any constraint that drove the decision.
11. Return ONLY the structured proposal object. Do not include explanations, markdown, or commentary outside the structured output.
"""


def build_replanning_user_prompt(context: "ReplanningContext", rag_context: str) -> str:
    """
    Assembles the full replanning prompt from a Phase 5 `ReplanningContext`
    -- every field your Phase 6 spec lists as required LLM input is
    included: trigger reason, current drone state, remaining mission,
    battery constraints, safety constraints, RAG info, and historical
    mission information. Nothing here recomputes those values -- they are
    read directly off the already-built `ReplanningContext` (Phase 5) and
    formatted for the model.
    """
    remaining_lines = "\n".join(
        f"  #{item.sequence} {item.command.value}"
        f"{f' lat={item.latitude} lon={item.longitude}' if item.latitude is not None else ''}"
        f"{f' alt={item.altitude}m' if item.altitude is not None else ''}"
        f"{f' hold={item.hold_time_s}s' if item.hold_time_s is not None else ''}"
        for item in context.remaining_waypoints
    ) or "  (no remaining waypoints)"

    completed_summary = ", ".join(
        f"#{item.sequence} {item.command.value}" for item in context.completed_waypoints
    ) or "(none completed yet)"

    battery = context.battery_status
    battery_lines = (
        f"  remaining_percent: {battery.remaining_percent}\n"
        f"  voltage_v: {battery.voltage_v}\n"
        f"  minimum_reserve_percent: {battery.minimum_reserve_percent}\n"
        f"  sufficient (above reserve): {battery.sufficient}"
    )

    safety = context.safety_constraints
    safety_lines = (
        f"  min_altitude_m: {safety.min_altitude_m}\n"
        f"  max_altitude_m: {safety.max_altitude_m}\n"
        f"  min_battery_reserve_percent: {safety.min_battery_reserve_percent}\n"
        f"  max_waypoints: {safety.max_waypoints}\n"
        f"  geofence: {safety.geofence}"
    )

    position = context.current_drone_position
    current_wp = (
        f"#{context.current_waypoint.sequence} {context.current_waypoint.command.value}"
        if context.current_waypoint
        else "(unknown)"
    )

    history_lines = (
        "\n".join(f"  - {entry}" for entry in context.relevant_history)
        if context.relevant_history
        else "  (no relevant historical mission information available)"
    )

    return f"""Reference context (retrieved from the drone mission knowledge base -- use it to ground any coordinates, altitude limits, or command rules; it is authoritative over your own assumptions):

{rag_context}

---

TRIGGER REASON:
{context.trigger_reason}

CURRENT DRONE STATE:
  position: lat={position.latitude_deg} lon={position.longitude_deg} alt={position.altitude_m}m
  current waypoint: {current_wp}
  completed waypoints so far: {completed_summary}

REMAINING MISSION (as currently planned, before your proposal):
{remaining_lines}

BATTERY CONSTRAINTS:
{battery_lines}

SAFETY CONSTRAINTS:
{safety_lines}

MISSION PRIORITY: {context.mission_priority}

RELEVANT HISTORICAL MISSION INFORMATION:
{history_lines}

---

Propose a revised remaining route now, following every rule in the system prompt. Remember: this is a proposal only -- you do not control the drone."""


# ---------------------------------------------------------------------
# Real-world coordinate & building resolution improvement -- mission
# intent / location extraction.
#
# This is the "extend the existing mission parser" step the improvement's
# spec asks for: this project's "NLP" has always been schema-constrained
# LLM generation (Phase 3's SYSTEM_PROMPT/build_user_prompt above), never
# a separate regex/spaCy layer -- so entity extraction (mission type,
# institution/campus/building, altitude) is implemented the exact same
# way, as a second, SEPARATE schema-constrained generation call (see
# `app/mission/intent_parser.py`) that runs BEFORE the location resolver
# and the full mission-generation prompt above. This prompt pair does
# not touch SYSTEM_PROMPT/build_user_prompt at all -- Phase 3-5's mission
# generation is completely unmodified and still runs afterward, exactly
# as it always has, on the SAME natural-language request string.
# ---------------------------------------------------------------------

INTENT_SYSTEM_PROMPT = """You are the mission-intent extraction component of a drone mission planning system. Your ONLY job is to read a natural-language drone mission request and extract structured information about WHAT the mission is and WHERE it should happen -- you do NOT decide coordinates, waypoints, or altitudes beyond what is explicitly stated, and you do NOT plan the mission itself.

Rules you MUST follow:

1. `mission_type` must be exactly one of: INSPECTION, SURVEY, DELIVERY, PATROL, GENERIC. Use GENERIC if the request does not clearly match one of the other four.
2. `location_mentioned` is true if the request refers to ANY real-world place by name (an institution, campus, building, block, landmark, address, or similar) that would need to be looked up to fly there -- false if the request only gives directions/offsets (e.g. "fly north 200 meters") or no location at all.
3. `institution` is the overall organization/institution name if the request names one (e.g. a university, company, or facility name), otherwise null.
4. `campus` is the specific campus/site name if the request distinguishes one (often the same as `institution` when only one is named -- that's fine, repeat it), otherwise null.
5. `building` is the specific building/block name or code if the request names one (e.g. "AB1", "the north warehouse", "Building 3"), otherwise null. Extract it exactly as written -- do not expand abbreviations or guess a full name.
6. `altitude_m` is the requested altitude in meters if one is explicitly stated, otherwise null. Convert units if given in something other than meters (e.g. "100 feet" -> approximately 30.48 -- round sensibly). Never invent an altitude that wasn't stated.
6b. `address` is used INSTEAD of institution/campus/building when the place is a street address, a landmark, a named place or a whole site that is not "a building inside a named institution/campus" -- e.g. "12 Anna Salai, Chennai", "221B Baker Street, London", "Chennai Central railway station", "Marina Beach lighthouse". Copy it as written (full address, including city if given). When `address` is set, leave `institution`, `campus` and `building` null. Use institution/campus/building (not `address`) for requests like "AB1 of VIT Chennai" or "the library at IIT Madras".
7. `raw_location_phrase` is the exact substring (or as close as reasonably possible) of the request that names the location, e.g. "AB1 of VIT Chennai" -- this is used as a fallback geocoding query, so keep it close to the original wording, not a paraphrase.
8. Do NOT resolve or invent any latitude/longitude, and do NOT invent an institution/campus/building that was not actually named in the request.
9. Return ONLY the structured intent object. Do not include explanations, markdown, or commentary outside the structured output.
"""


def build_intent_user_prompt(user_request: str) -> str:
    return f"""User's mission request:
"{user_request}"

Extract the mission intent now, following every rule in the system prompt."""
