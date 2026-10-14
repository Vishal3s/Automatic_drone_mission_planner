"""
RAMR novelty Phase 6 -- RAG + LLM Adaptive Reasoning.

    OBJECTIVE: Extend the existing RAG and LLM system to support
    replanning. DO NOT CREATE A SEPARATE RAG SYSTEM.
    REUSE: RAG pipeline, vector database, LLM client, mission planning
    schema.
    NEW FLOW: Trigger Event -> ReplanningContext -> Relevant RAG
    Retrieval -> LLM -> Structured Revised Mission Proposal.
    IMPORTANT: The LLM must receive trigger reason, current drone state,
    remaining mission, battery constraints, safety constraints, relevant
    RAG information, and historical mission information. THE LLM OUTPUT
    MUST BE STRUCTURED (RevisedMissionProposal: remaining waypoints,
    corrective waypoints, skipped waypoints, recommended action, reason,
    confidence). The LLM must NOT directly control the drone.
    SUCCESS CRITERIA: A replanning event produces a structured mission
    modification proposal.

Design notes:

- "DO NOT CREATE A SEPARATE RAG SYSTEM" / REUSE list, followed literally:
    * RAG pipeline / vector database -- `app.rag.retriever.retrieve()`
      and `.format_context()` (Phase 2), called exactly as
      `app/mission/generator.py` already calls them for ground-launch
      generation. No second retriever, no second Chroma collection, no
      new embedding call path.
    * LLM client -- `app.llm.llm_client.get_llm_client()` /
      `generate_structured()` (Phase 3), the SAME abstraction used by
      `generator.py`. Swapping `LLM_PROVIDER` in `.env` changes both
      mission generation AND this module identically, for free.
    * Mission planning schema -- proposed waypoints are `MissionItem`
      (Phase 3/4's Pydantic model, `app/mission/schema.py`), completely
      unchanged. `RevisedMissionProposal` below is a NEW Pydantic model
      (this phase's actual deliverable per your OUTPUT list), but it is
      built FROM `MissionItem`, not a competing waypoint representation.
- The NEW FLOW is implemented literally: `propose()` takes the trigger
  `ReplanningEvent` (Phase 3) and the already-built `ReplanningContext`
  (Phase 5, which itself was built from `MissionState` (Phase 1) +
  `DroneState` (Phase 2)) -- nothing here re-derives context that Phase 5
  already assembled. It runs ONE retrieval call keyed off the trigger
  context, formats it exactly like `generator.py` does, builds the
  replanning prompt (`app/llm/prompts.py`, extended additively this
  phase), and calls the shared LLM client for a schema-constrained
  `RevisedMissionProposal`.
- "THE LLM OUTPUT MUST BE STRUCTURED": guaranteed the same way Phase 3/4/5
  already guarantee it -- `generate_structured()`'s own schema-constrained
  decoding + internal JSON/schema retry loop (unchanged, reused as-is),
  PLUS a second, independent retry loop here (mirroring
  `generator.py: generate_validated_mission()`'s rule-violation retry
  loop) that runs the proposed waypoints through the existing Phase 4/5
  rule validator (`app.mission.validator.validate_remaining_mission()`)
  and feeds any violation back to the LLM for a corrected proposal. This
  is DEFENSE IN DEPTH, not new policy: the same "schema validation, then
  rule validation, with feedback" pipeline shape as ordinary mission
  generation, applied to a replanning proposal instead of a fresh
  mission.
- "The LLM must NOT directly control the drone": enforced structurally --
  this module never imports `app.mavlink.px4_client` (or anything else
  that talks to PX4/MAVSDK), and `propose()`'s return value is a
  `ReasoningResult` wrapping a *proposal* object, not a mission upload
  call. Nothing in this file can move the drone. A future phase that
  decides to ACT on an accepted proposal is a separate, explicit step
  the caller must take (e.g. handing `result.proposal` to something that
  merges it into a `MissionState` and re-validates before ever reaching
  `px4_client.py`) -- this module's job ends at "produced a structured
  proposal."
- Sequencing convention (documented here AND enforced in the system
  prompt in `app/llm/prompts.py`): when a proposal has BOTH
  `corrective_waypoints` and `remaining_waypoints`, they are numbered as
  ONE continuous route (`corrective_waypoints` first, then
  `remaining_waypoints` continuing the count) -- because a downstream
  consumer would fly them in exactly that order. Rule validation below
  therefore validates the CONCATENATION `corrective_waypoints +
  remaining_waypoints` via `validate_remaining_mission()` (reusing the
  RAMR novelty Phase 4 addition, which already tolerates a remaining/
  mid-flight route not starting with TAKEOFF) whenever that concatenation
  is non-empty. `skipped_waypoints` are int sequence numbers referencing
  the ORIGINAL remaining mission the LLM was shown, not new items, so
  they are not part of what gets rule-validated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.llm.llm_client import LLMGenerationError, get_llm_client
from app.llm.prompts import REPLANNING_SYSTEM_PROMPT, build_replanning_user_prompt
from app.mission.schema import MissionItem
from app.mission.validator import ValidationResult, validate_remaining_mission
from app.rag.retriever import format_context, retrieve
from app.replanning.context_builder import ReplanningContext

log = logging.getLogger(__name__)

MAX_REASONING_ATTEMPTS = 2


class RecommendedAction(str, Enum):
    """
    The set of high-level actions the reasoning LLM may recommend. Kept
    as a closed, schema-constrained enum for the same reason
    `MissionCommand` is (`app/mission/schema.py`): the LLM is
    structurally prevented from inventing an action name a downstream
    consumer wouldn't recognize.
    """

    CONTINUE = "CONTINUE"
    SKIP_WAYPOINTS = "SKIP_WAYPOINTS"
    INSERT_CORRECTIVE_WAYPOINTS = "INSERT_CORRECTIVE_WAYPOINTS"
    DIVERT = "DIVERT"
    RETURN_TO_LAUNCH = "RETURN_TO_LAUNCH"
    LAND = "LAND"
    ABORT_MISSION = "ABORT_MISSION"
    HOLD_POSITION = "HOLD_POSITION"


class RevisedMissionProposal(BaseModel):
    """
    The Phase 6 OUTPUT, verbatim: remaining waypoints, corrective
    waypoints, skipped waypoints, recommended action, reason, confidence.

    A Pydantic `BaseModel` (not a dataclass, unlike most other structures
    this project added in Phases 4-5) because it is what
    `llm_client.generate_structured()` schema-constrains the LLM's
    output against -- exactly how `Mission` (Phase 3) works. This is a
    PROPOSAL object only; nothing reads it as a command, and no
    downstream code auto-applies it to the drone (see this module's
    docstring).
    """

    model_config = ConfigDict(extra="forbid")

    remaining_waypoints: list[MissionItem] = Field(
        default_factory=list,
        description="The proposed continuation route after any corrective waypoints, renumbered as one continuous sequence.",
    )
    corrective_waypoints: list[MissionItem] = Field(
        default_factory=list,
        description="New waypoints proposed for insertion before remaining_waypoints (e.g. to re-inspect a low-quality area). Empty if no correction is needed.",
    )
    skipped_waypoints: list[int] = Field(
        default_factory=list,
        description="Original sequence numbers (from the remaining mission shown to the LLM) proposed to be dropped.",
    )
    recommended_action: RecommendedAction
    reason: str = Field(..., min_length=1, max_length=1000)
    confidence: float = Field(..., ge=0.0, le=1.0)

    def proposed_route(self) -> list[MissionItem]:
        """The single continuous route this proposal represents, if accepted as-is."""
        return list(self.corrective_waypoints) + list(self.remaining_waypoints)


@dataclass
class ReasoningResult:
    """Everything `AdaptiveReasoningEngine.propose()` returns."""

    proposal: RevisedMissionProposal
    validation: ValidationResult
    attempts: int
    context: ReplanningContext

    @property
    def accepted(self) -> bool:
        return self.validation.valid

    def as_dict(self) -> dict:
        return {
            "proposal": self.proposal.model_dump(),
            "validation": self.validation.as_dict(),
            "attempts": self.attempts,
            "accepted": self.accepted,
        }


def _validate_proposal(proposal: RevisedMissionProposal) -> ValidationResult:
    """
    Rule-validates the CONCATENATION of corrective + remaining waypoints
    (the single continuous route a downstream consumer would fly) via the
    existing `validate_remaining_mission()` (RAMR novelty Phase 4). An
    empty proposed route (e.g. a bare RETURN_TO_LAUNCH/LAND/ABORT_MISSION
    recommendation with no explicit waypoint items) is valid by
    definition -- there is nothing to rule-check.
    """
    route = proposal.proposed_route()
    if not route:
        return ValidationResult(valid=True)
    return validate_remaining_mission(route)


def _build_retrieval_query(context: ReplanningContext) -> str:
    """
    A short natural-language query built from the trigger + current
    situation, used to fetch relevant chunks from the SAME RAG index
    Phase 2/3 already built -- no new document set, no new query shape
    beyond what `retrieve()` already accepts (a plain string).
    """
    current = context.current_waypoint
    current_desc = f"near waypoint {current.command.value}" if current else "position unknown"
    return (
        f"{context.trigger_reason}. Drone is {current_desc} with "
        f"{len(context.remaining_waypoints)} waypoint(s) remaining. "
        f"Mission priority: {context.mission_priority}."
    )


class AdaptiveReasoningEngine:
    """
    Usage:
        engine = AdaptiveReasoningEngine()
        result = engine.propose(event, context)  # context from Phase 5's ReplanningContextBuilder
        if result.accepted:
            ...  # a future phase decides what, if anything, to do with result.proposal
    """

    def propose(
        self,
        context: ReplanningContext,
        *,
        rag_k: int | None = None,
        max_attempts: int = MAX_REASONING_ATTEMPTS,
    ) -> ReasoningResult:
        """
        Runs the NEW FLOW from your spec: ReplanningContext -> relevant
        RAG retrieval -> LLM -> structured RevisedMissionProposal.

        Always returns a `ReasoningResult` -- it does NOT raise on a
        rule-invalid proposal (that's a normal, expected outcome the
        caller must check via `.accepted`), matching
        `generate_validated_mission()`'s contract. It still raises
        `LLMGenerationError` if the LLM itself is unreachable or cannot
        produce a schema-valid response at all.
        """
        query = _build_retrieval_query(context)
        chunks = retrieve(query, k=rag_k)
        rag_context = format_context(chunks)
        log.info(
            "Retrieved %d context chunk(s) for replanning event %s (sources: %s)",
            len(chunks),
            context.trigger_event.event_id,
            sorted({c.source for c in chunks}),
        )

        client = get_llm_client()
        user_prompt = build_replanning_user_prompt(context, rag_context)

        proposal: RevisedMissionProposal | None = None
        result: ValidationResult | None = None

        for attempt in range(1, max_attempts + 1):
            proposal = client.generate_structured(
                system_prompt=REPLANNING_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema=RevisedMissionProposal,
            )
            result = _validate_proposal(proposal)

            log.info(
                "Attempt %d/%d: recommended_action=%s, confidence=%.2f, valid=%s, errors=%s",
                attempt,
                max_attempts,
                proposal.recommended_action.value,
                proposal.confidence,
                result.valid,
                result.errors,
            )

            if result.valid:
                break

            if attempt < max_attempts:
                # Same self-correction pattern as generator.py's
                # rule-violation retry loop: tell the model exactly which
                # rules its proposed route broke and ask for a corrected
                # proposal, from the same RAG-grounded base prompt.
                user_prompt = (
                    f"{user_prompt}\n\n"
                    "Your previous proposal violated the following rules:\n"
                    + "\n".join(f"- {e}" for e in result.errors)
                    + "\nReturn a corrected proposal that fixes every one of these issues."
                )

        assert proposal is not None and result is not None  # loop always runs >= 1 time
        return ReasoningResult(proposal=proposal, validation=result, attempts=attempt, context=context)


def _demo_cli() -> None:
    """
    Manual test entry point -- runs the FULL Phase 6 pipeline (RAG +
    LLM adaptive reasoning) against a simulated BATTERY_DEVIATION event,
    exactly like your TEST instruction:
        python -m app.replanning.adaptive_reasoner
    Needs the configured LLM provider reachable -- see the README's
    Phase 3 section for Groq/Ollama setup. Uses the real RAG index too
    (build it first with `python -m app.rag.retriever --build` if you
    haven't already).
    """
    import json
    import logging as _logging

    from app.mavlink.drone_state import DroneState
    from app.mission.schema import Mission, MissionCommand
    from app.mission.state import MissionStateManager
    from app.replanning.context_builder import ReplanningContextBuilder
    from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent

    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s: %(message)s")

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

    drone_state = DroneState(
        latitude_deg=1.009, longitude_deg=2.000, relative_altitude_m=30.0,
        battery_remaining_percent=0.10, flight_mode="MISSION", is_armed=True,
    )
    event = ReplanningEvent(
        event_type=EventType.BATTERY_DEVIATION,
        severity=EventSeverity.HIGH,
        source=EventSource.TELEMETRY,
        mission_id=manager.snapshot().mission_id,
        drone_state=drone_state,
        mission_state=manager.snapshot(),
        metadata={"battery_percent": 0.10, "min_reserve_percent": 0.15},
    )

    context = ReplanningContextBuilder().build(event, manager.snapshot(), drone_state)

    try:
        result = AdaptiveReasoningEngine().propose(context)
    except LLMGenerationError as exc:
        print(f"\nLLM reasoning failed: {exc}")
        raise SystemExit(1)

    print(result.proposal.model_dump_json(indent=2))
    print(f"\nValidation (after {result.attempts} attempt(s)):")
    print(json.dumps(result.validation.as_dict(), indent=2))
    if not result.accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    _demo_cli()
