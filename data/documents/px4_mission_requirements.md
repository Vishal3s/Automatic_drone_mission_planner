# PX4 Mission Requirements

Rules PX4 (and this project's own rule validator in Phase 5) expect a
mission to satisfy before it will be accepted and flown.

## Altitude frame

PX4 mission waypoints are normally specified as altitude **relative to
the home/takeoff position**, not above mean sea level (AMSL) and not
above ground level at the waypoint itself (PX4 does not do live terrain
following unless a terrain-following mode is explicitly configured).
Because the drone's home position is set at arm time and terrain is
generally not flat, a fixed relative altitude does not guarantee a fixed
height above the ground at every waypoint on uneven terrain — this is a
known limitation to mention when discussing mission safety, not a bug in
this project's code.

## Mission structure

- A mission is an ordered list of mission items, each with a sequence
  number starting at 0 (or 1, depending on convention — this project
  uses 1-based sequence numbers in the LLM-facing schema and remaps to
  0-based internally during MAVSDK conversion in Phase 6).
- For a multicopter that starts on the ground, the mission should begin
  with a takeoff item; PX4 will reject an attempt to run a mission
  starting with a waypoint while disarmed and landed.
- A mission should have a clear termination: land at a location, or
  return-to-launch. A mission that just stops after the last waypoint
  with no termination command leaves the vehicle's behavior at the last
  waypoint ambiguous, which is why Phase 5's rule validator requires a
  terminating action.

## Altitude limits used by this project

There is no single universal PX4-enforced altitude ceiling out of the
box — it depends on vehicle configuration and local regulation. This
project applies its **own** configured ceiling/floor
(`MIN_ALTITUDE_M` / `MAX_ALTITUDE_M` in `.env`, defaulting to 2m and
120m) as a safety policy layered on top of whatever PX4 itself allows,
because:

- 120 m above ground level is the common recreational/commercial drone
  altitude ceiling used as a reference point in many countries' drone
  regulations (India's Civil Aviation Requirements and the US Part 107
  both use 400 ft ≈ 122 m as a widely-cited ceiling for uncontrolled
  airspace) — treat this as a sensible project-level default, not a
  guarantee of legal compliance for your specific location, which you
  should verify separately.
- A minimum altitude (this project defaults to 2 m) avoids waypoints
  that are effectively at ground level, which are usually a sign of a
  unit mix-up (e.g. meters vs. feet) in the LLM's output rather than an
  intentional low-altitude mission.

## Geofence

PX4 supports a geofence (a maximum radius from home and/or a maximum
altitude, or a polygon) that will cause a configured failsafe action
(typically RTL or hold) if breached. This project's Phase 5 rule
validator checks generated waypoints against a configured safe operating
area *before* the mission ever reaches PX4, so a mission that would
breach geofence limits is rejected at the validation stage rather than
relying on PX4's in-flight failsafe to catch it.

## Acceptance radius

Each waypoint has an acceptance radius — how close (in meters) the
vehicle must get before PX4 considers the waypoint "reached" and moves
to the next item. Too small a radius on a mission with tight GPS
tolerance requirements can cause the vehicle to circle a waypoint
without ever formally reaching it; too large a radius can cause waypoints
to be skipped in tightly-spaced missions. This project uses a
conservative default and does not currently let the LLM set this value
directly — it is a deterministic default applied during mission
conversion (Phase 6).
