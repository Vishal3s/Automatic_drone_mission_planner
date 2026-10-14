"""
Phase 5 tests, part 2 -- run with:
    pytest tests/test_phase5_generator_integration.py -v

These exercise `generate_validated_mission()`'s retry-on-rule-violation
loop WITHOUT calling a real LLM or the real RAG index: `retrieve()` and
`get_llm_client()` are monkeypatched with fakes. This keeps the test fast
and dependency-free (no network, no sentence-transformers/Chroma, no
Groq/Ollama), while still proving the orchestration logic in
`app/mission/generator.py` -- not `app/mission/validator.py`, which
already has its own dedicated tests in `test_phase5_validation.py`.
"""

from __future__ import annotations

import pytest

from app.mission.schema import Mission, MissionCommand, MissionItem


def _valid_mission() -> Mission:
    return Mission(
        mission_name="Valid",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=50),
            MissionItem(sequence=2, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


def _invalid_mission() -> Mission:
    # Doesn't start with TAKEOFF -- a rule violation, not a schema one.
    return Mission(
        mission_name="Invalid",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.WAYPOINT, latitude=1, longitude=1, altitude=50),
            MissionItem(sequence=2, command=MissionCommand.LAND),
        ],
    )


class _FakeClient:
    """Returns each mission in `responses` in order, one per call."""

    def __init__(self, responses: list[Mission]):
        self._responses = list(responses)
        self.call_count = 0

    def generate_structured(self, **kwargs):
        mission = self._responses[self.call_count]
        self.call_count += 1
        return mission


@pytest.fixture(autouse=True)
def _stub_rag(monkeypatch):
    # Avoid loading a real embedding model / Chroma index for these tests.
    monkeypatch.setattr("app.mission.generator.retrieve", lambda *a, **k: [])
    monkeypatch.setattr("app.mission.generator.format_context", lambda *a, **k: "")


def test_valid_mission_on_first_try_returns_immediately(monkeypatch):
    from app.mission.generator import generate_validated_mission

    fake = _FakeClient([_valid_mission()])
    monkeypatch.setattr("app.mission.generator.get_llm_client", lambda: fake)

    result = generate_validated_mission("irrelevant request")

    assert result.accepted is True
    assert result.attempts == 1
    assert fake.call_count == 1


def test_invalid_then_valid_retries_once_and_succeeds(monkeypatch):
    from app.mission.generator import generate_validated_mission

    fake = _FakeClient([_invalid_mission(), _valid_mission()])
    monkeypatch.setattr("app.mission.generator.get_llm_client", lambda: fake)

    result = generate_validated_mission("irrelevant request", max_validation_attempts=2)

    assert result.accepted is True
    assert result.attempts == 2
    assert fake.call_count == 2


def test_still_invalid_after_all_attempts_returns_result_without_raising(monkeypatch):
    from app.mission.generator import generate_validated_mission

    fake = _FakeClient([_invalid_mission(), _invalid_mission()])
    monkeypatch.setattr("app.mission.generator.get_llm_client", lambda: fake)

    result = generate_validated_mission("irrelevant request", max_validation_attempts=2)

    assert result.accepted is False
    assert result.attempts == 2
    assert len(result.validation.errors) > 0
    assert fake.call_count == 2


def test_default_max_validation_attempts_is_used_when_not_specified(monkeypatch):
    from app.mission.generator import MAX_VALIDATION_ATTEMPTS, generate_validated_mission

    fake = _FakeClient([_invalid_mission()] * MAX_VALIDATION_ATTEMPTS)
    monkeypatch.setattr("app.mission.generator.get_llm_client", lambda: fake)

    result = generate_validated_mission("irrelevant request")

    assert result.attempts == MAX_VALIDATION_ATTEMPTS
    assert fake.call_count == MAX_VALIDATION_ATTEMPTS
