# Architecture & Compatibility Audit
## RAMR (Idea 1) + CV Inspection-Quality Feedback (Idea 5) against the Phase 1–8 implementation

This is an audit only — no new code is proposed here. Every claim below is backed by a specific file in the current codebase (`/home/claude/drone_mission_planner`, mirrored to your `drone_mission_planner` folder). Where the project brief you pasted describes something I could not find in the code, I've flagged it explicitly rather than assuming it exists.

---

## Step 1 — What Is Actually Implemented (evidence-based)

| File | What it does |
|---|---|
| `app/config.py` | Single `Settings` singleton (pydantic-settings) — every module reads config from here. |
| `app/rag/document_loader.py` | Reads `.md`/`.txt` files from `data/documents/`, splits into overlapping word-chunks. |
| `app/rag/embeddings.py` | Wraps `sentence-transformers` (`all-MiniLM-L6-v2` default), lazy-loaded singleton. |
| `app/rag/retriever.py` | Builds/queries a persistent ChromaDB collection; `retrieve(query, k)` returns ranked chunks; `format_context()` renders them for a prompt. |
| `app/llm/prompts.py` | `SYSTEM_PROMPT` (12 rules — allowed commands, per-command required fields, sequencing, no-fabrication) + `build_user_prompt()`. |
| `app/llm/llm_client.py` | `BaseLLMClient` → `GroqLLMClient` (default) / `OllamaLLMClient`, both using native JSON-schema-constrained structured output; retries on schema failure. |
| `app/mission/schema.py` | Pydantic `Mission`/`MissionItem` — 5 commands only (`TAKEOFF, WAYPOINT, LAND, RETURN_TO_LAUNCH, LOITER`), `extra="forbid"`, lat/lon/alt range checks. |
| `app/mission/validator.py` | Deterministic rule validator: sequence uniqueness/order, per-command required/forbidden fields, must-start-with-TAKEOFF, must-end-with-LAND/RTL, nothing after a terminator, Null Island warning. |
| `app/mission/generator.py` | `generate_mission()` (RAG→LLM→schema) and `generate_validated_mission()` (adds the rule-validator retry loop, feeding violations back to the LLM). |
| `app/mission/converter.py` | `to_mavsdk_mission_plan()` — converts our `Mission` into a real `mavsdk.mission.MissionPlan`, inferring TAKEOFF/LAND coordinates from neighboring waypoints or a supplied home position. |
| `app/mavlink/px4_client.py` | `run_mission_on_px4()` — connects to PX4 SITL, waits for preflight-ready, gets home position, converts, uploads, arms, starts, monitors `mission_progress()` to completion, returns accepted/rejected. |
| `app/database/`, `app/ui/` | **Empty** — only `__init__.py`. Nothing implemented. |
| `app/main.py` | Phase 1 environment check only; does not yet call the pipeline. |

**Important discrepancies between your pasted project description and the actual code** (called out now, per your instruction not to assume anything exists):

1. **"Historical Mission Memory" / "Mission History Storage" — not implemented at all.** `app/database/` contains only an empty `__init__.py`. There is no database connection, no ORM model, no write path anywhere in the codebase. Nothing stores a completed mission or its outcome today.
2. **"Building Inspection Waypoint Generation" (perimeter, 360°, standoff distance, inspection boundaries) — not implemented as a distinct module.** `app/llm/prompts.py`'s `SYSTEM_PROMPT` has no mention of perimeter geometry, standoff distance, or inspection boundaries. Waypoint placement today is *entirely* up to the LLM's free-text reasoning over RAG context — there is no deterministic geometry algorithm generating a 360° perimeter around a building footprint.
3. **"Web Dashboard" — not implemented.** `app/ui/` is empty.
4. **Real-time telemetry — only partially implemented, and not continuously.** `px4_client.py` subscribes to `telemetry.health()` and `telemetry.home()` only transiently, *before* arming, to confirm preflight readiness and get the home position — both generators are exited after their first yield. During the actual mission (after `start_mission()`), the only thing being watched is `mission.mission_progress()` (current/total waypoint index) and `mission.is_mission_finished()`. **`telemetry.position()`, `telemetry.battery()`, and `telemetry.flight_mode()` are never subscribed to during flight anywhere in the code.** There is no live GPS/altitude/battery stream today.
5. **"Battery requirements, battery reserve" as a safety-validation check — not implemented.** `app/mission/validator.py`'s rule list (re-read directly from the file) is: sequence uniqueness/order, per-command required/forbidden fields, start/end-command rules, Null Island warning. There is no battery-reserve estimate, no geofence check, and no inspection-boundary check anywhere in `validator.py`.
6. **Async concurrency exists but only sequentially, not in parallel.** `px4_client.py` is `asyncio`-based throughout, but every stage (`connect` → `preflight` → `upload` → `arm` → `start` → `monitor`) runs one after another via `await`. There is no `asyncio.gather()`/concurrent-task pattern anywhere — nothing currently runs two streams (e.g. telemetry + mission progress, or telemetry + camera) side by side.

