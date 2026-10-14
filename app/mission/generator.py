"""
Phase 3 (schema-only) / Phase 4-5 (schema + rule-validated) -- ties RAG
retrieval, the LLM client, and the deterministic rule validator together:

    user_request -> retrieve() -> format_context() -> build_user_prompt()
                 -> LLM (schema-constrained) -> Mission (Phase 3/4)
                 -> validate_mission()        -> ValidationResult (Phase 5)

matching the pipeline diagram from the original spec:
    LLM Output -> Schema Validation -> Rule Validation -> Valid/Invalid

Two entry points are exposed:
  - `generate_mission()` -- Phase 3/4 behaviour, unchanged: RAG + LLM,
    schema-valid `Mission` only, no rule validation. Kept as-is (and
    still used by the two `@pytest.mark.live_llm` tests) so existing
    callers/tests don't need to change.
  - `generate_validated_mission()` -- the full Phase 5 pipeline. Same as
    above, but every mission is also run through
    `app.mission.validator.validate_mission()`. If validation fails, the
    rule errors are fed back to the LLM (same self-correction pattern
    `llm_client.py` already uses for schema failures) and it is asked to
    produce a corrected mission, up to `max_validation_attempts` times.
    This is deliberately a SEPARATE retry loop from the one inside
    `generate_structured()` -- that one repairs schema/JSON problems,
    this one repairs rule problems the schema can't express (see
    `validator.py`'s docstring for why those can't just be more Pydantic
    constraints).

Real-world coordinate & building resolution improvement -- both entry
points above gained ONE new, optional, additive keyword argument:
`location_context`. Every existing call site (including both
`@pytest.mark.live_llm` tests and every fake-LLM unit test in
`tests/test_phase5_generator_integration.py`) keeps working completely
unmodified because it defaults to `None`. When a caller (specifically
`app/mission/location_aware_generator.py`, the new module this
improvement adds) HAS a verified `ResolvedLocation`, it renders it into a
short text block and passes it here, where it's appended to the SAME RAG
`context` string that already gets built from `retrieve()`/
`format_context()` -- so from the LLM's point of view, "the resolved
real-world location" is just one more authoritative fact sitting
alongside the knowledge-base excerpts it already trusts (per
`SYSTEM_PROMPT` rule 10: "Use ONLY the reference context and the user's
request to decide coordinates"). Nothing about the RAG index, the LLM
client, the schema, or the rule validator changed to support this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.llm.llm_client import LLMGenerationError, get_llm_client
from app.llm.prompts import SYSTEM_PROMPT, build_user_prompt
from app.mission.schema import Mission
from app.mission.validator import ValidationResult, validate_mission
from app.rag.retriever import format_context, retrieve

log = logging.getLogger(__name__)

MAX_VALIDATION_ATTEMPTS = 2


def _with_location_context(context: str, location_context: str | None) -> str:
    """Additive helper shared by both entry points below -- see this module's docstring."""
    if not location_context:
        return context
    return (
        f"{context}\n\n---\n\nRESOLVED REAL-WORLD LOCATION (verified -- treat as authoritative, "
        f"same as the reference context above):\n{location_context}"
    )


def generate_mission(user_request: str, *, rag_k: int | None = None, location_context: str | None = None) -> Mission:
    """
    Turn a natural-language mission request into a schema-valid Mission.

    Raises `LLMGenerationError` if the LLM could not be reached, or
    could not produce a schema-valid mission after its internal retries
    (see `llm_client.MAX_ATTEMPTS`).

    NOTE: this only guarantees schema validity (Phase 3/4). It does NOT
    run Phase 5's rule validator -- use `generate_validated_mission()`
    for the full pipeline.

    `location_context` is optional (default `None`, fully backward
    compatible) -- see this module's docstring.
    """
    chunks = retrieve(user_request, k=rag_k)
    context = _with_location_context(format_context(chunks), location_context)
    log.info(
        "Retrieved %d context chunk(s) for request: %r (sources: %s)",
        len(chunks),
        user_request,
        sorted({c.source for c in chunks}),
    )

    client = get_llm_client()
    user_prompt = build_user_prompt(user_request, context)

    mission = client.generate_structured(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=Mission,
    )

    log.info(
        "Generated mission %r with %d item(s): %s",
        mission.mission_name,
        len(mission.mission_items),
        [item.command.value for item in mission.mission_items],
    )
    return mission


