# Waypoints and Mission Sequencing Rules

These are the deterministic sequencing rules this project's Phase 5
validator enforces on every LLM-generated mission, independent of
whatever the LLM itself produced.

## Sequence numbers

- Every mission item must have a `sequence` number.
- Sequence numbers must be unique within a mission — no two items may
  share the same sequence number.
- Sequence numbers should be contiguous and increasing (1, 2, 3, ... in
  this project's 1-based convention) with no gaps and no items out of
  order. A mission with sequence numbers `[1, 2, 4]` (a gap) or
  `[1, 3, 2]` (out of order) is rejected as malformed, even though each
  individual item might otherwise be valid, because it indicates the LLM
  did not generate a coherent ordered plan.

## Logical ordering

- A multicopter mission that starts from the ground should begin with a
  `TAKEOFF` item. A mission that begins with a `WAYPOINT`, `LAND`, or
  `RETURN_TO_LAUNCH` as its first item is rejected.
- `TAKEOFF` should not appear more than once in a mission — a second
  takeoff mid-mission has no defined meaning for a multicopter that is
  already airborne and is rejected.
- A mission should end in a terminating action: `LAND` or
  `RETURN_TO_LAUNCH`. A mission whose last item is a `WAYPOINT` (i.e. it
  just stops flying somewhere with no landing or return instruction) is
  rejected, because the vehicle's behavior after the last waypoint would
  otherwise be undefined by this project's own contract with the user.
- `RETURN_TO_LAUNCH` and `LAND`, if present, should be the **last** item
  in the mission — a `WAYPOINT` appearing after an `RTL`/`LAND` item is
  unreachable and indicates a malformed plan.

## Waypoint coordinate requirements

- Every `WAYPOINT`, `LAND` (when it specifies a location), and `LOITER`
  item must include `latitude` and `longitude`.
- Latitude must be between -90 and +90 degrees.
- Longitude must be between -180 and +180 degrees.
- A coordinate of exactly `(0, 0)` (the point in the Gulf of Guinea
  sometimes called "Null Island") is a common sign of a missing/unset
  value rather than an intentional destination, and this project's
  validator flags it as a warning even though it is technically a valid
  coordinate pair.

## Altitude requirements

- `TAKEOFF`, `WAYPOINT`, and `LOITER` items must include an `altitude`
  in meters.
- Altitude must fall within the project's configured
  `MIN_ALTITUDE_M`–`MAX_ALTITUDE_M` range (see
  `px4_mission_requirements.md`).
- A negative altitude is always invalid for these commands in this
  project's schema (it would mean below the takeoff point, which is not
  a supported mission shape here) and is rejected outright rather than
  merely flagged.

## Mission size

- A mission with zero mission items is invalid — there is nothing to
  fly.
- This project caps mission size at `MAX_WAYPOINTS` (default 50) items
  as a sanity limit; a much larger number is more likely to indicate the
  LLM produced a malformed/repetitive plan than a genuinely intended
  large survey mission, and should be reviewed rather than silently
  accepted.