None of this is a criticism of Phases 1–8 — those phases were scoped as "plan → validate → convert → execute once," and they do that solidly (61 tests passing, an honest MAVSDK-verified conversion layer, a real PX4 SITL round trip). It just means several things RAMR and the CV loop need (a live telemetry stream, a persistence layer, concurrent monitoring) are genuinely new, not "already there but I missed it."

---

## Step 2 — Component-by-Component Reuse Table

| Existing Phase/Component | Current Implementation | Reusable? | Modification Needed? | Role in New Novelty |
|---|---|---|---|---|
| RAG retrieval (`rag/retriever.py`) | Query → ranked chunks + `format_context()` | **Yes, directly** | None | Reused verbatim by the Replanning Context Builder for mid-flight re-prompts |
| LLM client (`llm/llm_client.py`) | Schema-constrained `generate_structured()` | **Yes, directly** | None | Reused verbatim by the Adaptive Mission Replanner — same call, new (delta) prompt |
| Prompts (`llm/prompts.py`) | Fixed system prompt + user-prompt builder | **Yes, as a base** | **Yes** — needs a second, replanning-specific prompt variant that explains delta context (current position, remaining waypoints, trigger reason) instead of a fresh request | Adaptive Replanner's context-to-prompt step |
| `Mission`/`MissionItem` schema (`mission/schema.py`) | 5-command Pydantic model | **Yes, directly** | None for RAMR's core loop. **Possibly extend** if corrective/re-shoot waypoints need a distinguishing flag (e.g. `is_corrective: bool`) for logging/dashboard purposes later | The revised mission plan RAMR produces is still a `Mission` — no new schema needed to represent it |
| Rule validator (`mission/validator.py`) | Sequencing/field/ordering rules | **Yes, directly, as a second pass** | None to reuse it; **new rules should be added** (battery reserve, geofence) since these were never implemented in Phase 5 despite being in your original concept | Re-validator step, called a second time on every replanned mission — exactly the report's "re-entrant loop through validation, not a bypass" |
| Converter (`mission/converter.py`) | `Mission` → `MissionPlan`, home-position-aware | **Yes, directly** | **Yes — see Step 5.** It currently has no concept of "start this converted plan mid-route" — it always builds item #1 as TAKEOFF. A replanned mission plan will need a variant (or a parameter) that does NOT start with TAKEOFF, since the drone is already airborne | Converts RAMR's revised waypoint list into a MAVSDK plan for re-upload |
| `run_mission_on_px4()` (`mavlink/px4_client.py`) | One-shot connect→upload→arm→start→monitor-to-completion | **Partially** | **Yes, substantial.** It currently returns only after the mission finishes or fails — there is no hook to intervene *during* `_monitor_mission()`, and no exposed way to reuse an already-connected `System` object for a second `upload_mission()` call mid-flight | Needs to be split into a long-lived connection/session object the Trigger Engine and Replanner can both act on concurrently with monitoring, rather than one linear function |
| Mission history / historical memory | **Not implemented** | N/A | N/A — **build new** | Needed for RAMR's Outcome Tagger and (later) failure-aware retrieval; also needed to log every replanning event for your panel demo |
| Web dashboard | **Not implemented** | N/A | N/A — **build new** | Where the trigger→replan→re-route story gets demonstrated |

Mapped against your Step 2 checklist specifically:

| Your checklist item | Status |
|---|---|
| 1. Real-time telemetry monitoring | **Partial** — the MAVSDK calls exist and are verified, but nothing subscribes continuously during flight today |
| 2. Event-driven triggers | **Missing** — no trigger/event concept anywhere in the code |
| 3. Current drone state collection | **Partial** — `telemetry.position()`/`battery()` are real, documented MAVSDK calls (verified in Phase 7/8 research) but unused past preflight |
| 4. Remaining mission tracking | **Missing** — `MissionProgress.current`/`.total` gives you a waypoint *index*, not a data structure of "what's left" |
| 5. Mission interruption or modification | **Missing** — no pause/resume call exists anywhere in the code (MAVSDK's `pause_mission()` is never invoked) |
| 6. Mid-flight mission updates | **Missing** — `upload_mission()` is called exactly once, before `arm()` |
| 7. Safety revalidation | **Reusable as-is** — `validate_mission()` is a pure function; calling it twice costs nothing |
| 8. Battery prediction for corrective routes | **Missing** — no battery logic exists in validator or anywhere else |
| 9. Camera feed integration | **Missing** — no camera/image code anywhere in the codebase |
| 10. CV image quality analysis | **Missing** |
| 11. Coverage tracking | **Missing** |
| 12. Corrective waypoint generation | **Missing** as a distinct concept, but **the machinery to generate any waypoint set already exists** (RAG+LLM+schema+validator) — it just needs a different prompt/trigger |
| 13. Mission history storage | **Missing** (see Step 1, point 1) |
| 14. Storing replanning events | **Missing** — no schema, no table |
| 15. Storing CV quality failures | **Missing** |
| 16. Async/concurrent processing | **Partial** — `asyncio` is used throughout, but only sequentially; no concurrent-stream pattern exists yet |

---

## Step 3 — Compatibility Audit Against Your Target Architecture

```
INITIAL MISSION → AI MISSION PLANNER → WAYPOINT GENERATOR → SAFETY VALIDATOR → MAVSDK/PX4 → DRONE FLIGHT
                                                                                                    │
                                                                       ┌────────────────────────────┴──────────────────────────┐
                                                                       ▼                                                        ▼
                                                              TELEMETRY STREAM                                          CAMERA STREAM
                                                                       │                                                        │
                                                                       ▼                                                        ▼
                                                            TELEMETRY ANALYZER                                     CV QUALITY ANALYZER
                                                                       │                                                        │
                                                                       └───────────────────────┬────────────────────────────────┘
                                                                                                 ▼
                                                                                         TRIGGER ENGINE
                                                                                                 │
                                                                                                 ▼
                                                                                REAL-TIME ADAPTIVE MISSION REPLANNER
                                                                                                 │
                                                                                                 ▼
                                                                                       UPDATED MISSION PLAN
                                                                                                 │
                                                                                                 ▼
                                                                                         SAFETY VALIDATOR
                                                                                                 │
                                                                                                 ▼
                                                                                              MAVSDK
                                                                                                 │
                                                                                                 ▼
                                                                                               PX4
```

| Connection | Status |
|---|---|
| Initial mission → AI Mission Planner | **Already implemented** (`generator.generate_mission`) |
| AI Mission Planner → Waypoint Generator | **Already implemented, but collapsed into one step** — there is no separate "waypoint generator" module; the LLM emits `MissionItem`s directly. This is fine for RAMR/CV as-is (see Step 6) |
| Waypoint Generator → Safety Validator | **Already implemented** (`generator.generate_validated_mission`) |
| Safety Validator → MAVSDK/PX4 | **Already implemented** (`converter.to_mavsdk_mission_plan` → `px4_client.run_mission_on_px4`) |
| MAVSDK/PX4 → Drone Flight | **Already implemented** (arm/start, verified against PX4 SITL semantics) |
| Drone Flight → Telemetry Stream | **Partially implemented** — the MAVSDK calls (`telemetry.position()`, `telemetry.battery()`) are real and were verified during Phase 7/8, but nothing currently subscribes to them past preflight |
| Drone Flight → Camera Stream | **Missing entirely** |
| Telemetry Stream → Telemetry Analyzer | **Missing** — no analyzer/threshold logic exists |
| Camera Stream → CV Quality Analyzer | **Missing entirely** |
| Telemetry Analyzer + CV Quality Analyzer → Trigger Engine | **Missing entirely** — no event bus, no trigger concept |
| Trigger Engine → Real-Time Adaptive Mission Replanner | **Missing entirely** |
| Replanner → Updated Mission Plan | **Partially implemented** — the *generation* machinery (RAG+LLM+schema) exists and is reusable, but nothing today builds a "delta" prompt from live drone state, and nothing builds a mission plan that starts mid-route rather than at TAKEOFF |
| Updated Mission Plan → Safety Validator | **Reusable as-is** — `validate_mission()` doesn't care where a `Mission` came from |
| Safety Validator → MAVSDK (second pass) | **Missing the re-upload mechanism** — `upload_mission()` has only ever been called once, before arming, anywhere in this codebase; calling it again mid-flight is unverified against your actual code (though verified against MAVSDK docs — see Step 5) |
| MAVSDK → PX4 (mid-flight) | **Needs refactoring** — `run_mission_on_px4()`'s current shape (connect→run-to-completion→return) has no seam for a second upload while it's still monitoring |

**Bottom line for Step 3: the "plan once, execute once" half of this diagram is solid and fully verified. Everything from "Telemetry Stream" downward on the right-hand branch, and the entire left-hand Camera Stream branch, is new work — none of it exists today, partially or otherwise, except for the raw MAVSDK/Telemetry API calls being real and already verified in Phase 7/8's research.**

---

## Step 4 — Gap Analysis

### A. Required for RAMR

- **Trigger Engine.** Nothing in the code currently evaluates "is this deviation significant enough to replan." Needed because right now the *only* thing that reacts to conditions during flight is PX4's own built-in failsafes (low-battery RTL, geofence breach) — exactly the "scripted failsafe, not reasoning" gap your novelty document identified.
- **Drone/Mission State Manager.** `MissionProgress` only gives `(current_index, total)`. There's no structure holding "which waypoints are done, which remain, what was the original plan, what's the drone's live position/battery right now" — RAMR's context-builder needs all of that together.
- **Replanning Context Builder (delta-context serializer).** `build_user_prompt()` today always builds a *fresh* prompt from a NL request + RAG context. It has no notion of "here's the original plan, here's what's been flown, here's why we're replanning, here's the current position" — a new, second prompt-building function is needed (reusing the RAG/LLM call itself, per Step 2).
- **Re-validator entry point.** Technically already reusable (`validate_mission()`), but there's no orchestration code today that calls it a *second* time on a revised mission and decides "abort/RTH-fallback" vs. "proceed" — that orchestration doesn't exist.
- **Mission-diff/merge + mid-flight MAVSDK update mechanism.** This is the single riskiest gap — see Step 5. Nothing in `converter.py` or `px4_client.py` handles "the drone is mid-route, build a plan that continues from here" or "pause, re-upload, resume without restarting from waypoint 1."
- **Battery reserve checking.** Your original concept lists this as a Phase 5 validator responsibility; it was never actually implemented (Step 1, point 5). RAMR's "check whether sufficient battery remains" step has nothing to call today — this needs to be built, not just wired in.

### B. Required for Computer Vision Feedback

- **Camera stream access.** Nothing in the codebase touches a camera — not PX4 SITL's simulated camera, not any image source at all. In Gazebo SITL this means adding a camera plugin to the simulated vehicle model and a way to pull frames from it (e.g. a Gazebo camera topic) — currently zero infrastructure for this exists.
- **Image capture pipeline.** Needed to turn a raw frame into something the quality analyzer can score, and to associate a frame with *which waypoint* it was taken at (for coverage tracking and for "which waypoint needs a re-shoot").
- **Blur/exposure detection.** Not implemented — needs a new, small module (classical Laplacian-variance blur scoring is the standard low-cost approach, per the novelty document).
- **Coverage-gap detector.** Needs to compare captured-frame waypoints against the planned inspection perimeter — but as Step 1 point 2 notes, there currently *is no* deterministic perimeter/inspection-boundary concept in this codebase to compare against. This is a real, non-trivial prerequisite gap: the CV loop's "coverage gap" detection has nothing to measure coverage *against* yet.
- **CV event generator.** Needs to turn a detector's output into a structured event (`IMAGE_BLUR_DETECTED`, `COVERAGE_GAP_DETECTED`) the Trigger Engine can consume — this event shape doesn't exist anywhere yet.

### C. Required for Integration

- **Event bus / shared trigger interface.** The architecture diagram you specified explicitly merges telemetry-sourced and CV-sourced triggers into one Trigger Engine — this requires a common event shape and a place both sources publish to. Doesn't exist.
- **Async concurrent processing.** As found in Step 1 point 6, everything today is sequential `await`-chained. Watching telemetry AND a camera feed AND mission progress simultaneously, all while a mission continues flying, requires an `asyncio.gather()`/task-based pattern that doesn't exist anywhere in this codebase yet.
- **Shared drone/mission state.** Both the Telemetry Analyzer and the CV Analyzer need to reference "what waypoint are we on / what's the plan" — this state currently lives nowhere (it's local to the one-shot `run_mission_on_px4()` call and discarded when it returns).
- **Replanning event + CV-failure logging.** Both depend on the mission-history persistence layer that, per Step 1 point 1, does not exist at all yet.

---

## Step 5 — MAVSDK/PX4 Feasibility (researched, not assumed)

You explicitly asked me not to assume anything here, so this section is based on checking current MAVSDK-Python source/docs and PX4's own flight-mode documentation, on top of what's already verified in `px4_client.py`'s own docstring from Phase 7/8.

**1. How the current mission is uploaded today:** `drone.mission.upload_mission(mission_plan)`, called exactly once, before `drone.action.arm()`. Confirmed directly from `px4_client.py`.

**2. How mission progress is tracked today:** `drone.mission.mission_progress()` (an async generator yielding `(current, total)`), consumed until `drone.mission.is_mission_finished()` returns `True`. No other telemetry is read during the mission today.

**3. Does the current implementation support pausing, clearing, uploading a revised mission, and continuing? No — none of `pause_mission()`, `clear_mission()`, or a second `upload_mission()` call appear anywhere in the current code.** They exist as documented MAVSDK plugin methods (verified against the MAVSDK-Python mission plugin's method list during Phase 6/7/8 research), but nothing calls them.

**4. What exactly happens when a mission is modified while the drone is flying:** This is the critical, previously-unverified finding. Per PX4's own Mission Mode documentation:

> "A mission can be uploaded while the vehicle is paused, in which case the current active mission item is set to 1."
> "The mission will only reset when the vehicle is disarmed or when a new mission is uploaded."

**This means `upload_mission()` always replaces the entire mission and always resets PX4's internal "current item" pointer to item 1 of the newly uploaded plan — there is no partial/patch upload.** PX4 has no concept of "replace items 5 onward, keep items 1–4." This directly confirms the exact risk your novelty document already flagged ("the implementation must carefully handle the current-item offset so the drone does not restart from waypoint zero") — it is a real constraint, not a hypothetical one.

The practical consequence: **a revised mission plan must be built as "a fresh mission whose own item 1 is wherever the drone should go next"** — i.e. the mission-diff/merge component cannot simply append corrective waypoints to the *original* mission's item list and re-upload that; it must construct a *new* `MissionPlan` containing only the remaining/corrected route, with its own sequence starting at 1. `converter.to_mavsdk_mission_plan()` as it exists today always assumes item 1 is `TAKEOFF` — it has no mode for "the drone is already airborne, don't add a TAKEOFF item, start from wherever we are."

**5. Does MAVSDK allow your planned approach?** Yes, with the correction above. The documented, supported sequence is:
```
pause_mission()   -- vehicle holds/loiters at current position
upload_mission(new_plan)   -- current item resets to 1 of new_plan
start_mission()   -- same call used for both "start" and "resume" (confirmed in MAVSDK's own missions guide: "Start or resume a paused mission using Mission::start_mission")
```
There is also a documented `set_current_mission_item(index)` method (verified against the MAVSDK-Python source in Phase 6). A plausible alternative strategy — upload a merged plan containing *both* completed and remaining items, then call `set_current_mission_item()` to jump straight to the right index rather than re-flying completed waypoints — is technically supported by the API surface, but I could not find documentation confirming PX4 accepts an arbitrary jump target safely in every case, so I'm flagging it as a *possible* alternative, not a verified one. **The safer, fully-documented strategy is: build the new plan as "remaining route only," starting at item 1.**

**6. Safest realistic implementation strategy for PX4 SITL, given all of the above:**
```
1. Trigger fires
2. pause_mission()                         -- vehicle loiters, doesn't keep flying the old plan
3. Read current position/battery from telemetry
4. Build a NEW Mission (schema) containing only: remaining/corrected waypoints,
   ending in the same terminator (LAND/RTL) the original mission had
5. validate_mission() on that new Mission (re-entrant through the same validator)
6. If invalid: fall back to a safe default (e.g. skip straight to RETURN_TO_LAUNCH) rather than retrying indefinitely
7. to_mavsdk_mission_plan() on the new Mission -- but converter.py needs a new
   parameter/mode: "don't require/insert a TAKEOFF item, this mission starts airborne"
8. upload_mission(new_plan)                -- current item resets to 1, which is correct here
   because item 1 of new_plan IS "where the drone should go next"
9. start_mission()                          -- resumes flight on the new plan
```

**Limitations to design around, stated plainly:**
- No partial mission patch exists — every replan is a full mission replacement. Latency-sensitive triggers (the report's own "replanning latency" metric) must budget for a full RAG+LLM+validate+convert+upload round trip, not an incremental update.
- The vehicle must be paused (not disarmed, not mid-waypoint-transition) before the new upload is safe and well-defined, per the documentation quoted above.
- `converter.py` genuinely needs a new code path (not just a parameter tweak) for "mission that doesn't start with TAKEOFF" — its current TAKEOFF-coordinate-inference logic (Step 6 below) assumes item 1 is always TAKEOFF.

---

## Step 6 — Minimum Refactoring Required

Following "extend, don't rebuild":

**Stays completely unchanged:**
- `app/rag/*` (document loading, embeddings, retrieval) — reused as-is by the Replanning Context Builder.
- `app/llm/llm_client.py` — reused as-is; same `generate_structured()` call, different prompt.
- `app/mission/schema.py` — the `Mission`/`MissionItem` model already represents anything RAMR or the CV loop would produce; no changes needed to represent a corrective waypoint (it's just another `WAYPOINT` item).
- `app/mission/validator.py` — reused as-is for its existing rules; new rules (battery, geofence) are *additions*, not modifications to what's there.

**Gets wrapped (new code around it, old code untouched):**
- `run_mission_on_px4()` — instead of modifying this function's internals, it should become one *path* inside a longer-lived session object that also exposes `pause()`, `replan_and_resume()`, etc. The existing function's logic (connect → preflight → upload → arm → start) is still exactly right for the *initial* mission — it just needs to hand off its live `drone` (`System`) object afterward instead of only returning a final result.

**Gets extended (new optional capability, old behavior preserved):**
- `app/mission/converter.py` — needs a new mode/function for "convert a mission that starts mid-flight" (no TAKEOFF item, current position stands in for what TAKEOFF's coordinate inference does today). The existing `to_mavsdk_mission_plan()` keeps working exactly as-is for initial missions.
- `app/llm/prompts.py` — needs a second prompt-builder (`build_replanning_prompt()` or similar) alongside the existing `build_user_prompt()`, not a rewrite of it.
- `app/mission/validator.py` — needs new rule functions (battery reserve, geofence) added to the existing rule set, following the same `_check_*(items, errors)` pattern already established there.

**Needs new modules entirely (nothing to extend, nothing existed):**
- Trigger Engine, Drone/Mission State Manager, Replanning Context Builder, Mission-diff/merge, CV pipeline (capture, blur/exposure detection, coverage tracking), database/persistence layer, event bus/async orchestration.

**Nothing needs to be refactored destructively.** There is no part of Phases 1–8 that is actively wrong for the novelty's purposes — the gaps are additive, not corrective, with the one caveat above about `converter.py` needing a genuinely new code path (not just a parameter) for mid-flight missions.

---

## Step 7 — Proposed Backend Architecture (design only, not code)

| # | Module | Responsibility | Inputs | Outputs | Existing code reused | New code required | Runs |
|---|---|---|---|---|---|---|---|
| 1 | Drone State Manager | Hold the live snapshot: position, battery, flight mode, armed state | `telemetry.position()`, `.battery()`, `.flight_mode()` streams | A shared, always-current state object | The MAVSDK calls themselves (verified, unused today) | The subscription loop + shared state object | Continuously, for the mission's duration |
| 2 | Mission State Manager | Track original plan, completed items, remaining items | The uploaded `Mission`, `mission_progress()` stream | "Remaining route" data structure | `Mission`/`MissionItem` schema | State-tracking logic | Continuously |
| 3 | Telemetry Monitor | Watch Drone State for trigger-worthy deviations (battery, missed waypoint) | Drone State Manager | Raw candidate trigger events | None directly — new thresholds | Threshold/rule logic | Continuously |
| 4 | Camera/Image Stream Manager | Pull frames from PX4 SITL's simulated camera, tag each with the waypoint it was captured at | Gazebo camera topic/plugin | Tagged frames | None — entirely new | Camera plugin config + frame-pull code | Continuously during inspection legs |
| 5 | CV Inspection Quality Analyzer | Score each frame for blur/exposure; compare captured coverage against the planned perimeter | Tagged frames, Mission State Manager | `IMAGE_BLUR_DETECTED` / `COVERAGE_GAP_DETECTED` events | None | Blur/exposure scoring, coverage comparison | Per frame (near-real-time) |
| 6 | Trigger Engine | Merge Telemetry Monitor + CV Analyzer events into one decision: replan or not | Events from #3 and #5 | A single structured trigger with a reason code | None | Event merging + de-dup/debounce logic | Continuously, event-driven |
| 7 | Adaptive Mission Replanner | Orchestrate the whole replan: pause, build context, call LLM, get revised `Mission` | Trigger, Drone State, Mission State | A candidate revised `Mission` | `generate_structured()` (LLM client), RAG retrieval | Orchestration + delta-prompt construction | On demand, per trigger |
| 8 | Replanning Context Builder | Turn live state + trigger reason into a prompt | Drone State, Mission State, trigger reason | A prompt string | `format_context()`, `build_user_prompt()` as a template | New `build_replanning_prompt()` | On demand |
| 9 | Corrective Waypoint Generator | For CV triggers specifically, decide where a re-shoot waypoint goes | CV event (which waypoint/area), Mission State | Additional/modified `MissionItem`s | `MissionItem` schema | Insertion/reordering logic | On demand, CV-triggered only |
| 10 | Safety Revalidator | Re-run rule validation on the revised mission | Revised `Mission` | `ValidationResult` | `validate_mission()` | None (called as-is) | On demand |
| 11 | Battery Feasibility Checker | Confirm the revised route is flyable on remaining battery | Revised `Mission`, current battery | Go/no-go | None | New estimator (even a simple linear one is fine to start) | On demand |
| 12 | MAVSDK Mission Update Manager | `pause_mission()` → convert (mid-flight mode) → `upload_mission()` → `start_mission()` | Revised + validated `Mission` | Upload success/failure | `to_mavsdk_mission_plan()` as a base | New mid-flight-start conversion mode + pause/resume orchestration | On demand |
| 13 | Replanning Event Logger | Record what happened and why | Trigger, revised mission, outcome | A persisted row | None | New persistence layer (Phase 10 scope) | On demand |
| 14 | Mission Learning/History Storage | Store completed missions + outcomes | Final mission state | Persisted history | None | New persistence layer | Once per mission |

## Step 8 — Event Flow

**IMAGE_BLUR_DETECTED:**
```
Camera frame -> CV Analyzer -> IMAGE_BLUR_DETECTED (waypoint id, severity)
  -> Trigger Engine -> Adaptive Replanner
  -> Context Builder (current position, remaining route, "revisit waypoint N — blurry")
  -> LLM (RAG-grounded) -> candidate revised Mission (adds/reorders a re-shoot waypoint)
  -> Safety Revalidator -> Battery Feasibility Checker
  -> if both pass: MAVSDK Mission Update Manager (pause -> convert -> upload -> resume)
  -> if either fails: fall back to continuing the original remaining route (log the failed attempt), or RTH if the failure itself is safety-relevant
  -> Replanning Event Logger records outcome either way
```

**COVERAGE_GAP_DETECTED:** same shape as above, trigger reason is a missed inspection-boundary segment rather than one blurry frame; the Corrective Waypoint Generator computes what segment needs (re-)covering against the Mission State Manager's planned-perimeter record.

**BATTERY_DEVIATION:** Telemetry Monitor (not CV) fires the trigger; the revised mission is much more likely to end early with RETURN_TO_LAUNCH/LAND rather than adding waypoints — Battery Feasibility Checker's role here is dominant.

**MISSED_WAYPOINT:** Telemetry Monitor detects the vehicle passed a waypoint's acceptance radius without triggering `mission_progress()` advancing as expected (or a timeout); replanning either re-inserts the missed waypoint or accepts the miss and continues, depending on remaining battery and mission priority — this needs a policy decision from you and your teammate, not something I'd assume.

**SAFETY_EVENT:** Any hard safety violation (Safety Revalidator failure, geofence breach) short-circuits straight to RETURN_TO_LAUNCH rather than attempting another replan cycle — no retry loop, per the "safety validation must be non-negotiable and fast" note in your own novelty document.

## Step 9 — Recommended Implementation Order

| Phase | Objective | Existing code reused | New components | Test criteria | Risk |
|---|---|---|---|---|---|
| A | Audit (this document) | — | — | You and your teammate agree on scope | Low |
| B | Continuous telemetry + Drone/Mission State Manager | `telemetry.position/battery/flight_mode` (verified, unused) | State manager, subscription loop | Live state visible/loggable throughout a normal mission | Low |
| C | Minimal persistence layer (lean Phase 10) | None | SQLite schema for missions + replanning events | A completed mission and a manually-logged "event" round-trip through the DB | Low |
| D | Trigger Engine + one manual/simple trigger (e.g. a debug command, not yet real telemetry-driven) | State Manager | Trigger Engine skeleton | Firing the debug trigger reaches the Replanner | Low-Medium |
| E | Mid-flight converter mode + pause/reupload/resume in PX4 SITL | `converter.py`, `px4_client.py` connection | New converter mode, MAVSDK Mission Update Manager | Manually trigger a replan mid-SITL-flight; drone pauses, receives a new (even trivial) plan, resumes without restarting from waypoint 1 | **High — this is the riskiest phase, isolate and prove it before anything else** |
| F | Real telemetry-driven triggers (battery deviation, missed waypoint) + battery-reserve validator rule | Trigger Engine (D), Validator | Threshold logic, new validator rule | Injected battery drain in SITL produces a real (not manual) trigger and a safe response | Medium |
| G | Camera integration in Gazebo SITL | None | Camera plugin, frame capture pipeline | Frames are retrievable and waypoint-tagged | Medium |
| H | Blur/exposure detection | Frames (G) | Laplacian-variance or similar scorer | Synthetic blurry frame reliably scores low | Low-Medium |
| I | Coverage-gap detection + corrective waypoint generation | Mission State Manager, blur detector pattern | Coverage comparator, corrective waypoint logic | A deliberately-skipped area is detected and a re-shoot waypoint is proposed | Medium-High (depends on Step 4's "no perimeter concept exists yet" gap being resolved first) |
| J | CV-triggered replanning (wire G-I into the Trigger Engine from D-E) + full baseline-vs-adaptive evaluation | Everything above | Final integration glue, evaluation harness | The report's own evaluation methodology (20–30 trials per disturbance, baseline vs. adaptive) runs and produces real numbers | Medium |

---

## Step 10 — Final Verdict

1. **Can your current implementation up to Phase 8 support this novelty? Yes, as a foundation — but it is a foundation, not most of the work.** Roughly 45–50% of what a working RAMR+CV prototype needs already exists and is verified (RAG, LLM client, schema, rule validator, mission→MAVSDK conversion, a working PX4 SITL connection and one-shot execution). The other half — everything to the right of "Drone Flight" in your target diagram, plus persistence — is new.

2. **Directly reusable, no changes:** RAG retrieval, LLM client, `Mission`/`MissionItem` schema, and the existing validator rules.

3. **Requires modification (not rebuild):** `converter.py` (new mid-flight-start mode), `prompts.py` (new replanning prompt), `px4_client.py` (needs to expose a long-lived session rather than a single run-to-completion call), `validator.py` (new rules added alongside old ones).

4. **Completely missing:** Trigger Engine, telemetry-during-flight subscription/analysis, camera/CV pipeline entirely, mission/drone state management, mission-diff/merge logic, the mid-flight pause→reupload→resume mechanism, any database/persistence layer, event bus, and all Streamlit UI.

5. **Is anything fundamentally incompatible?** No. The one real constraint — PX4 always resets to item 1 on upload, no partial patching — is a design constraint to build around, not a blocker; the recommended strategy in Step 5 (pause → build remaining-route-only plan → validate → convert with a new mid-flight mode → upload → resume) works within it cleanly.

6. **Major refactoring or extensions?** Extensions. Nothing in Phases 1–8 needs to be torn out or rewritten; `converter.py` needs a genuinely new code path (see Step 6), everything else needs wrapping or additive changes.

7. **Minimum work for a working prototype:** (a) a battery-reserve rule in the validator (small), (b) continuous telemetry subscription during flight (small-medium — the MAVSDK calls are already verified, just unused), (c) a Trigger Engine that can fire on at least one real condition (medium), (d) the mid-flight-start converter mode + pause/reupload/resume sequence (medium-large — this is the technically riskiest piece, per Step 5), (e) a minimal persistence layer for logging replanning events (small-medium). The camera/CV half (Idea 5) is a separate, parallel track of comparable size that can be built and tested independently before wiring it in as a second trigger source, exactly as your architecture principle describes.

8. **What to implement first:** Telemetry-during-flight subscription and a minimal Drone/Mission State Manager, then a Trigger Engine with one simple rule-based trigger (not ML), then the pause→build-remaining-route→revalidate→reupload→resume sequence end-to-end in PX4 SITL with a *manually* injected trigger (no CV yet) — get that whole loop proven safe and working first, since it's both the highest-risk piece (Step 5) and the piece the CV loop plugs into afterward, not the other way around.
