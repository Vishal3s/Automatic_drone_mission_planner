"""
RAMR novelty Phase 3 -- Event and Trigger Infrastructure: event bus.

    CREATE: 2. Event bus or event dispatcher.

A minimal, synchronous publish/subscribe dispatcher. Deliberately NOT
async and NOT tied to MAVSDK/asyncio in any way -- unlike
`app/mavlink/drone_state.py`'s `subscribe_drone_state()`, nothing here
needs to await anything. Any code path (a synchronous rule check, or a
callback invoked from inside an async telemetry watcher) can call
`publish()` directly.

Why this exists as its own object rather than `TriggerEngine` just being
called directly: your spec asks for the bus and the engine as two
separate deliverables, and keeping them separate means MULTIPLE
subscribers can react to the same event stream independently -- e.g. a
future Replanning Event Logger (Step 7 of the audit) can subscribe
alongside `TriggerEngine` without the engine needing to know the logger
exists, and vice versa. This is also what makes "ALL trigger sources
should use the same event format" actually enforceable: every publisher
(telemetry, a future CV module, an operator UI) calls the same
`publish(event)` with the same `ReplanningEvent` shape, regardless of
who's listening.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

from app.trigger.events import EventType, ReplanningEvent

log = logging.getLogger(__name__)

Subscriber = Callable[[ReplanningEvent], None]


class EventBus:
    """
    Subscribe to a specific `EventType`, or pass `event_type=None` to
    receive every event regardless of type. `publish()` never raises
    because one subscriber's exception -- a bug in a future CV module,
    say -- must not stop every other subscriber (including
    `TriggerEngine`) from seeing the event.
    """

    def __init__(self) -> None:
        self._subscribers_by_type: dict[EventType, list[Subscriber]] = defaultdict(list)
        self._subscribers_all: list[Subscriber] = []
        self._history: list[ReplanningEvent] = []  # useful for tests/debugging/a future logger

    def subscribe(self, callback: Subscriber, event_type: EventType | None = None) -> None:
        if event_type is None:
            self._subscribers_all.append(callback)
        else:
            self._subscribers_by_type[event_type].append(callback)

    def publish(self, event: ReplanningEvent) -> None:
        self._history.append(event)
        log.info(
            "Event published: %s (severity=%s, source=%s, mission_id=%s)",
            event.event_type.value, event.severity.value, event.source.value, event.mission_id,
        )
        subscribers = [*self._subscribers_all, *self._subscribers_by_type.get(event.event_type, [])]
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                log.exception(
                    "Subscriber %r raised while handling event %s (%s) -- other subscribers still ran.",
                    callback, event.event_id, event.event_type.value,
                )

    def history(self) -> list[ReplanningEvent]:
        """Every event published so far, oldest first -- for tests/debugging."""
        return list(self._history)
