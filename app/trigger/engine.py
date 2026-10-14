"""
RAMR novelty Phase 3 -- Event and Trigger Infrastructure: TriggerEngine.

    CREATE: 3. TriggerEngine.
    SUCCESS CRITERIA: The TriggerEngine can receive an event and
    correctly identify whether adaptive replanning should be activated.
    TEST: Manually publish BATTERY_DEVIATION. Verify:
        Event -> TriggerEngine -> Adaptive Replanning Request
    DO NOT implement CV yet.

Design notes:

- `TriggerEngine.handle_event()` is the "receive an event and correctly
  identify whether adaptive replanning should be activated" function
  your success criteria names. It can be called directly, or wired to an
  `EventBus` (`bus.subscribe(engine.handle_event)`) so it reacts to
  every published event automatically, from whatever source published
  it -- consistent with "ALL trigger sources should use the same event
  format."
- `AdaptiveReplanningRequest` is the third link in
  "Event -> TriggerEngine -> Adaptive Replanning Request": produced only
  when `handle_event()` decides replanning should be activated. This
  project stops there -- nothing here actually replans anything. That's
  a future Adaptive Mission Replanner phase's job; this phase's whole
  scope is deciding WHETHER to ask for one and handing off a well-formed
  request when it does.
- The severity-threshold table below (`DEFAULT_ACTIVATION_THRESHOLDS`) is
  deliberately simple and documented as such -- it's what makes
  `handle_event()` a real, testable, deterministic decision today rather
  than a stub, but it is NOT meant to be the final word on (e.g.) exactly
  when a battery deviation is severe enough to replan for -- that's
  separate, later scope (the audit's Step 4 "Battery Feasibility
  Checker"). Thresholds are overridable per `TriggerEngine` instance for
  exactly that reason.
- CV event types (`IMAGE_BLUR_DETECTED`/`BAD_EXPOSURE_DETECTED`/
  `COVERAGE_GAP_DETECTED`/`INSPECTION_QUALITY_FAILURE`) already had
  default thresholds here since Phase 3, so they were handled correctly
  the moment a real publisher started using them without this file
  needing to change -- exactly what happened in RAMR/CV novelty Phase 13
  (`app/vision/event_generator.py`), which is the first thing that
  actually constructs and publishes `IMAGE_BLUR_DETECTED`/
  `BAD_EXPOSURE_DETECTED`/`INSPECTION_QUALITY_FAILURE` events from real
  CV output. `COVERAGE_GAP_DETECTED` remains unused -- no coverage-gap
  detector exists in this project yet.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from app.trigger.events import EventSeverity, EventType, ReplanningEvent, severity_at_least

log = logging.getLogger(__name__)


@dataclass
class AdaptiveReplanningRequest:
    """
    Produced by `TriggerEngine.handle_event()` once it decides adaptive
    replanning should be activated. Carries the triggering event along
    (so a downstream Adaptive Mission Replanner has full context: why
    this was requested, what the drone/mission state looked like at the
    time) plus a human-readable `reason`.
    """

    request_id: str
    event: ReplanningEvent
    reason: str
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "event_id": self.event.event_id,
            "event_type": self.event.event_type.value,
            "mission_id": self.event.mission_id,
            "reason": self.reason,
            "requested_at": self.requested_at.isoformat(),
        }


@dataclass
class TriggerDecision:
    """Full record of one `handle_event()` call, whether or not it activated a replan."""

    event: ReplanningEvent
    should_replan: bool
    reason: str
    request: AdaptiveReplanningRequest | None = None


# Minimum severity at which each event type is considered "worth
# interrupting the mission for." See this module's docstring for why
# these are simple, overridable defaults rather than hardened rules.
DEFAULT_ACTIVATION_THRESHOLDS: dict[EventType, EventSeverity] = {
    EventType.BATTERY_DEVIATION: EventSeverity.MEDIUM,
    EventType.MISSED_WAYPOINT: EventSeverity.MEDIUM,
    EventType.MISSION_DEVIATION: EventSeverity.MEDIUM,
    EventType.SAFETY_EVENT: EventSeverity.HIGH,
    EventType.OPERATOR_REQUEST: EventSeverity.LOW,  # any explicit operator request is honored
    EventType.IMAGE_BLUR_DETECTED: EventSeverity.MEDIUM,
    EventType.BAD_EXPOSURE_DETECTED: EventSeverity.MEDIUM,
    EventType.COVERAGE_GAP_DETECTED: EventSeverity.MEDIUM,
    # RAMR/CV novelty Phase 13 addition -- same MEDIUM default as the
    # other CV event types above, for the same documented reason.
    EventType.INSPECTION_QUALITY_FAILURE: EventSeverity.MEDIUM,
}


class TriggerEngine:
    """
    Usage (direct):
        engine = TriggerEngine()
        decision = engine.handle_event(event)
        if decision.should_replan:
            ...hand decision.request to a future Adaptive Mission Replanner...

    Usage (via EventBus -- so any publisher reaches the engine the same way):
        bus = EventBus()
        engine = TriggerEngine(on_replan_requested=my_callback)
        bus.subscribe(engine.handle_event)
        bus.publish(event)   # -> engine.handle_event() runs automatically
    """

    def __init__(
        self,
        thresholds: dict[EventType, EventSeverity] | None = None,
        on_replan_requested: Callable[[AdaptiveReplanningRequest], None] | None = None,
    ) -> None:
        self._thresholds = thresholds if thresholds is not None else dict(DEFAULT_ACTIVATION_THRESHOLDS)
        self._on_replan_requested = on_replan_requested
        self._decision_log: list[TriggerDecision] = []

    def handle_event(self, event: ReplanningEvent) -> TriggerDecision:
        """
        Decide whether `event` should activate adaptive replanning.
        Never raises for an unrecognized event type or a below-threshold
        severity -- both are normal, expected outcomes (same "don't raise
        for an expected rejection" pattern used throughout this project,
        e.g. `generate_validated_mission()` / `run_mission_on_px4()`).
        """
        threshold = self._thresholds.get(event.event_type)
        if threshold is None:
            decision = TriggerDecision(
                event=event,
                should_replan=False,
                reason=f"No activation threshold configured for {event.event_type.value}.",
            )
            log.warning(decision.reason)
            self._decision_log.append(decision)
            return decision

        if not severity_at_least(event.severity, threshold):
            decision = TriggerDecision(
                event=event,
                should_replan=False,
                reason=(
                    f"{event.event_type.value} severity {event.severity.value} is below "
                    f"activation threshold {threshold.value} -- not replanning."
                ),
            )
            log.info(decision.reason)
            self._decision_log.append(decision)
            return decision

        reason = (
            f"{event.event_type.value} severity {event.severity.value} meets/exceeds "
            f"activation threshold {threshold.value} -- adaptive replanning activated."
        )
        request = AdaptiveReplanningRequest(request_id=str(uuid.uuid4()), event=event, reason=reason)
        decision = TriggerDecision(event=event, should_replan=True, reason=reason, request=request)
        log.info(reason)
        self._decision_log.append(decision)

        if self._on_replan_requested is not None:
            self._on_replan_requested(request)

        return decision

    def decision_log(self) -> list[TriggerDecision]:
        """Every decision made so far, oldest first -- for tests/debugging/a future logger."""
        return list(self._decision_log)


def _demo_cli() -> None:
    """
    Manual test entry point, matching your spec's own TEST section
    exactly: manually publish a BATTERY_DEVIATION event and verify
    Event -> TriggerEngine -> Adaptive Replanning Request.
        python -m app.trigger.engine
    No PX4/LLM/network needed -- everything here is synthetic/offline.
    """
    import json

    from app.trigger.bus import EventBus
    from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    bus = EventBus()
    requests: list[AdaptiveReplanningRequest] = []
    engine = TriggerEngine(on_replan_requested=requests.append)
    bus.subscribe(engine.handle_event)

    event = ReplanningEvent(
        event_type=EventType.BATTERY_DEVIATION,
        severity=EventSeverity.HIGH,
        source=EventSource.TELEMETRY,
        mission_id="demo-mission-1",
        metadata={"battery_percent": 0.12, "threshold_percent": 0.20},
    )

    print("Publishing event:")
    print(json.dumps(event.as_dict(), indent=2))

    bus.publish(event)

    print(f"\nTriggerEngine made {len(engine.decision_log())} decision(s).")
    decision = engine.decision_log()[-1]
    print(f"should_replan: {decision.should_replan}")
    print(f"reason: {decision.reason}")

    if requests:
        print("\nAdaptiveReplanningRequest produced:")
        print(json.dumps(requests[-1].as_dict(), indent=2))
    else:
        print("\nNo AdaptiveReplanningRequest was produced.")


if __name__ == "__main__":
    _demo_cli()
