"""
RAMR novelty Phase 10 -- RAMR Integration Testing.

    OBJECTIVE: Integrate the complete telemetry-based adaptive replanning
    system.
    FINAL FLOW:
        PX4 -> Telemetry -> DroneStateManager -> TriggerEngine ->
        AdaptiveMissionReplanner -> ReplanningContextBuilder -> RAG + LLM
        -> Revised Mission Proposal -> Safety Validator -> Battery
        Checker -> MissionUpdateManager -> MAVSDK -> PX4.
    TEST SCENARIOS: (1) Battery deviation. (2) Missed waypoint.
    (3) Mission deviation. (4) Invalid replanning proposal.
    (5) Multiple simultaneous triggers. (6) Replanning while drone is
    near mission completion.
    SUCCESS CRITERIA: The complete RAMR loop works autonomously.

This module is the FINAL FLOW diagram, wired up literally, component by
component, every one of them REUSED unchanged from an earlier phase:

    Telemetry / DroneStateManager  -- app.mavlink.drone_state (Phase 2)
    TriggerEngine                  -- app.trigger.engine (Phase 3)
    AdaptiveMissionReplanner       -- app.replanning.replanner (Phase 4)
    ReplanningContextBuilder       -- app.replanning.context_builder (Phase 5)
    RAG + LLM (AdaptiveReasoningEngine) -- app.replanning.adaptive_reasoner (Phase 6)
    Safety Validator + Battery Checker  -- app.replanning.safety_validator (Phase 7)
                                           + app.replanning.battery_feasibility (Phase 8)
    MissionUpdateManager           -- app.mavlink.mission_update_manager (Phase 9)

Nothing here reimplements any of the above -- `RAMROrchestrator` only
sequences calls to them and adapts return values between the shapes each
phase already defined.

Design notes -- reading the FINAL FLOW diagram's ORDERING literally:

- The diagram places `AdaptiveMissionReplanner` (Phase 4, deterministic,
  "DO NOT USE THE LLM YET") BEFORE `ReplanningContextBuilder` -> "RAG +
  LLM" (Phase 5/6). That ordering is not an accident of the drawing: it
  matches this project's standing rule that safety-critical/expensive
  reasoning should not be the FIRST thing tried, and Phase 4's own
  docstring already frames itself as "basic ... without using LLM
  reasoning initially." So `handle_event()` below tries the deterministic
  replanner FIRST for every triggered event, and only falls through to
  building a `ReplanningContext` and invoking the LLM
  (`AdaptiveReasoningEngine.propose()`) when the deterministic replanner
  reports `UNSUPPORTED` or `UNHANDLED_EVENT_TYPE` -- i.e. it doesn't know
  how to handle this event type/data at all. A `MODIFIED` or `UNCHANGED`
  deterministic result is trusted and the LLM is never invoked for that
  event. (`ReplanningContext` is still built once a proposal needs
  Safety Validation, regardless of source -- see below -- so it is not
  skipped, only deferred until it's actually needed, matching Phase 5's
  own "do not rebuild ... unnecessarily" instruction: build once, reuse
  for both the wrapped-deterministic-proposal path and the LLM path.)
- "Revised Mission Proposal" is Phase 6's `RevisedMissionProposal`
  regardless of which engine produced it. A deterministic
  `RAMRResult.modified_mission` is therefore ADAPTED into a
  `RevisedMissionProposal` (`_proposal_from_deterministic()` below) purely
  so exactly ONE Safety-Validator code path exists downstream, matching
  "No revised mission can reach MAVSDK without validation" -- there is no
  separate, unvalidated fast path for deterministic replans.
- "Safety Validator -> Battery Checker": `ReplanningSafetyValidator.validate()`
  (Phase 7) already calls the Phase 8 `BatteryFeasibilityChecker`
  internally (see that module's docstring) -- so calling it once here
  covers both diagram boxes, exactly as those two phases already wired
  it together. Not re-implemented or duplicated here.
- "No revised mission can reach MAVSDK without validation" is enforced
  the same structural way Phase 7 established: `safety_result.approved_mission`
  (or a REJECTED result's `fallback_action`) is the only thing this module
  ever acts on -- it is never possible to reach `MissionUpdateManager.apply_update()`
  with a mission that didn't come out of `ReplanningSafetyValidator.validate()`
  as `APPROVED`.
- Two gaps Phase 7/8/9 explicitly left open (noted in their own
  docstrings and this project's own review of them) are closed HERE,
  because integrating the full loop is what surfaces them:
    1. A REJECTED safety result carries a `fallback_action`
       (CONTINUE_ORIGINAL_MISSION / RETURN_TO_HOME / ABORT) but nothing
       before this phase ever EXECUTED it. `_execute_fallback()` below
       does: CONTINUE_ORIGINAL_MISSION is a true no-op (the drone is
       already flying the original mission; nothing to send), while
       RETURN_TO_HOME and ABORT are carried out with a direct,
       deterministic MAVSDK Action-plugin call (`drone.action
       .return_to_launch()` / `drone.action.land()` -- the SAME `.action`
       plugin `app/mavlink/px4_client.py` already uses for `arm()`, not a
       new dependency). This is still NOT the LLM deciding anything --
       `_choose_fallback()` (Phase 7) already made the decision
       deterministically; this module only carries out that decision,
       consistent with the standing rule that safety-critical actions
       must not depend solely on the LLM.
    2. An APPROVED `SafetyValidationResult` with `approved_mission is None`
       (a bare action-only proposal -- e.g. a plain RETURN_TO_LAUNCH/LAND/
       ABORT_MISSION/HOLD_POSITION recommendation with no waypoint items,
       which `MissionUpdateManager.apply_update()` has no way to accept
       since it requires a non-empty `Mission`) is likewise executed
       directly via the Action plugin (`_execute_bare_action()`), rather
       than silently doing nothing or pretending `MissionUpdateManager`
       handles it. If no `drone` connection was supplied to
       `handle_event()` (e.g. a caller only wants the proposal/validation
       decision, not execution), this is reported honestly via
       `OrchestrationStage.APPROVED_ACTION_ONLY` rather than crashing or
       silently skipping.
  `drone.action.hold()` (for `HOLD_POSITION`) is part of MAVSDK's
  documented Action plugin API (puts the vehicle into HOLD flight mode)
  -- like every other MAVSDK behavior claim in this project, this is
  cited from the library's own documented surface, not assumed; it has
  NOT been exercised against real PX4 SITL in this sandbox (same
  standing limitation as Phase 9 -- see this module's "Known
  limitations" and the README).
- "Multiple simultaneous triggers": two independent, already-existing
  guards are reused (not reimplemented) so concurrent replanning
  requests are always REJECTED, never queued or interleaved:
    1. `MissionUpdateManager.is_update_in_progress(mission_state_manager)`
       (Phase 9) is checked FIRST, before this module does any work at
       all for an event -- catches a second event arriving while an
       earlier one has already reached REPLANNING/UPDATING.
    2. `MissionUpdateManager`'s own internal `asyncio.Lock` (Phase 9) is
       still the final word for a true race (two `handle_event()` calls
       both passing check #1 before either reaches `apply_update()`) --
       this is why `RAMROrchestrator` holds and reuses ONE
       `MissionUpdateManager` instance across every `handle_event()` call
       rather than constructing a fresh one per call, which would defeat
       the lock entirely.
- "Replanning while drone is near mission completion":
  `_is_near_mission_completion()` below is a new, small, deliberately
  simple rule -- your spec names this as its own test scenario but does
  not specify exact numeric criteria, so rather than invent an arbitrary
  distance/waypoint-count threshold, this checks the one thing already
  tracked and unambiguous: whether every remaining item in
  `MissionState.remaining_waypoints` is already a terminal command
  (LAND/RETURN_TO_LAUNCH) -- i.e. there is no more real waypoint work
  left to adapt. When true, `handle_event()` stops immediately with
  `NEAR_MISSION_COMPLETION` and does not invoke the replanner, LLM, or
  safety validator at all -- there is nothing left to safely replan.

Known limitations (stated plainly, not glossed over):

- Exactly like Phase 9, no PX4 SITL simulator is available in this cloud
  sandbox. `tests/test_phase_orchestrator.py` proves this module calls
  the right components in the right ORDER with the right DATA using
  fakes (mirroring Phase 9's own fake-MAVSDK test pattern) -- it does
  NOT prove real PX4 SITL behaves this way. `_demo_cli()` below is built
  so you can point it at your own PX4 SITL/MAVSDK setup for real
  verification, same as Phase 9's.
- `drone.action.hold()` for `HOLD_POSITION` and `drone.action.land()`
  used as "abort" are both cited from MAVSDK's documented Action plugin
  API, but -- like `MissionUpdateManager`'s mid-flight mission-replacement
  behavior in Phase 9 -- have not been verified against a real flight
  controller in this sandbox.
- `_proposal_from_deterministic()`'s choice of `RecommendedAction` and
  `confidence=1.0` for a deterministic proposal is this module's own
  reasonable mapping (documented inline), not something any earlier
  phase's spec defines -- `RevisedMissionProposal` was designed in Phase
  6 for LLM output, and this is its first reuse for a non-LLM source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from app.mavlink.drone_state import DroneState
from app.mavlink.mission_update_manager import MissionUpdateManager, MissionUpdateResult
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionState, MissionStateManager, MissionStatus
from app.replanning.adaptive_reasoner import AdaptiveReasoningEngine, RecommendedAction, RevisedMissionProposal
from app.replanning.context_builder import ReplanningContext, ReplanningContextBuilder, SafetyConstraints
from app.replanning.replanner import AdaptiveMissionReplanner, RAMRResult, RAMRStatus
from app.replanning.safety_validator import FallbackAction, ReplanningSafetyValidator, SafetyValidationResult
from app.trigger.engine import TriggerEngine
from app.trigger.events import ReplanningEvent

log = logging.getLogger(__name__)


class OrchestrationStage(str, Enum):
    """Every place `handle_event()` can stop, in the order it can reach them."""

    NOT_TRIGGERED = "NOT_TRIGGERED"                        # TriggerEngine decided not to activate replanning
    UPDATE_ALREADY_IN_PROGRESS = "UPDATE_ALREADY_IN_PROGRESS"  # concurrency guard #1 (Phase 9's is_update_in_progress)
    MISSION_NOT_RUNNING = "MISSION_NOT_RUNNING"             # nothing to replan -- mission isn't RUNNING
    NEAR_MISSION_COMPLETION = "NEAR_MISSION_COMPLETION"     # only terminal items remain -- nothing left to adapt
    NO_CHANGE = "NO_CHANGE"                                  # deterministic replanner: UNCHANGED
    REJECTED = "REJECTED"                                    # Safety Validator: REJECTED, fallback is a no-op or couldn't run
    FALLBACK_EXECUTED = "FALLBACK_EXECUTED"                  # Safety Validator: REJECTED, fallback action carried out
    APPROVED_ACTION_ONLY = "APPROVED_ACTION_ONLY"            # APPROVED, no waypoints, no drone supplied to execute it
    ACTION_EXECUTED = "ACTION_EXECUTED"                      # APPROVED, no waypoints, action carried out via MAVSDK
    APPLIED = "APPLIED"                                       # APPROVED, MissionUpdateManager.apply_update() succeeded
    UPDATE_FAILED = "UPDATE_FAILED"                          # APPROVED, but MissionUpdateManager.apply_update() failed


class ReplanSource(str, Enum):
    NONE = "NONE"
    DETERMINISTIC = "DETERMINISTIC"
    LLM = "LLM"


@dataclass
class OrchestrationResult:
    """What `RAMROrchestrator.handle_event()` always returns."""

    stage: OrchestrationStage
    reason: str
    event: ReplanningEvent
    replan_source: ReplanSource = ReplanSource.NONE
    context: ReplanningContext | None = None
    proposal: RevisedMissionProposal | None = None
    safety_result: SafetyValidationResult | None = None
    fallback_action: FallbackAction | None = None
    action_executed: bool | None = None  # None = n/a, True/False = whether a MAVSDK action call succeeded
    update_result: MissionUpdateResult | None = None

    @property
    def accepted(self) -> bool:
        """True for every outcome where the autonomous loop did the right, safe thing -- not just 'a mission was uploaded'."""
        return self.stage in (
            OrchestrationStage.NOT_TRIGGERED,
            OrchestrationStage.NEAR_MISSION_COMPLETION,
            OrchestrationStage.NO_CHANGE,
            OrchestrationStage.REJECTED,
            OrchestrationStage.FALLBACK_EXECUTED,
            OrchestrationStage.ACTION_EXECUTED,
            OrchestrationStage.APPLIED,
        )

    def as_dict(self) -> dict:
        return {
            "stage": self.stage.value,
            "reason": self.reason,
            "event_id": self.event.event_id,
            "event_type": self.event.event_type.value,
            "replan_source": self.replan_source.value,
            "proposal": self.proposal.model_dump() if self.proposal else None,
            "safety_result": self.safety_result.as_dict() if self.safety_result else None,
            "fallback_action": self.fallback_action.value if self.fallback_action else None,
            "action_executed": self.action_executed,
            "update_result": (
                {
                    "accepted": self.update_result.accepted,
                    "stage": self.update_result.stage,
                    "error": self.update_result.error,
                }
                if self.update_result
                else None
            ),
        }


def _is_near_mission_completion(mission_state: MissionState) -> bool:
    """See this module's docstring: true when nothing but terminal items remain."""
    non_terminal_remaining = [
        item
        for item in mission_state.remaining_waypoints
        if item.command not in (MissionCommand.LAND, MissionCommand.RETURN_TO_LAUNCH)
    ]
    return len(non_terminal_remaining) == 0


