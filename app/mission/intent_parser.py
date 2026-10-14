"""
Real-world coordinate & building resolution improvement -- Step 1
("Natural-Language Location Extraction").

    Extend the existing mission parser so that it can identify
    location-related entities... Do NOT hard-code these exact sentences.
    Use the existing NLP/mission parsing architecture.

This project's "existing NLP/mission parsing architecture" IS its
schema-constrained LLM generation (`app/llm/llm_client.py`'s
`generate_structured()`, Phase 3) -- there has never been a separate
regex/spaCy entity extractor anywhere in this codebase, by design (see
`app/mission/schema.py`'s docstring on why per-field structure is
enforced via a Pydantic-derived JSON schema rather than ad-hoc parsing).
`parse_mission_intent()` below extends that SAME architecture with a
second schema (`MissionIntent`) and prompt pair
(`INTENT_SYSTEM_PROMPT`/`build_intent_user_prompt`, added additively to
`app/llm/prompts.py`), reusing `get_llm_client()` completely unchanged --
this is a second generation call, not a second LLM integration.

This module does NOT call the RAG retriever, the location resolver, or
`app.mission.generator` -- it only extracts intent from raw text. See
`app/mission/location_aware_generator.py` for how this, `LocationResolver`,
and the existing (unmodified) `generate_validated_mission()` are wired
together.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.llm.llm_client import get_llm_client
from app.llm.prompts import INTENT_SYSTEM_PROMPT, build_intent_user_prompt

log = logging.getLogger(__name__)


class MissionType(str, Enum):
    INSPECTION = "INSPECTION"
    SURVEY = "SURVEY"
    DELIVERY = "DELIVERY"
    PATROL = "PATROL"
    GENERIC = "GENERIC"


class ScanMode(str, Enum):
    """How a building inspection is flown. PERIMETER = one ring; STRUCTURAL = rings stacked up the facade."""

    PERIMETER = "PERIMETER"
    STRUCTURAL = "STRUCTURAL"
    ROAD = "ROAD"


_STRUCTURAL_RE = re.compile(r"\bstructural\b|\bstructure\s+scan\b", re.IGNORECASE)


_ROAD_RE = re.compile(
    r"\b(road|path|route|corridor)\s+scan\b|\bscan\s+(the\s+|a\s+)?(roads?|paths?|routes?|corridors?)\b",
    re.IGNORECASE,
)


def detect_scan_mode(user_request: str) -> ScanMode | None:
    """Deterministic keyword check -- the words of the request decide the scan type, whatever the LLM returned."""
    text = user_request or ""
    if _ROAD_RE.search(text):
        return ScanMode.ROAD
    if _STRUCTURAL_RE.search(text):
        return ScanMode.STRUCTURAL
    return None


_FILE_REQUEST_RE = re.compile(
    r"\b(generate|create|make|save|export|write|give)\b.*\b(file|waypoints?)\b|\.waypoints\b",
    re.IGNORECASE,
)


def wants_waypoints_file(user_request: str) -> bool:
    """True when the request asks for the Mission Planner .waypoints file to be written (e.g. 'generate the waypoints file')."""
    return bool(_FILE_REQUEST_RE.search(user_request or ""))


class MissionIntent(BaseModel):
    """
    Schema-constrained extraction result -- same `extra="forbid"`
    discipline `app.mission.schema.Mission`/`MissionItem` already use, so
    the LLM cannot smuggle extra fields into the structured output.
    """

    model_config = ConfigDict(extra="forbid")

    mission_type: MissionType
    location_mentioned: bool
    institution: Optional[str] = None
    campus: Optional[str] = None
    building: Optional[str] = None
    altitude_m: Optional[float] = None
    # Phase 15.5: a street address / landmark / named place that is not "a
    # building inside a named campus" -- resolved as ONE geocoding lookup.
    address: Optional[str] = None
    raw_location_phrase: Optional[str] = None
    # Structural scan (app/mission/structural_scan.py). Set from the words of the request, not from the model.
    scan_mode: ScanMode = ScanMode.PERIMETER


def parse_mission_intent(user_request: str) -> MissionIntent:
    """
    Turn a natural-language mission request into a `MissionIntent`. Never
    resolves coordinates -- see `app/location/location_resolver.py` for
    that, which is given `intent.institution`/`.campus`/`.building` (and
    falls back to `.raw_location_phrase` when those are more specific
    than the resolver's own hierarchy needs) as INPUT, never the other
    way around.

    Raises `LLMGenerationError` (from `app.llm.llm_client`) if the LLM
    could not be reached or could not produce a schema-valid response --
    same failure mode as `app.mission.generator.generate_mission()`, and
    the caller should handle it the same way.
    """
    client = get_llm_client()
    intent = client.generate_structured(
        system_prompt=INTENT_SYSTEM_PROMPT,
        user_prompt=build_intent_user_prompt(user_request),
        schema=MissionIntent,
    )
    log.info(
        "Parsed mission intent for %r: type=%s location_mentioned=%s institution=%r campus=%r building=%r address=%r altitude_m=%s",
        user_request, intent.mission_type.value, intent.location_mentioned,
        intent.institution, intent.campus, intent.building, intent.address, intent.altitude_m,
    )
    # Phase 15.5 consistency: an address is ONE place -- if the model filled
    # both, the address wins only when no campus/institution was named
    # (so "AB1 of VIT Chennai" can never be turned into an address lookup).
    if intent.address and (intent.institution or intent.campus):
        intent = intent.model_copy(update={"address": None})
    elif intent.address:
        intent = intent.model_copy(update={"building": None})
    forced = detect_scan_mode(user_request)
    if forced is not None and intent.scan_mode != forced:
        intent = intent.model_copy(update={"scan_mode": forced})
    return intent


def _demo_cli() -> None:
    """
    Manual test entry point:
        python -m app.mission.intent_parser "Inspect AB1 of VIT Chennai at 30 meters."
    Needs the configured LLM provider reachable (see app/mission/generator.py's own _demo_cli docstring).
    """
    import sys

    from app.llm.llm_client import LLMGenerationError

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    request = " ".join(sys.argv[1:]) or "Inspect AB1 of VIT Chennai at 30 meters."
    print(f"Request: {request}\n")

    try:
        intent = parse_mission_intent(request)
    except LLMGenerationError as exc:
        print(f"\nIntent extraction failed: {exc}")
        raise SystemExit(1)

    print(intent.model_dump_json(indent=2))


if __name__ == "__main__":
    _demo_cli()