@dataclass
class MissionGenerationResult:
    """Everything the full pipeline (schema + rule validation) produces."""

    mission: Mission
    validation: ValidationResult
    attempts: int

    @property
    def accepted(self) -> bool:
        return self.validation.valid


def generate_validated_mission(
    user_request: str,
    *,
    rag_k: int | None = None,
    max_validation_attempts: int = MAX_VALIDATION_ATTEMPTS,
    location_context: str | None = None,
) -> MissionGenerationResult:
    """
    Full Phase 3-5 pipeline: RAG -> LLM (schema-constrained) -> rule
    validation, with rule-violation feedback fed back to the LLM for
    up to `max_validation_attempts` tries.

    Always returns a `MissionGenerationResult` -- it does NOT raise on a
    rule-invalid mission (that is a normal, expected outcome the caller
    must check via `.accepted` / `.validation.valid`). It still raises
    `LLMGenerationError` if the LLM itself is unreachable or can't
    produce a schema-valid response at all (a problem no amount of rule
    feedback can fix).

    `location_context` is optional (default `None`, fully backward
    compatible) -- see this module's docstring.
    """
    chunks = retrieve(user_request, k=rag_k)
    context = _with_location_context(format_context(chunks), location_context)
    log.info(
        "Retrieved %d context chunk(s) for request: %r (sources: %s)",
        len(chunks),
        user_request,
        sorted({c.source for c in chunks}),
    )

    client = get_llm_client()
    user_prompt = build_user_prompt(user_request, context)

    mission: Mission | None = None
    result: ValidationResult | None = None

    for attempt in range(1, max_validation_attempts + 1):
        mission = client.generate_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=Mission,
        )
        result = validate_mission(mission)

        log.info(
            "Attempt %d/%d: mission %r, %d item(s), valid=%s, errors=%s, warnings=%s",
            attempt,
            max_validation_attempts,
            mission.mission_name,
            len(mission.mission_items),
            result.valid,
            result.errors,
            result.warnings,
        )

        if result.valid:
            break

        if attempt < max_validation_attempts:
            # Same self-correction idea as llm_client.py's schema-retry
            # loop, one level up: tell the model exactly which rules it
            # broke and ask for a corrected mission, using the SAME
            # RAG-grounded user_prompt as the base (not a growing chat
            # history) since each attempt is an independent
            # generate_structured() call.
            user_prompt = (
                f"{user_prompt}\n\n"
                "Your previous answer violated the following mission rules:\n"
                + "\n".join(f"- {e}" for e in result.errors)
                + "\nReturn a corrected mission that fixes every one of these issues."
            )

    assert mission is not None and result is not None  # loop always runs >=1 time
    return MissionGenerationResult(mission=mission, validation=result, attempts=attempt)


def _demo_cli() -> None:
    """
    Manual test entry point -- runs the FULL Phase 3-5 pipeline (schema
    generation + rule validation):
        python -m app.mission.generator "Take off, fly to two waypoints at 50 meters, and return home."
    Needs the configured LLM provider reachable: Groq needs GROQ_API_KEY
    in .env + internet, Ollama needs `ollama serve` running and the
    configured model pulled -- see the README's Phase 3 section.
    """
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    request = " ".join(sys.argv[1:]) or (
        "Take off, fly to two waypoints at 50 meters, and return home."
    )
    print(f"Request: {request}\n")

    try:
        result = generate_validated_mission(request)
    except LLMGenerationError as exc:
        print(f"\nLLM generation failed: {exc}")
        raise SystemExit(1)

    print(result.mission.model_dump_json(indent=2))
    print(f"\nValidation (after {result.attempts} attempt(s)):")
    print(json.dumps(result.validation.as_dict(), indent=2))
    if not result.accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    _demo_cli()