def _proposal_from_deterministic(event: ReplanningEvent, ramr_result: RAMRResult) -> RevisedMissionProposal:
    """
    Adapts a Phase 4 `RAMRResult` (status MODIFIED) into a Phase 6
    `RevisedMissionProposal`, so exactly one Safety Validator code path
    handles both deterministic and LLM-sourced proposals. See this
    module's docstring for why this mapping (not specified by any
    earlier phase) is what it is.
    """
    items = list(ramr_result.modified_mission.mission_items) if ramr_result.modified_mission else []

    if event.event_type.value == "MISSED_WAYPOINT":
        missed = event.metadata.get("missed_sequence")
        action = RecommendedAction.SKIP_WAYPOINTS
        skipped = [missed] if missed is not None else []
    elif len(items) == 1 and items[0].command == MissionCommand.RETURN_TO_LAUNCH:
        action = RecommendedAction.RETURN_TO_LAUNCH
        skipped = []
    else:
        action = RecommendedAction.CONTINUE
        skipped = []

    return RevisedMissionProposal(
        remaining_waypoints=items,
        corrective_waypoints=[],
        skipped_waypoints=skipped,
        recommended_action=action,
        reason=ramr_result.reason,
        # Deterministic, rule-based computation -- not a guess, hence 1.0
        # (distinct from an LLM's own self-reported confidence).
        confidence=1.0,
    )


