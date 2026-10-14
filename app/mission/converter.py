"""
Phase 6 -- converts our own `Mission` (schema-valid AND rule-valid, i.e.
already passed through `app.mission.validator.validate_mission()`) into
the shape MAVSDK's `mission` plugin actually needs to upload to PX4:
`mavsdk.mission.MissionPlan`.

Read this module's docstring fully before touching it -- several of its
choices come directly from things that turned out to be non-obvious
after checking MAVSDK's current (3.17.2) docs/source rather than
trusting assumptions, and are recorded here so a later phase doesn't
"fix" them back into something wrong:

1. `MissionItem`'s constructor takes 14 POSITIONAL arguments in a fixed
   order (verified against
   https://github.com/mavlink/MAVSDK-Python/blob/main/mavsdk/mission.py
   and the official example at
   https://github.com/mavlink/MAVSDK-Python/blob/main/examples/mission.py):
       latitude_deg, longitude_deg, relative_altitude_m, speed_m_s,
       is_fly_through, gimbal_pitch_deg, gimbal_yaw_deg, camera_action,
       loiter_time_s, camera_photo_interval_s, acceptance_radius_m,
       yaw_deg, camera_photo_distance_m, vehicle_action
   Fields this project doesn't use (gimbal/camera/yaw/acceptance-radius)
   are passed `float("nan")` -- the exact sentinel the official example
   uses for "leave this at the PX4 default", not a made-up placeholder.

2. `MissionItem.VehicleAction` has `TAKEOFF` and `LAND` members. Per the
   C++ API reference (mavsdk.mavlink.io/main/en/cpp/api_reference/
   structmavsdk_1_1_mission_1_1_mission_item.html), their semantics are:
     - Takeoff: "Vehicle will takeoff and go to defined waypoint" -- so
       a Takeoff item's OWN coordinates are where it flies to right
       after lifting off, not a throwaway value.
     - Land: "When a waypoint is reached vehicle will land at current
       position" -- so a Land item's coordinates are where it flies to
       BEFORE descending.
   This directly conflicts with our own `MissionItem` schema
   (`app/mission/schema.py`), which *forbids* latitude/longitude on
   TAKEOFF and LAND (deliberately -- real MAVLink TAKEOFF/LAND commands
   don't need them, and it keeps the LLM from being asked to invent
   coordinates for a "just go up" or "just come down" step). Rather than
   change the schema (and force the LLM to hallucinate a coordinate for
   these), this converter INFERS the coordinate:
     - TAKEOFF borrows the coordinates of the next mission item that
       has any (searching forward).
     - LAND borrows the coordinates of the previous mission item that
       has any (searching backward).
     - If neither search finds a coordinate (a degenerate
       "TAKEOFF, LAND" mission with nothing in between -- schema- and
       rule-valid, if unusual), the caller-supplied home position is
       used, since that's the only coordinate available in that case.
   This is exactly why `to_mavsdk_mission_plan()` accepts optional
   `home_latitude_deg`/`home_longitude_deg` parameters -- Phase 7's
   PX4-SITL connection code is expected to read the vehicle's actual
   home position (via `Telemetry.home()`) and pass it in before
   converting, so this function stays a pure, synchronously-testable
   transformation with no MAVSDK connection of its own.

3. RETURN_TO_LAUNCH has NO equivalent `MissionItem` at all -- MAVSDK
   handles it as mission-plugin *behavior*
   (`Mission.set_return_to_launch_after_mission(True)`), called once
   after upload, not as an item in the plan. So a Mission ending in
   RETURN_TO_LAUNCH produces a `MissionPlan` with no item for it, plus
   `ConvertedMission.return_to_launch_after_mission = True` telling the
   Phase 7 caller to make that call. A Mission ending in LAND instead
   produces an explicit `Land`-action item and
   `return_to_launch_after_mission = False`.

Everything here is validated with the same instructions given at the
top of this project: verify MAVSDK/PX4 API behavior against current
official documentation before writing code that depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.mission.schema import Mission, MissionCommand, MissionItem


class MissionConversionError(Exception):
    """
    Raised when a schema-valid, rule-valid Mission still can't be
    converted -- specifically, a TAKEOFF or LAND item has no coordinate
    anywhere in the mission to borrow AND no home position was supplied.
    This should be rare in practice (Phase 7 always has a home position
    from telemetry once connected), but converter.py must not silently
    invent a (0, 0) coordinate -- that's exactly the "Null Island"
    footgun `validator.py` already warns about elsewhere in this
    project.
    """


@dataclass
class ConvertedMission:
    """What `to_mavsdk_mission_plan()` hands back to the Phase 7 uploader."""

    plan: "object"  # mavsdk.mission.MissionPlan -- see note below on lazy import
    return_to_launch_after_mission: bool

def _or_nan(value):
    return float("nan") if value is None else float(value)

def _find_coordinates_forward(items: list[MissionItem], start: int) -> tuple[float, float] | None:
    for item in items[start:]:
        if item.latitude is not None and item.longitude is not None:
            return item.latitude, item.longitude
    return None


def _find_coordinates_backward(items: list[MissionItem], start: int) -> tuple[float, float] | None:
    for item in reversed(items[: start + 1]):
        if item.latitude is not None and item.longitude is not None:
            return item.latitude, item.longitude
    return None


def to_mavsdk_mission_plan(
    mission: Mission,
    *,
    home_latitude_deg: float | None = None,
    home_longitude_deg: float | None = None,
) -> ConvertedMission:
    """
    Convert an already schema-valid AND rule-valid `Mission` into a
    `ConvertedMission` wrapping a `mavsdk.mission.MissionPlan`.

    This function does NOT itself call `validate_mission()` -- by the
    time a mission reaches Phase 6, it must have already come through
    Phase 5 with `valid=True` (see `app/mission/generator.py`'s
    `generate_validated_mission()`). Converting an unvalidated mission
    is a programming error, not something this function should silently
    paper over.

    `home_latitude_deg`/`home_longitude_deg` are only used as a last
    resort, for a TAKEOFF or LAND item with no other coordinate to
    borrow from the rest of the mission -- see the module docstring,
    point 2.

    Raises `MissionConversionError` if a TAKEOFF/LAND item needs a
    coordinate and none is available (neither borrowed nor supplied).
    """
    # Lazy import -- same reasoning as the lazy `ollama`/`groq` imports in
    # app/llm/llm_client.py: importing this module (e.g. for its type
    # hints, or from a test that never actually builds a plan) shouldn't
    # require the `mavsdk` package to be installed.
    from mavsdk.mission import MissionItem as MavsdkMissionItem
    from mavsdk.mission import MissionPlan

    items = mission.mission_items
    mavsdk_items: list[MavsdkMissionItem] = []
    return_to_launch_after_mission = False

    # Tracks the most recent altitude we've seen, for the degenerate
    # case of a LAND item with no preceding waypoint to borrow an
    # altitude from either (see module docstring point 2) -- falls back
    # to the configured minimum safe altitude if nothing has been seen
    # yet (shouldn't happen given TAKEOFF must be first per
    # validate_mission(), but this keeps the function total rather than
    # crashing on a hypothetical future rule change).
    last_known_altitude_m = settings.min_altitude_m

    for index, item in enumerate(items):
        if item.command == MissionCommand.RETURN_TO_LAUNCH:
            # No MissionItem for this -- see module docstring point 3.
            return_to_launch_after_mission = True
            continue

        if item.command == MissionCommand.TAKEOFF:
            coords = _find_coordinates_forward(items, index + 1)
            if coords is None:
                if home_latitude_deg is None or home_longitude_deg is None:
                    raise MissionConversionError(
                        f"Item #{item.sequence} (TAKEOFF) has no waypoint after it to borrow "
                        "coordinates from, and no home position was supplied. Pass "
                        "home_latitude_deg/home_longitude_deg (e.g. from Telemetry.home() "
                        "once connected to PX4) or add at least one WAYPOINT/LOITER item."
                    )
                coords = (home_latitude_deg, home_longitude_deg)
            latitude, longitude = coords
            last_known_altitude_m = item.altitude
            mavsdk_items.append(
                MavsdkMissionItem(
                    latitude,
                    longitude,
                    item.altitude,
                    settings.mission_default_speed_ms,
                    True,  # is_fly_through
                    float("nan"),  # gimbal_pitch_deg
                    float("nan"),  # gimbal_yaw_deg
                    MavsdkMissionItem.CameraAction.NONE,
                    float("nan"),  # loiter_time_s
                    float("nan"),  # camera_photo_interval_s
                    float("nan"),  # acceptance_radius_m
                    float("nan"),  # yaw_deg
                    float("nan"),  # camera_photo_distance_m
                    MavsdkMissionItem.VehicleAction.TAKEOFF,
                )
            )
            continue

        if item.command == MissionCommand.LAND:
            coords = _find_coordinates_backward(items, index - 1)
            if coords is None:
                if home_latitude_deg is None or home_longitude_deg is None:
                    raise MissionConversionError(
                        f"Item #{item.sequence} (LAND) has no preceding waypoint to borrow "
                        "coordinates from, and no home position was supplied. Pass "
                        "home_latitude_deg/home_longitude_deg (e.g. from Telemetry.home() "
                        "once connected to PX4) or add at least one WAYPOINT/LOITER item."
                    )
                coords = (home_latitude_deg, home_longitude_deg)
            latitude, longitude = coords
            mavsdk_items.append(
                MavsdkMissionItem(
                    latitude,
                    longitude,
                    last_known_altitude_m,
                    settings.mission_default_speed_ms,
                    False,  # is_fly_through -- must actually stop to land
                    float("nan"),
                    float("nan"),
                    MavsdkMissionItem.CameraAction.NONE,
                    float("nan"),
                    float("nan"),
                    float("nan"),
                    float("nan"),
                    float("nan"),
                    MavsdkMissionItem.VehicleAction.LAND,
                )
            )
            continue

        # WAYPOINT and LOITER: both are guaranteed by validate_mission()
        # to carry latitude/longitude/altitude already.
        last_known_altitude_m = item.altitude
        mavsdk_items.append(
            MavsdkMissionItem(
                item.latitude,
                item.longitude,
                item.altitude,
                settings.mission_default_speed_ms,
                # Fly through plain waypoints; stop to loiter -- and stop for an inspection photo.
                item.command == MissionCommand.WAYPOINT and not item.take_photo,
                _or_nan(item.gimbal_pitch_deg),
                0.0 if item.gimbal_pitch_deg is not None else float("nan"),  # gimbal yaw relative to the nose
                MavsdkMissionItem.CameraAction.TAKE_PHOTO if item.take_photo else MavsdkMissionItem.CameraAction.NONE,
                item.hold_time_s if item.hold_time_s is not None else float("nan"),
                float("nan"),
                float("nan"),
                _or_nan(item.yaw_deg),  # heading (PX4 uses it when MIS_YAWMODE = 0 "heading as set by mission")
                float("nan"),
                MavsdkMissionItem.VehicleAction.NONE,
            )
        )

    return ConvertedMission(
        plan=MissionPlan(mavsdk_items),
        return_to_launch_after_mission=return_to_launch_after_mission,
    )
