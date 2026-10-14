"""
Export a validated Mission as a QGroundControl plan file (.plan).

The planner stays the brain: it decides the route, the camera aim and the
checks. QGroundControl (QGC) is the pilot's console: it opens this file,
shows it on its own map, uploads it to the flight controller, and flies it
with QGC's own safety switches. This module only WRITES the file. It never
talks to a drone.

QGC's .plan format is JSON (fileType "Plan", version 1). Each mission item
is a MAVLink command (command id, 7 parameters, frame). Mapping used here:

    TAKEOFF            MAV_CMD 22  NAV_TAKEOFF          (lat/lon borrowed from the next waypoint, as in converter.py)
    WAYPOINT           MAV_CMD 16  NAV_WAYPOINT         param4 = yaw (heading), null when not set
    LOITER             MAV_CMD 19  NAV_LOITER_TIME      param1 = hold seconds
    LAND               MAV_CMD 21  NAV_LAND
    RETURN_TO_LAUNCH   MAV_CMD 20  NAV_RETURN_TO_LAUNCH
    gimbal pitch       MAV_CMD 205 DO_MOUNT_CONTROL     written just before its waypoint, param1 = pitch, param7 = 2 (MAVLink targeting)
    photo              MAV_CMD 203 DO_DIGICAM_CONTROL   written just after its waypoint, param5 = 1 (shoot)

Altitudes are relative to home (MAV_FRAME_GLOBAL_RELATIVE_ALT = 3), the same
as the rest of the project. Commands without a position use frame 2
(MAV_FRAME_MISSION) with zeros, which is how QGC itself writes them.
Nothing in this file is sent to a drone; use QGC to upload the result.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import settings
from app.mission.schema import Mission, MissionCommand

# MAVLink command ids and frames (https://mavlink.io/en/messages/common.html)
MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_NAV_LOITER_TIME = 19
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_NAV_LAND = 21
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_DO_DIGICAM_CONTROL = 203
MAV_CMD_DO_MOUNT_CONTROL = 205
MAV_FRAME_MISSION = 2
MAV_FRAME_GLOBAL_RELATIVE_ALT = 3
MAV_MOUNT_MODE_MAVLINK_TARGETING = 2

# QGC's firmware/vehicle ids: 12 = PX4 Pro, 2 = quadrotor
QGC_FIRMWARE_PX4 = 12
QGC_VEHICLE_QUADROTOR = 2

_NAN = float("nan")


class QGCExportError(ValueError):
    """The mission cannot be written as a QGC plan (e.g. it has no position to start from)."""


def _item(command: int, params: list, *, frame: int, auto_continue: bool = True) -> dict:
    return {
        "type": "SimpleItem",
        "autoContinue": auto_continue,
        "command": command,
        "doJumpId": 0,  # set afterwards, in order
        "frame": frame,
        "params": [None if (isinstance(p, float) and p != p) else p for p in params],  # NaN -> null, as QGC writes it
    }


def _positional(command: int, lat: float, lon: float, alt: float, p1=0, p4=None) -> dict:
    params = [p1, 0, 0, p4, lat, lon, alt]
    return _item(command, params, frame=MAV_FRAME_GLOBAL_RELATIVE_ALT)


def _plain(command: int, params: list) -> dict:
    return _item(command, params + [0] * (7 - len(params)), frame=MAV_FRAME_MISSION)


def mission_to_qgc_items(mission: Mission) -> list[dict]:
    """The QGC SimpleItems for `mission`, in order. Raises QGCExportError if there is nothing to take off from."""
    items = mission.mission_items
    coords = [(i.latitude, i.longitude) for i in items if i.latitude is not None and i.longitude is not None]
    if not coords:
        raise QGCExportError("The mission has no coordinates -- nothing to write to a QGC plan.")

    out: list[dict] = []
    last_lat, last_lon = coords[0]
    for index, item in enumerate(items):
        if item.command == MissionCommand.TAKEOFF:
            # Take off at the first waypoint's position, as converter.py does (QGC's takeoff needs a position).
            lat, lon = next(((i.latitude, i.longitude) for i in items[index + 1:] if i.latitude is not None), coords[0])
            out.append(_positional(MAV_CMD_NAV_TAKEOFF, lat, lon, item.altitude or 0.0))
            last_lat, last_lon = lat, lon
        elif item.command in (MissionCommand.WAYPOINT, MissionCommand.LOITER):
            if item.latitude is None or item.longitude is None:
                raise QGCExportError(f"Item #{item.sequence} ({item.command.value}) has no position.")
            if item.gimbal_pitch_deg is not None:
                out.append(_plain(MAV_CMD_DO_MOUNT_CONTROL, [item.gimbal_pitch_deg, 0, 0, 0, 0, 0, MAV_MOUNT_MODE_MAVLINK_TARGETING]))
            if item.command == MissionCommand.WAYPOINT:
                out.append(_positional(MAV_CMD_NAV_WAYPOINT, item.latitude, item.longitude, item.altitude or 0.0,
                                       p4=item.yaw_deg))
            else:
                out.append(_positional(MAV_CMD_NAV_LOITER_TIME, item.latitude, item.longitude, item.altitude or 0.0,
                                       p1=item.hold_time_s or 0))
            if item.take_photo:
                out.append(_plain(MAV_CMD_DO_DIGICAM_CONTROL, [0, 0, 0, 0, 1, 0]))
            last_lat, last_lon = item.latitude, item.longitude
        elif item.command == MissionCommand.LAND:
            out.append(_positional(MAV_CMD_NAV_LAND, last_lat, last_lon, 0.0))
        elif item.command == MissionCommand.RETURN_TO_LAUNCH:
            out.append(_plain(MAV_CMD_NAV_RETURN_TO_LAUNCH, []))
        else:  # pragma: no cover -- MissionCommand is a closed set
            raise QGCExportError(f"Unsupported command {item.command.value}.")

    for n, entry in enumerate(out, start=1):
        entry["doJumpId"] = n
    return out


def build_qgc_plan(mission: Mission, *, home: tuple[float, float] | None = None, name: str | None = None) -> dict:
    """The whole .plan document as a dict."""
    items = mission_to_qgc_items(mission)
    if home is None:
        first = next((i for i in mission.mission_items if i.latitude is not None), None)
        home = (first.latitude, first.longitude)
    return {
        "fileType": "Plan",
        "version": 1,
        "groundStation": "Autonomous AI Drone Mission Planner",
        "mission": {
            "cruiseSpeed": settings.mission_default_speed_ms,
            "firmwareType": QGC_FIRMWARE_PX4,
            "hoverSpeed": settings.mission_default_speed_ms,
            "items": items,
            "plannedHomePosition": [home[0], home[1], 0.0],
            "vehicleType": QGC_VEHICLE_QUADROTOR,
            "version": 2,
        },
        "geoFence": {"circles": [], "polygons": [], "version": 2},
        "rallyPoints": {"points": [], "version": 2},
        "name": name or mission.mission_name,
    }


def qgc_plan_json(mission: Mission, *, home: tuple[float, float] | None = None, name: str | None = None) -> str:
    return json.dumps(build_qgc_plan(mission, home=home, name=name), indent=2)


def save_qgc_plan(mission: Mission, path: str | Path, *, home: tuple[float, float] | None = None, name: str | None = None) -> Path:
    target = Path(path)
    target.write_text(qgc_plan_json(mission, home=home, name=name), encoding="utf-8")
    return target