class RAMROrchestrator:
    """
    Usage:
        orchestrator = RAMROrchestrator()
        result = await orchestrator.handle_event(
            event,
            mission_state_manager=state_manager,   # Phase 1, tracking the ACTIVE mission
            drone_state=drone_state,                 # Phase 2's latest DroneState snapshot
            drone=drone,                              # an already-connected mavsdk.System, or None to only decide/propose
            home_latitude_deg=home_lat, home_longitude_deg=home_lon,
        )
        if result.stage == OrchestrationStage.APPLIED:
            ...
    """

    def __init__(
        self,
        *,
        trigger_engine: TriggerEngine | None = None,
        context_builder: ReplanningContextBuilder | None = None,
        deterministic_replanner: AdaptiveMissionReplanner | None = None,
        reasoning_engine: AdaptiveReasoningEngine | None = None,
        safety_validator: ReplanningSafetyValidator | None = None,
        update_manager: MissionUpdateManager | None = None,
    ) -> None:
        self._trigger_engine = trigger_engine or TriggerEngine()
        self._context_builder = context_builder or ReplanningContextBuilder()
        self._deterministic_replanner = deterministic_replanner or AdaptiveMissionReplanner()
        self._reasoning_engine = reasoning_engine or AdaptiveReasoningEngine()
        self._safety_validator = safety_validator or ReplanningSafetyValidator()
        # ONE shared MissionUpdateManager, reused across every handle_event()
        # call -- required for its internal lock to actually serialize
        # concurrent update attempts (see this module's docstring).
        self._update_manager = update_manager or MissionUpdateManager()

    async def handle_event(
        self,
        event: ReplanningEvent,
        *,
        mission_state_manager: MissionStateManager,
        drone_state: DroneState,
        drone=None,
        home_latitude_deg: float,
        home_longitude_deg: float,
        safety_constraints: SafetyConstraints | None = None,
        mission_priority: str = "NORMAL",
        mission_history: list[dict] | None = None,
    ) -> OrchestrationResult:
        # -- Telemetry -> DroneStateManager -> TriggerEngine --
        decision = self._trigger_engine.handle_event(event)
        if not decision.should_replan:
            return OrchestrationResult(stage=OrchestrationStage.NOT_TRIGGERED, reason=decision.reason, event=event)

        # -- Concurrency guard, layer 1 (Phase 9, reused) --
        if MissionUpdateManager.is_update_in_progress(mission_state_manager):
            return OrchestrationResult(
                stage=OrchestrationStage.UPDATE_ALREADY_IN_PROGRESS,
                reason="A mission update is already REPLANNING/UPDATING for this mission -- rejecting this event rather than queuing it.",
                event=event,
            )

        snapshot = mission_state_manager.snapshot()
        if snapshot.mission_status != MissionStatus.RUNNING:
            return OrchestrationResult(
                stage=OrchestrationStage.MISSION_NOT_RUNNING,
                reason=f"mission_status is {snapshot.mission_status.value}, not RUNNING -- nothing to replan.",
                event=event,
            )

        # -- Replanning while near mission completion --
        if _is_near_mission_completion(snapshot):
            return OrchestrationResult(
                stage=OrchestrationStage.NEAR_MISSION_COMPLETION,
                reason="Only terminal (LAND/RETURN_TO_LAUNCH) items remain in the active mission -- skipping replanning this close to completion.",
                event=event,
            )

        # -- AdaptiveMissionReplanner (Phase 4, deterministic-first) --
        ramr_result = self._deterministic_replanner.replan(
            event, snapshot, drone_state,
            home_latitude_deg=home_latitude_deg, home_longitude_deg=home_longitude_deg,
        )

        if ramr_result.status == RAMRStatus.UNCHANGED:
            return OrchestrationResult(
                stage=OrchestrationStage.NO_CHANGE, reason=ramr_result.reason, event=event,
                replan_source=ReplanSource.DETERMINISTIC,
            )

        if ramr_result.status == RAMRStatus.MODIFIED:
            proposal = _proposal_from_deterministic(event, ramr_result)
            replan_source = ReplanSource.DETERMINISTIC
        else:
            # UNSUPPORTED or UNHANDLED_EVENT_TYPE -- escalate to RAG + LLM.
            context = self._context_builder.build(
                event, snapshot, drone_state,
                safety_constraints=safety_constraints, mission_priority=mission_priority,
                mission_history=mission_history,
            )
            reasoning_result = self._reasoning_engine.propose(context)
            proposal = reasoning_result.proposal
            replan_source = ReplanSource.LLM
            if not reasoning_result.accepted:
                log.warning(
                    "LLM proposal for event %s failed rule validation after %d attempt(s): %s -- "
                    "forwarding it to the Safety Validator anyway, which will independently "
                    "reject it (defense in depth: this module never assumes the reasoning engine's "
                    "own retry loop is the last word).",
                    event.event_id, reasoning_result.attempts, reasoning_result.validation.errors,
                )

        # -- ReplanningContextBuilder (only built once, reused for Safety Validation) --
        context = self._context_builder.build(
            event, snapshot, drone_state,
            safety_constraints=safety_constraints, mission_priority=mission_priority,
            mission_history=mission_history,
        )

        # -- Safety Validator -> Battery Checker (Phase 7, which already calls Phase 8 internally) --
        safety_result = self._safety_validator.validate(
            proposal, context, snapshot,
            home_latitude_deg=home_latitude_deg, home_longitude_deg=home_longitude_deg,
        )

        if not safety_result.approved:
            executed = await self._execute_fallback(safety_result.fallback_action, drone)
            stage = OrchestrationStage.FALLBACK_EXECUTED if executed else OrchestrationStage.REJECTED
            return OrchestrationResult(
                stage=stage, reason=safety_result.fallback_reason or "Revised mission proposal rejected.",
                event=event, replan_source=replan_source, context=context, proposal=proposal,
                safety_result=safety_result, fallback_action=safety_result.fallback_action,
                action_executed=executed,
            )

        # -- Approved: either a bare action, or a real mission -> MissionUpdateManager -> MAVSDK --
        if safety_result.approved_mission is None:
            if drone is None:
                return OrchestrationResult(
                    stage=OrchestrationStage.APPROVED_ACTION_ONLY,
                    reason=(
                        f"Approved recommendation is {proposal.recommended_action.value} with no waypoint "
                        f"items -- no `drone` connection was supplied to execute it, so this is being "
                        f"reported rather than silently skipped."
                    ),
                    event=event, replan_source=replan_source, context=context, proposal=proposal,
                    safety_result=safety_result,
                )
            executed = await self._execute_bare_action(proposal.recommended_action, drone)
            return OrchestrationResult(
                stage=OrchestrationStage.ACTION_EXECUTED if executed else OrchestrationStage.UPDATE_FAILED,
                reason=f"Approved bare action {proposal.recommended_action.value} "
                       f"{'executed via MAVSDK action plugin.' if executed else 'failed to execute.'}",
                event=event, replan_source=replan_source, context=context, proposal=proposal,
                safety_result=safety_result, action_executed=executed,
            )

        if drone is None:
            return OrchestrationResult(
                stage=OrchestrationStage.APPROVED_ACTION_ONLY,
                reason="A revised mission was approved, but no `drone` connection was supplied to upload it.",
                event=event, replan_source=replan_source, context=context, proposal=proposal,
                safety_result=safety_result,
            )

        update_result = await self._update_manager.apply_update(
            drone=drone,
            mission_state_manager=mission_state_manager,
            approved_mission=safety_result.approved_mission,
            home_latitude_deg=home_latitude_deg,
            home_longitude_deg=home_longitude_deg,
        )
        return OrchestrationResult(
            stage=OrchestrationStage.APPLIED if update_result.accepted else OrchestrationStage.UPDATE_FAILED,
            reason=update_result.error or "Revised mission applied and resumed.",
            event=event, replan_source=replan_source, context=context, proposal=proposal,
            safety_result=safety_result, update_result=update_result,
        )

    # -----------------------------------------------------------------
    # Deterministic action execution -- see this module's docstring
    # -----------------------------------------------------------------

    @staticmethod
    async def _execute_fallback(fallback_action: FallbackAction | None, drone) -> bool | None:
        """
        Carries out a Safety Validator fallback decision (Phase 7). Never
        consults the LLM -- the decision was already made deterministically
        by `_choose_fallback()`; this only performs it.
        Returns True/False if a MAVSDK action was attempted, None if the
        fallback is a true no-op (CONTINUE_ORIGINAL_MISSION) or no `drone`
        was supplied to act on RETURN_TO_HOME/ABORT.
        """
        if fallback_action == FallbackAction.CONTINUE_ORIGINAL_MISSION or fallback_action is None:
            return None
        if drone is None:
            return None
        try:
            if fallback_action == FallbackAction.RETURN_TO_HOME:
                await drone.action.return_to_launch()
            elif fallback_action == FallbackAction.ABORT:
                await drone.action.land()
            return True
        except Exception as exc:  # noqa: BLE001 -- any MAVSDK ActionError/connection issue
            log.error("Fallback action %s failed: %s", fallback_action.value, exc)
            return False

    @staticmethod
    async def _execute_bare_action(action: RecommendedAction, drone) -> bool:
        """
        Carries out an APPROVED bare-action proposal (no waypoint items)
        via MAVSDK's Action plugin. See this module's docstring for which
        MAVSDK calls back each `RecommendedAction` and the citation for
        `hold()`.
        """
        try:
            if action == RecommendedAction.RETURN_TO_LAUNCH:
                await drone.action.return_to_launch()
            elif action in (RecommendedAction.LAND, RecommendedAction.ABORT_MISSION):
                await drone.action.land()
            elif action == RecommendedAction.HOLD_POSITION:
                await drone.action.hold()
            else:
                # CONTINUE / SKIP_WAYPOINTS / INSERT_CORRECTIVE_WAYPOINTS / DIVERT with an
                # empty route is a degenerate "nothing to do" case -- no MAVSDK call needed.
                return True
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("Bare action %s failed: %s", action.value, exc)
            return False


