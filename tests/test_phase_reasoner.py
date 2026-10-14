"""
RAMR novelty Phase 6 tests -- RAG + LLM Adaptive Reasoning.
Run with: pytest tests/test_phase_reasoner.py -v

Mirrors `test_phase5_generator_integration.py`'s approach: `retrieve()`/
`format_context()` and `get_llm_client()` are monkeypatched with fakes so
these tests are fast and dependency-free (no network, no
sentence-transformers/Chroma, no Groq/Ollama) while still exercising the
real orchestration logic in `app/replanning/adaptive_reasoner.py`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.mavlink.drone_state import DroneState
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager
from app.replanning.adaptive_reasoner import (
    AdaptiveReasoningEngine,
    MAX_REASONING_ATTEMPTS,
    RecommendedAction,
    RevisedMissionProposal,
)
from app.replanning.context_builder import ReplanningContextBuilder
from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent


# ---------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------


def _mission() -> Mission:
    return Mission(
        mission_name="Reasoner test mission",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=2.0, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.01, longitude=2.0, altitude=30),
            MissionItem(sequence=4, command=MissionCommand.WAYPOINT, latitude=1.02, longitude=2.0, altitude=30),
            MissionItem(sequence=5, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


def _context_at(current_sequence: int = 3, battery_remaining_percent: float = 0.10):
    manager = MissionStateManager()
    manager.start(_mission())
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=current_sequence, total=5)

    drone_state = DroneState(
        latitude_deg=1.01, longitude_deg=2.0, relative_altitude_m=30.0,
        battery_remaining_percent=battery_remaining_percent,
    )
    event = ReplanningEvent(
        event_type=EventType.BATTERY_DEVIATION,
        severity=EventSeverity.HIGH,
        source=EventSource.TELEMETRY,
        mission_id=manager.snapshot().mission_id,
        drone_state=drone_state,
        mission_state=manager.snapshot(),
        metadata={"battery_percent": battery_remaining_percent},
    )
    return ReplanningContextBuilder().build(event, manager.snapshot(), drone_state)


def _rth_only_proposal(reason: str = "Battery insufficient, returning home.") -> RevisedMissionProposal:
    return RevisedMissionProposal(
        remaining_waypoints=[MissionItem(sequence=1, command=MissionCommand.RETURN_TO_LAUNCH)],
        corrective_waypoints=[],
        skipped_waypoints=[4],
        recommended_action=RecommendedAction.RETURN_TO_LAUNCH,
        reason=reason,
        confidence=0.9,
    )


def _invalid_proposal() -> RevisedMissionProposal:
    # WAYPOINT missing coordinates -- a rule violation, not a schema one.
    return RevisedMissionProposal(
        remaining_waypoints=[
            MissionItem(sequence=1, command=MissionCommand.WAYPOINT, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
        corrective_waypoints=[],
        skipped_waypoints=[],
        recommended_action=RecommendedAction.CONTINUE,
        reason="Continue to remaining waypoints.",
        confidence=0.6,
    )


class _FakeClient:
    """Returns each proposal in `responses` in order, one per call."""

    def __init__(self, responses: list[RevisedMissionProposal]):
        self._responses = list(responses)
        self.call_count = 0
        self.last_kwargs: dict | None = None

    def generate_structured(self, **kwargs):
        self.last_kwargs = kwargs
        proposal = self._responses[self.call_count]
        self.call_count += 1
        return proposal


@pytest.fixture(autouse=True)
def _stub_rag(monkeypatch):
    monkeypatch.setattr("app.replanning.adaptive_reasoner.retrieve", lambda *a, **k: [])
    monkeypatch.setattr("app.replanning.adaptive_reasoner.format_context", lambda *a, **k: "")


# ---------------------------------------------------------------------
# RevisedMissionProposal schema
# ---------------------------------------------------------------------


def test_proposal_defaults_have_empty_waypoint_lists():
    proposal = RevisedMissionProposal(
        recommended_action=RecommendedAction.CONTINUE, reason="No change needed.", confidence=0.8,
    )
    assert proposal.remaining_waypoints == []
    assert proposal.corrective_waypoints == []
    assert proposal.skipped_waypoints == []


def test_confidence_out_of_range_is_rejected():
    with pytest.raises(ValidationError):
        RevisedMissionProposal(recommended_action=RecommendedAction.CONTINUE, reason="x", confidence=1.5)
    with pytest.raises(ValidationError):
        RevisedMissionProposal(recommended_action=RecommendedAction.CONTINUE, reason="x", confidence=-0.1)


def test_unknown_recommended_action_is_rejected():
    with pytest.raises(ValidationError):
        RevisedMissionProposal(recommended_action="TELEPORT", reason="x", confidence=0.5)


def test_proposed_route_concatenates_corrective_then_remaining():
    proposal = RevisedMissionProposal(
        corrective_waypoints=[MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1, longitude=1, altitude=30)],
        remaining_waypoints=[MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH)],
        recommended_action=RecommendedAction.INSERT_CORRECTIVE_WAYPOINTS,
        reason="Re-inspect a low-quality area before returning.",
        confidence=0.7,
    )
    route = proposal.proposed_route()
    assert [i.sequence for i in route] == [1, 2]
    assert route[0].command == MissionCommand.WAYPOINT
    assert route[1].command == MissionCommand.RETURN_TO_LAUNCH


# ---------------------------------------------------------------------
# AdaptiveReasoningEngine.propose() -- orchestration
# ---------------------------------------------------------------------


def test_valid_proposal_on_first_try_returns_immediately(monkeypatch):
    context = _context_at()
    fake = _FakeClient([_rth_only_proposal()])
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake)

    result = AdaptiveReasoningEngine().propose(context)

    assert result.accepted is True
    assert result.attempts == 1
    assert fake.call_count == 1
    assert result.proposal.recommended_action == RecommendedAction.RETURN_TO_LAUNCH
    assert result.context is context


def test_invalid_then_valid_retries_once_and_succeeds(monkeypatch):
    context = _context_at()
    fake = _FakeClient([_invalid_proposal(), _rth_only_proposal()])
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake)

    result = AdaptiveReasoningEngine().propose(context, max_attempts=2)

    assert result.accepted is True
    assert result.attempts == 2
    assert fake.call_count == 2
    # The second call's user_prompt should carry the validation feedback.
    assert "violated the following rules" in fake.last_kwargs["user_prompt"]


def test_still_invalid_after_all_attempts_returns_result_without_raising(monkeypatch):
    context = _context_at()
    fake = _FakeClient([_invalid_proposal(), _invalid_proposal()])
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake)

    result = AdaptiveReasoningEngine().propose(context, max_attempts=2)

    assert result.accepted is False
    assert result.attempts == 2
    assert len(result.validation.errors) > 0
    assert fake.call_count == 2


def test_default_max_attempts_is_used_when_not_specified(monkeypatch):
    context = _context_at()
    fake = _FakeClient([_invalid_proposal()] * MAX_REASONING_ATTEMPTS)
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake)

    result = AdaptiveReasoningEngine().propose(context)

    assert result.attempts == MAX_REASONING_ATTEMPTS
    assert fake.call_count == MAX_REASONING_ATTEMPTS


def test_bare_recommendation_with_no_waypoints_is_valid_by_default(monkeypatch):
    # ABORT_MISSION with empty remaining/corrective lists -- nothing to rule-check.
    context = _context_at()
    proposal = RevisedMissionProposal(
        recommended_action=RecommendedAction.ABORT_MISSION,
        reason="Critical battery failure -- aborting entirely.",
        confidence=0.95,
    )
    fake = _FakeClient([proposal])
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake)

    result = AdaptiveReasoningEngine().propose(context)

    assert result.accepted is True
    assert result.validation.errors == []


def test_propose_passes_schema_and_prompts_to_llm_client(monkeypatch):
    context = _context_at()
    fake = _FakeClient([_rth_only_proposal()])
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake)

    AdaptiveReasoningEngine().propose(context)

    assert fake.last_kwargs["schema"] is RevisedMissionProposal
    assert "system_prompt" in fake.last_kwargs
    assert "user_prompt" in fake.last_kwargs


def test_retrieval_query_reflects_trigger_and_remaining_count(monkeypatch):
    captured = {}

    def _fake_retrieve(query, k=None):
        captured["query"] = query
        return []

    monkeypatch.setattr("app.replanning.adaptive_reasoner.retrieve", _fake_retrieve)

    context = _context_at()
    fake = _FakeClient([_rth_only_proposal()])
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake)

    AdaptiveReasoningEngine().propose(context)

    assert "BATTERY_DEVIATION" in captured["query"]
    assert str(len(context.remaining_waypoints)) in captured["query"]


# ---------------------------------------------------------------------
# Prompt construction -- app/llm/prompts.py additions
# ---------------------------------------------------------------------


def test_replanning_user_prompt_includes_every_required_input():
    from app.llm.prompts import build_replanning_user_prompt

    context = _context_at()
    prompt = build_replanning_user_prompt(context, rag_context="[Source: test.md]\nsome retrieved fact")

    assert context.trigger_reason in prompt
    assert "some retrieved fact" in prompt
    assert "BATTERY CONSTRAINTS" in prompt
    assert "SAFETY CONSTRAINTS" in prompt
    assert "REMAINING MISSION" in prompt
    assert "MISSION PRIORITY" in prompt
    assert "RELEVANT HISTORICAL MISSION INFORMATION" in prompt


def test_replanning_system_prompt_forbids_direct_drone_control():
    from app.llm.prompts import REPLANNING_SYSTEM_PROMPT

    assert "do NOT control the drone" in REPLANNING_SYSTEM_PROMPT or "do not control the drone" in REPLANNING_SYSTEM_PROMPT.lower()
    assert "TAKEOFF" in REPLANNING_SYSTEM_PROMPT  # forbidden-in-remaining-mission rule is documented


# ---------------------------------------------------------------------
# Full chain -- matches the spec's SUCCESS CRITERIA and TEST
# (a simulated BATTERY_DEVIATION event produces a valid revised proposal)
# ---------------------------------------------------------------------


def test_full_chain_battery_deviation_produces_valid_structured_proposal(monkeypatch):
    context = _context_at(current_sequence=3, battery_remaining_percent=0.05)
    assert context.trigger_event.event_type == EventType.BATTERY_DEVIATION
    assert context.battery_status.sufficient is False

    fake = _FakeClient([_rth_only_proposal(reason="Battery below reserve; returning to launch.")])
    monkeypatch.setattr("app.replanning.adaptive_reasoner.get_llm_client", lambda: fake)

    result = AdaptiveReasoningEngine().propose(context)

    assert result.accepted is True
    assert isinstance(result.proposal, RevisedMissionProposal)
    assert result.proposal.recommended_action == RecommendedAction.RETURN_TO_LAUNCH
    assert 0.0 <= result.proposal.confidence <= 1.0
    assert result.proposal.reason
    assert result.as_dict()["accepted"] is True
