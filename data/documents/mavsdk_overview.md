# MAVSDK Overview (for this project)

High-level context for how this project uses MAVSDK. Exact class/method
signatures for the installed `mavsdk` version are verified directly
against the SDK when Phase 6 (mission conversion) and Phase 7-8 (PX4 SITL
connection and mission upload) are implemented, rather than assumed here
— MAVSDK-Python has moved major versions before, and this document is
deliberately conceptual so it doesn't go stale the way a hard-coded code
sample would.

## What MAVSDK is, in this pipeline

MAVSDK is the layer between this project's own Python mission
representation and the MAVLink protocol that PX4 actually speaks.
Nothing in `app/mission/` (schema, generator, validator, converter)
imports MAVSDK — that module tree only knows about this project's own
mission format. Only `app/mavlink/` talks to MAVSDK, and only after a
mission has already passed schema and rule validation. This separation
is deliberate: it means the LLM and the deterministic validator can be
tested completely offline, with no PX4/MAVSDK/SITL running at all, and
only the final upload step needs a live connection.

## Mission plugin, conceptually

MAVSDK exposes a mission-related plugin that accepts an ordered
collection of mission items (each carrying the same kind of data this
project's own schema already captures: command type, coordinates,
altitude, and a few per-command parameters) and can upload that
collection to a connected vehicle, then start it.

## Connection, conceptually

MAVSDK connects to a vehicle (real or PX4 SITL) over a MAVLink-carrying
transport -- typically UDP for SITL. The connection process is
asynchronous: the app requests a connection, then waits for the system
to report that a vehicle is actually present and its telemetry is
flowing, before it is safe to do anything mission-related.

## Why the LLM never produces MAVSDK/MAVLink objects directly

The LLM's structured JSON output (Phase 3) uses this project's own
command names (`TAKEOFF`, `WAYPOINT`, `LAND`, `RETURN_TO_LAUNCH`,
`LOITER`) and field names, not MAVSDK's or MAVLink's. The deterministic
converter (Phase 6) is the only code that maps from this project's
command names to whatever the installed MAVSDK version's mission API
actually expects. If the LLM produced MAVSDK objects (or raw MAVLink
messages) directly, an LLM mistake could reach the flight controller
with no deterministic check in between; routing everything through this
project's own schema first means every LLM output is validated in a
library-independent format before any MAVSDK-specific code ever runs.