def _demo_cli() -> None:
    """
    Manual test entry point covering TEST SCENARIO 1 (Battery deviation)
    end-to-end through the FULL FINAL FLOW, using a fake in-process MAVSDK
    System (no real PX4 SITL needed to see the flow -- run this project's
    other `_demo_cli()`s, or your own SITL run, for the parts that need a
    real flight controller):
        python -m app.replanning.orchestrator
    """
    import asyncio as _asyncio
    import json
    import types

    from app.mission.converter import ConvertedMission

    class _FakeMissionPlugin:
        def __init__(self):
            self.calls: list[str] = []

        async def pause_mission(self):
            self.calls.append("pause")

        async def clear_mission(self):
            self.calls.append("clear")

        async def upload_mission(self, plan):
            self.calls.append("upload")

        async def set_return_to_launch_after_mission(self, enable):
            self.calls.append(f"set_rtl({enable})")

        async def start_mission(self):
            self.calls.append("start")

        async def mission_progress(self):
            # Total intentionally matches the demo's own trimmed replan
            # (1 waypoint + RTH = 2 items) so this demo shows the happy path.
            yield types.SimpleNamespace(current=1, total=2)

    class _FakeActionPlugin:
        async def return_to_launch(self):
            print("[fake MAVSDK] action.return_to_launch()")

        async def land(self):
            print("[fake MAVSDK] action.land()")

        async def hold(self):
            print("[fake MAVSDK] action.hold()")

    class _FakeSystem:
        def __init__(self):
            self.mission = _FakeMissionPlugin()
            self.action = _FakeActionPlugin()

    import sys as _sys

    fake_mission_module = types.ModuleType("mavsdk.mission")
    fake_mission_module.MissionError = Exception
    fake_mavsdk_package = types.ModuleType("mavsdk")
    fake_mavsdk_package.mission = fake_mission_module
    _sys.modules["mavsdk"] = fake_mavsdk_package
    _sys.modules["mavsdk.mission"] = fake_mission_module

    import app.mavlink.mission_update_manager as mum

    mum.to_mavsdk_mission_plan = lambda mission, **kwargs: ConvertedMission(
        plan=types.SimpleNamespace(mission_items=list(mission.mission_items)),
        return_to_launch_after_mission=True,
    )

    home_lat, home_lon = 1.0000, 2.0000
    mission = Mission(
        mission_name="Demo 5-waypoint survey",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0000, longitude=2.0000, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.0090, longitude=2.0000, altitude=30),
            MissionItem(sequence=4, command=MissionCommand.WAYPOINT, latitude=1.0180, longitude=2.0000, altitude=30),
            MissionItem(sequence=5, command=MissionCommand.WAYPOINT, latitude=1.0270, longitude=2.0000, altitude=30),
            MissionItem(sequence=6, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )
    manager = MissionStateManager()
    manager.start(mission)
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=3, total=6)

    drone_state = DroneState(
        latitude_deg=1.0090, longitude_deg=2.0000, relative_altitude_m=30.0,
        battery_remaining_percent=0.22,
    )
    from app.trigger.events import EventSeverity, EventSource, EventType

    event = ReplanningEvent(
        event_type=EventType.BATTERY_DEVIATION,
        severity=EventSeverity.HIGH,
        source=EventSource.TELEMETRY,
        mission_id=manager.snapshot().mission_id,
        metadata={"battery_percent": 0.22, "min_reserve_percent": 0.15},
    )

    drone = _FakeSystem()
    orchestrator = RAMROrchestrator()

    result = _asyncio.run(
        orchestrator.handle_event(
            event, mission_state_manager=manager, drone_state=drone_state, drone=drone,
            home_latitude_deg=home_lat, home_longitude_deg=home_lon,
        )
    )
    print(json.dumps(result.as_dict(), indent=2, default=str))
    print(f"\nFake MAVSDK mission plugin calls: {drone.mission.calls}")


if __name__ == "__main__":
    _demo_cli()
