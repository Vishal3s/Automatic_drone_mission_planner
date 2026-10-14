# MAVLink Navigation Commands Used in This Project

This project's mission generator is restricted to the small set of MAVLink
navigation commands listed below. These are the only commands the
`app/mission/schema.py` enum (Phase 4) will allow — anything else the LLM
proposes gets rejected before it ever reaches MAVSDK.

## MAV_CMD_NAV_TAKEOFF

Commands the vehicle to take off from its current position and climb to a
target altitude.

- Key parameter: target altitude (meters). For multicopters this is
  normally interpreted as altitude above the takeoff point (relative
  altitude), not above mean sea level.
- Must be the **first** item in a mission for a multicopter mission that
  starts on the ground — PX4 rejects a mission whose first item is a
  waypoint/land/RTL if the vehicle is not already airborne.
- Latitude/longitude parameters are typically ignored for a multicopter
  takeoff (it takes off from wherever it is armed); they matter more for
  fixed-wing takeoff.

## MAV_CMD_NAV_WAYPOINT

Commands the vehicle to fly to a specific latitude/longitude/altitude and
then continue to the next mission item.

- Required parameters: latitude (degrees), longitude (degrees), altitude
  (meters, relative to home/takeoff point unless the mission explicitly
  uses an absolute frame).
- Optional parameters: acceptance radius (how close the vehicle must get
  before the waypoint is considered "reached," typically a few meters),
  hold time (seconds to loiter at the waypoint before continuing).
- Multiple `NAV_WAYPOINT` items in sequence form the core flight path of
  an inspection or survey mission.

## MAV_CMD_NAV_LAND

Commands the vehicle to land at a specified location (or its current
location if none is given).

- If latitude/longitude are provided, the vehicle flies to that position
  before descending and landing.
- Typically the final item in a mission that ends with landing rather
  than returning home.

## MAV_CMD_NAV_RETURN_TO_LAUNCH (RTL)

Commands the vehicle to fly back to its home/launch position and land (or
hold, depending on vehicle configuration).

- Takes no waypoint parameters — the flight controller already knows the
  recorded home position.
- The most common and safest mission termination action; PX4's own
  failsafe system also triggers this same behavior automatically on low
  battery or lost link, so a mission that ends in RTL is consistent with
  the vehicle's own safety behavior.

## MAV_CMD_NAV_LOITER_TIME / MAV_CMD_NAV_LOITER_UNLIM

Commands the vehicle to circle/hold at a location.

- `LOITER_TIME`: holds position for a specified duration (seconds) then
  continues to the next mission item.
- `LOITER_UNLIM`: holds indefinitely — used as a "pause here until told
  otherwise" item; a mission should generally not end on an unlimited
  loiter as a *default* termination action, since the vehicle would
  never land or return on its own.

## Commands intentionally excluded from this project's schema

The following exist in the full MAVLink command set but are **not**
exposed to the LLM or the schema in this project, because they either
need vehicle-specific tuning, are irrelevant to inspection-style
multirotor missions, or add risk without a validated use case yet:
`MAV_CMD_NAV_VTOL_TAKEOFF/LAND`, `MAV_CMD_DO_SET_CAM_TRIGG_DIST`,
`MAV_CMD_NAV_SPLINE_WAYPOINT`, `MAV_CMD_CONDITION_YAW`,
`MAV_CMD_DO_JUMP`. They can be added deliberately in a later phase, with
matching validation rules, if the project needs them.
