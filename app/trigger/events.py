"""
RAMR novelty Phase 3 -- Event and Trigger Infrastructure: event models.

    OBJECTIVE: Create a common event-driven trigger architecture.
    CREATE: 1. Event models  2. Event bus or event dispatcher  3. TriggerEngine
    ALL trigger sources should use the same event format.

This file is item 1 (event models). `bus.py` is item 2, `engine.py` is
item 3.

Design notes:

- `ReplanningEvent` is the ONE common event shape every trigger source
  uses, exactly as your spec requires ("ALL trigger sources should use
  the same event format") -- a future CV module publishing
  `IMAGE_BLUR_DETECTED` and today's telemetry-driven
  `BATTERY_DEVIATION` are both just `ReplanningEvent` instances with a
  different `event_type`/`source`/`metadata`. Fields match your spec
  exactly: event_id, mission_id, event_type, severity, timestamp,
  drone_state, mission_state, metadata, source.
- "Reuse existing data structures" (the standing project rule): `drone_state`
  and `mission_state` on the event are the EXACT `DroneState`
  (`app/mavlink/drone_state.py`) and `MissionState`
  (`app/mission/state.py`) objects already built in Phases 1-2 -- no new
  "event-flavored" snapshot type was invented.
- `EventType` includes every event your spec lists, including the three
  CV ones (`IMAGE_BLUR_DETECTED`, `BAD_EXPOSURE_DETECTED`,
  `COVERAGE_GAP_DETECTED`). Per your instruction ("DO NOT implement CV
  yet"), nothing in this project detects blur/exposure/coverage gaps --
  these three values exist here ONLY so the event model and
  `TriggerEngine` already know how to handle them the moment a future CV
  phase starts publishing them, without this file or `engine.py` needing
  to change then.
- `EventSource` covers the two examples your spec gives (`TELEMETRY`,
  `COMPUTER_VISION`) plus `OPERATOR` and `SAFETY_MONITOR`, since your own
  event list includes `OPERATOR_REQUEST` and `SAFETY_EVENT`, which need a
  matching source. Not explicitly specified beyond the two examples, so
  flagging this as a reasonable, easily-extended default rather than
  something copied verbatim from your text.
- `EventSeverity` (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`) is not explicitly
  named in your spec, but `TriggerEngine`'s success criteria ("correctly
  identify whether adaptive replanning should be activated") needs
  *some* signal to decide on beyond just "this event type happened" --
  severity is the natural one, and your own `ReplanningEvent` field list
  already includes `severity`. `severity_at_least()` below is the
  ordering helper `engine.py` uses for threshold comparisons.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.mavlink.drone_state import DroneState
from app.mission.state import MissionState


class EventType(str, Enum):
    """Every event type your spec listed under "CREATE EVENTS SUCH AS"."""

    BATTERY_DEVIATION = "BATTERY_DEVIATION"
    MISSED_WAYPOINT = "MISSED_WAYPOINT"
    MISSION_DEVIATION = "MISSION_DEVIATION"
    SAFETY_EVENT = "SAFETY_EVENT"
    OPERATOR_REQUEST = "OPERATOR_REQUEST"
    IMAGE_BLUR_DETECTED = "IMAGE_BLUR_DETECTED"
    BAD_EXPOSURE_DETECTED = "BAD_EXPOSURE_DETECTED"
    COVERAGE_GAP_DETECTED = "COVERAGE_GAP_DETECTED"
    # RAMR/CV novelty Phase 13 addition -- a generic "this inspection
    # frame failed quality analysis" event, for a CV quality problem that
    # doesn't map to one of the specific types above (e.g. a future
    # quality check this project doesn't have yet). See
    # app/vision/event_generator.py for where this is actually produced.
    INSPECTION_QUALITY_FAILURE = "INSPECTION_QUALITY_FAILURE"


class EventSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


_SEVERITY_ORDER: dict[EventSeverity, int] = {
    EventSeverity.LOW: 0,
    EventSeverity.MEDIUM: 1,
    EventSeverity.HIGH: 2,
    EventSeverity.CRITICAL: 3,
}


def severity_at_least(severity: EventSeverity, threshold: EventSeverity) -> bool:
    """True if `severity` is at or above `threshold` on the LOW < MEDIUM < HIGH < CRITICAL scale."""
    return _SEVERITY_ORDER[severity] >= _SEVERITY_ORDER[threshold]


class EventSource(str, Enum):
    """Where an event originated. TELEMETRY and COMPUTER_VISION are your spec's own examples."""

    TELEMETRY = "TELEMETRY"
    COMPUTER_VISION = "COMPUTER_VISION"
    OPERATOR = "OPERATOR"
    SAFETY_MONITOR = "SAFETY_MONITOR"


@dataclass
class ReplanningEvent:
    """
    The one common event shape every trigger source publishes.

    `event_id` and `timestamp` are auto-generated if not supplied.
    `mission_id`, `drone_state`, `mission_state`, and `metadata` are
    optional -- a very early event (e.g. an operator request before any
    mission has started) may not have a mission/drone state to attach
    yet, and `metadata` carries whatever event-specific detail doesn't
    belong in the common fields (e.g. `{"battery_percent": 0.12,
    "threshold_percent": 0.20}` for a BATTERY_DEVIATION).

    `waypoint_id` -- RAMR/CV novelty Phase 13 addition -- is the 1-based
    mission-item sequence number the event concerns (e.g. "WP8" in your
    Phase 13 example is this project's `MissionItem.sequence`/
    `InspectionFrame.waypoint_id`/`InspectionQualityResult.waypoint_id`
    convention, reused rather than inventing a new string waypoint-ID
    scheme -- see `app/mission/schema.py`). Optional and defaults to
    `None` since most existing event types (BATTERY_DEVIATION,
    OPERATOR_REQUEST, ...) aren't about one specific waypoint; every
    caller that constructed a `ReplanningEvent` before this phase keeps
    working unchanged.
    """

    event_type: EventType
    severity: EventSeverity
    source: EventSource

    mission_id: str | None = None
    waypoint_id: int | None = None
    drone_state: DroneState | None = None
    mission_state: MissionState | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        """Flat, JSON-serializable summary -- for logging and for a future Replanning Event Logger."""
        return {
            "event_id": self.event_id,
            "mission_id": self.mission_id,
            "waypoint_id": self.waypoint_id,
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "source": self.source.value,
            "timestamp": self.timestamp.isoformat(),
            "drone_state": self.drone_state.as_dict() if self.drone_state else None,
            "mission_state": self.mission_state.as_dict() if self.mission_state else None,
            "metadata": self.metadata,
        }
