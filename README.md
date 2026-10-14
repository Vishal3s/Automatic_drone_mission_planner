# Autonomous AI Drone Mission Planner

An AI pipeline that turns a natural-language mission request into a
validated, PX4-SITL-testable drone mission:

```
Natural Language Request -> RAG -> LLM -> Structured Mission JSON
    -> Schema Validation -> Rule Validation -> Mission Conversion
    -> MAVSDK/MAVLink -> PX4 SITL -> Accepted / Rejected
```

Being built phase by phase. **This README currently documents Phase 1
(environment setup) only** — it will grow with each phase.

## Project structure

```
drone_mission_planner/
├── app/
│   ├── main.py              # entry point; will orchestrate the full pipeline
│   ├── config.py            # central settings, loaded from .env (all phases read this)
│   ├── rag/                 # Phase 2 — document loading, embeddings, retrieval
│   ├── llm/                 # Phase 3 — LLM client + prompts
│   ├── mission/              # Phase 4-6 — schema, generator, validator, converter
│   ├── mavlink/              # Phase 7-8 — PX4 SITL connection, mission upload/test
│   ├── database/             # Phase 10 — mission logging
│   └── ui/                   # Phase 11 — Streamlit app
├── data/
│   └── documents/            # Phase 2 knowledge-base source files
├── tests/                    # one test file per phase
├── requirements.txt
├── .env.example               # copy to .env
└── README.md
```

One deliberate change from the structure you sketched: `app/config.py`
was added. It's a single Pydantic `Settings` object that every later
module imports (`from app.config import settings`) instead of each file
calling `os.getenv(...)` separately. Reasoning is in the docstring at the
top of that file.

## Phase 1 — Environment Setup

### Prerequisites

- Python 3.11 or 3.12 (3.13 also works; avoid 3.9/3.10 — some Phase 2/6
  dependencies need 3.11+)
- Git (optional, for version control)
- Windows: PowerShell (commands below use PowerShell syntax)

### 1. Create and activate a virtual environment

**Windows (PowerShell):**
```powershell
cd drone_mission_planner
python -m venv venv
.\venv\Scripts\Activate.ps1
```
If activation is blocked by execution policy, run once as your user:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

**macOS / Linux:**
```bash
cd drone_mission_planner
python3 -m venv venv
source venv/bin/activate
```

You'll know it worked because your prompt gets a `(venv)` prefix.

### 2. Install dependencies

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` currently lists dependencies for **all** phases (RAG,
LLM, MAVSDK, Streamlit, etc.), pinned to specific versions, so the
environment is stable as we add phases — you install once now and won't
need to touch this step again until a later phase calls for something
new. Phase 1 itself only actually uses `python-dotenv`,
`pydantic`, and `pydantic-settings`; the rest (chromadb,
sentence-transformers, mavsdk, streamlit, ...) install now but stay
unused until their phase.

> Note: `sentence-transformers` pulls in PyTorch, so this install can
> take a few minutes and a few GB of disk on first run. That's expected.

### 3. Configure environment variables

```powershell
copy .env.example .env
```
(macOS/Linux: `cp .env.example .env`)

Open `.env` and review it — the defaults are fine to start (Ollama as
the LLM provider, PX4 SITL's default MAVSDK port, etc.). You don't need
Ollama installed yet for Phase 1; that arrives in Phase 3.

### 4. Run the Phase 1 entry point

```powershell
python -m app.main
```

Run it as a module (`python -m app.main`), not `python app/main.py` —
otherwise Python won't resolve the `app.config` import correctly.

### Expected output

```
HH:MM:SS [INFO] drone_mission_planner: Drone Mission Planner -- Phase 1 environment check
HH:MM:SS [INFO] drone_mission_planner: Project root: <your path>\drone_mission_planner
HH:MM:SS [INFO] drone_mission_planner: Python: 3.11.x
HH:MM:SS [INFO] drone_mission_planner: All Phase 1 checks passed.
HH:MM:SS [INFO] drone_mission_planner: Current configuration (from .env / defaults):
HH:MM:SS [INFO] drone_mission_planner:   LLM_PROVIDER        = ollama
HH:MM:SS [INFO] drone_mission_planner:   LLM_MODEL           = llama3.1:8b
HH:MM:SS [INFO] drone_mission_planner:   EMBEDDING_MODEL     = sentence-transformers/all-MiniLM-L6-v2
HH:MM:SS [INFO] drone_mission_planner:   CHROMA_PERSIST_DIR  = <your path>\drone_mission_planner\data\chroma_db
HH:MM:SS [INFO] drone_mission_planner:   PX4_CONNECTION_URL  = udp://:14540
HH:MM:SS [INFO] drone_mission_planner:   DATABASE_URL        = sqlite:///./data/missions.db
HH:MM:SS [INFO] drone_mission_planner: Phase 1 complete: ...
```
Exit code `0` means every check passed.

### 5. Run the Phase 1 tests

```powershell
pytest tests/test_phase1_environment.py -v
```

Expected: 5 tests, all `PASSED`. These only check that settings load and
the folder structure is intact — nothing LLM/RAG/PX4-related is tested
yet.

## Common Phase 1 errors

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'app'` | Ran `python app/main.py` directly, or venv not activated | Use `python -m app.main`; confirm `(venv)` is in your prompt |
| `.env not found` warning in the log | You skipped step 3 | `copy .env.example .env` |
| `pip install` fails building `sentence-transformers`/torch | Very old pip, or Python <3.9 | `pip install --upgrade pip` first; confirm `python --version` is 3.11+ |
| PowerShell: `cannot be loaded because running scripts is disabled` | Execution policy blocks venv activation | Run the `Set-ExecutionPolicy` command above once |
| `pytest: command not found` | venv not activated, or step 2 skipped | Re-activate the venv, re-run `pip install -r requirements.txt` |
| `pip install -r requirements.txt` fails on a specific pin (`No version of X==Y`) | That package shipped a newer release and the old pin no longer exists on PyPI | Check `pypi.org/project/<name>` for the current version, edit that one line in `requirements.txt`, reinstall. This happened once already with `mavsdk` — see git history / the pinned versions' comment at the top of the file. |

## Phase 2 — RAG System

Knowledge base (`data/documents/*.md`) → chunking → embeddings
(`sentence-transformers`) → Chroma vector store → `retrieve(query)`.

Five knowledge-base documents ship with the project:
`mavlink_commands.md`, `px4_mission_requirements.md`,
`waypoints_and_sequencing.md`, `safety_constraints.md`,
`mavsdk_overview.md` — covering exactly the command set, sequencing
rules, and safety constraints Phase 4/5's schema and validator will
enforce, so RAG and validation stay consistent with each other by
construction, not by coincidence.

### 1. Build the vector index

```powershell
python -m app.rag.retriever --build
```

This reads every file in `data/documents/`, chunks it, embeds every
chunk with the model named in `.env` (`EMBEDDING_MODEL`, default
`sentence-transformers/all-MiniLM-L6-v2`), and stores it in a Chroma
collection persisted at `CHROMA_PERSIST_DIR` (default
`./data/chroma_db/`, git-ignored). **First run downloads the embedding
model (~90 MB) from Hugging Face** — needs network access once; after
that it's cached locally and loads offline.

Expected output:
```
INFO: Loading embedding model: sentence-transformers/all-MiniLM-L6-v2 ...
INFO: Embedding model loaded. Vector size: 384
INFO: Loaded 19 chunk(s) from 5 document(s) in ...\data\documents
INFO: Indexed 19 chunk(s) into Chroma collection 'drone_mission_docs' at ...
Indexed 19 chunks.
```

### 2. Try a retrieval query

```powershell
python -m app.rag.retriever "Take off and fly to three waypoints at 50 meters."
```

Expected: the top-`RAG_TOP_K` (default 4) most relevant chunks printed
with their source file, chunk index, and distance score — you should see
`mavlink_commands.md` (TAKEOFF/WAYPOINT) and
`waypoints_and_sequencing.md` (altitude/coordinate rules) ranked near
the top for this particular query.

### 3. Run the Phase 2 tests

```powershell
pytest tests/test_phase2_rag.py -v
```

The test file is split into two groups: plain document-loading/chunking
tests (no heavy dependencies, always run), and `@pytest.mark.heavy`
tests that load the real embedding model and build a real Chroma index
(slower, needs the model download above to have succeeded once). To run
only the fast group:

```powershell
pytest tests/test_phase2_rag.py -v -m "not heavy"
```

Expected: 7 fast tests pass instantly; the 4 `heavy` tests pass after a
noticeably longer first run (model load + embedding + index build).

## Common Phase 2 errors

| Symptom | Cause | Fix |
|---|---|---|
| `Chroma collection ... is empty` when querying | Skipped step 1 | Run `python -m app.rag.retriever --build` first |
| Very slow first run of `--build` or the `heavy` tests | Downloading the embedding model weights | Expected once; subsequent runs use the local Hugging Face cache and are fast |
| `FileNotFoundError: Knowledge-base folder does not exist` | Ran from the wrong directory | `cd` into `drone_mission_planner` (the folder containing `app/`) before running |
| Retrieval returns irrelevant chunks | Index is stale after editing `data/documents/*.md` | Re-run `python -m app.rag.retriever --build` (it rebuilds from scratch) |

## Phase 3 — LLM Mission Generation

```
user_request -> retrieve() [Phase 2] -> prompt -> LLM (schema-constrained) -> Mission
```

Two providers are implemented, switchable via `.env`'s `LLM_PROVIDER`
with no other code changes: **`groq`** (default — free hosted API, no
local RAM needed) and **`ollama`** (fully local/offline). Groq became
the default after `ollama` hit an out-of-memory error loading an 8B
model on this project's own test laptop — a hosted API sidesteps that
class of problem entirely, at the cost of needing internet access and a
free API key.

### Why a mission schema appears now, not in "Phase 4"

Both providers' structured-outputs features need an actual JSON schema
to constrain generation against, and the natural source of that schema
is a Pydantic model of the mission shape — which is literally what the
original Phase 4 asked for. So `app/mission/schema.py` (a `Mission` /
`MissionItem` Pydantic model, enum-restricted to the 5 allowed commands,
`extra="forbid"`, numeric fields range-checked against `.env`) landed in
this phase instead of being deferred. What it does **not** yet do —
per-command required fields, sequencing/termination order, geofence,
battery reserve — stays deferred to Phase 5's deterministic rule
validator, exactly as originally scoped; that phase will import this
same `Mission` model rather than redefine it.

### Why the LLM can't just invent a command

Two independent layers enforce this, not one:
1. **Structural**: `command` is a Pydantic `Enum` of exactly `TAKEOFF`,
   `WAYPOINT`, `LAND`, `RETURN_TO_LAUNCH`, `LOITER`. That enum becomes
   part of the JSON schema passed to the provider's structured-output
   parameter (Groq's `response_format`, Ollama's `format=`), so the
   model is *decoding-constrained* — it cannot emit a token sequence
   that produces a command outside this set, the same way it can't
   produce `"sequence": "one"` for an integer field.
2. **Prompt-level**: the system prompt (`app/llm/prompts.py`) also
   states the allowed commands and their required fields in plain
   language, as a second line of defense and to guide *which* of the
   allowed commands the model picks.

A subtler thing `app/llm/llm_client.py`'s `_strict_json_schema()` fixes:
Groq's (and OpenAI's) strict structured-output mode requires *every*
property to appear in `"required"`, even nullable ones like `latitude`
on a `TAKEOFF` item — plain Pydantic doesn't do this for `Optional`
fields by default, it would just be silently rejected or misbehave
without the fix. This is a documented, widely-hit gotcha, not something
specific to this project; `_strict_json_schema()` patches it
recursively (including inside `$defs`, where Pydantic nests
`MissionItem`) and is covered by
`test_strict_json_schema_forces_every_field_required` in the test file.

### Provider setup

**Option A — Groq (default):**

1. Get a free API key at [console.groq.com/keys](https://console.groq.com/keys).
2. In `.env`: `GROQ_API_KEY=<your key>`, `LLM_PROVIDER=groq`, `LLM_MODEL=openai/gpt-oss-20b` (already the default).
3. That's it — no local install, no model download, no RAM to worry about.

Model choice: `openai/gpt-oss-20b` is a free-tier, production-status
Groq model, fast, and the one Groq's own structured-outputs docs use as
their example. `llama-3.3-70b-versatile` is a stronger/slower free-tier
alternative if output quality matters more than speed. Groq's free tier
has a request-rate limit (check current numbers at
[console.groq.com/settings/limits](https://console.groq.com/settings/limits)) —
fine for interactive testing, something to watch once Phase 9's
automated test harness is firing many requests in a loop.

**Option B — Ollama (local/offline):**

```powershell
ollama pull qwen3:8b
```
Then in `.env`: `LLM_PROVIDER=ollama`, `LLM_MODEL=qwen3:8b`. `ollama
serve` usually auto-starts as a background service after install —
check with `ollama list`; if that also fails to connect, the service
isn't running (restart it via Windows Services, don't fight the port
from the command line). Needs ~6GB free RAM for the 8B model — drop to
`qwen3:4b` if that OOMs, per the earlier troubleshooting in this
project's history.

### Try the generator

```powershell
python -m app.mission.generator "Take off, fly to two waypoints at 50 meters, and return home."
```

Expected output: INFO logs showing retrieved RAG sources, then a JSON
mission printed to stdout, e.g.:
```json
{
  "mission_name": "Two Waypoint Survey",
  "mission_items": [
    {"sequence": 1, "command": "TAKEOFF", "latitude": null, "longitude": null, "altitude": 50.0, "hold_time_s": null},
    {"sequence": 2, "command": "WAYPOINT", "latitude": ..., "longitude": ..., "altitude": 50.0, "hold_time_s": null},
    {"sequence": 3, "command": "WAYPOINT", "latitude": ..., "longitude": ..., "altitude": 50.0, "hold_time_s": null},
    {"sequence": 4, "command": "RETURN_TO_LAUNCH", "latitude": null, "longitude": null, "altitude": null, "hold_time_s": null}
  ]
}
```
Exact coordinates will vary — the knowledge base doesn't contain real
building coordinates yet (that's for a later refinement of
`data/documents/`), so don't be surprised if the model picks
placeholder-looking numbers or keeps the mission to TAKEOFF/RTL only per
system-prompt rule 10.

### Run the Phase 3 tests

```powershell
pytest tests/test_phase3_llm.py -v
```

Fast group (`Mission`/`MissionItem` schema, `_strict_json_schema` — no
LLM call) always runs. The `@pytest.mark.live_llm` tests actually call
whichever provider `.env` currently points at; skip them if no provider
is configured/reachable where you're testing:
```powershell
pytest tests/test_phase3_llm.py -v -m "not live_llm"
```

## Common Phase 3 errors

| Symptom | Cause | Fix |
|---|---|---|
| `GROQ_API_KEY is not set in .env` | Forgot to add the key, or `LLM_PROVIDER=groq` with no key | Add `GROQ_API_KEY=...` from [console.groq.com/keys](https://console.groq.com/keys) |
| `Groq rejected the API key (401)` | Key is wrong/revoked | Generate a fresh key on the Groq console |
| `Groq free-tier rate limit hit (429)` | Too many requests too fast | Wait, or check current limits at console.groq.com/settings/limits |
| `Groq returned an error (status ...)` mentioning the model | Model id typo or Groq deprecated/renamed it | Check current ids at [console.groq.com/docs/models](https://console.groq.com/docs/models) |
| `Could not reach Ollama at http://localhost:11434` | `ollama serve` isn't running (Ollama provider only) | `ollama list` to check the background service; restart it if needed |
| `ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate` | Ollama ran out of RAM loading the model | Switch to a smaller model (`qwen3:4b`) or switch `LLM_PROVIDER` to `groq` |
| `LLM did not produce a schema-valid Mission after 3 attempts` | Model genuinely struggling with the schema | Check the logged raw output in the warning lines to see what it actually returned; this is rarer with `openai/gpt-oss-20b` on Groq than with a small local model |
| Generated mission has `null` coordinates for every waypoint | Knowledge base doesn't have real coordinates for the place you named | Expected for now — `data/documents/` doesn't yet contain a building/location gazetteer; the model is correctly refusing to fabricate coordinates per system-prompt rule 10 |

## Phase 4/5 — Deterministic Rule Validation

```
Mission (schema-valid, from Phase 3) -> validate_mission() -> {"valid": bool, "errors": [...], "warnings": [...]}
```

`app/mission/validator.py` is a **second, separate validation layer**
from `app/mission/schema.py`'s Pydantic model. The schema already
guarantees a `Mission` is *structurally* sound (allowed command names,
correct types, numeric fields in range) — but a structurally valid
mission can still be nonsensical or unsafe: it might never take off,
take off twice, keep issuing commands after landing, or send a WAYPOINT
with no coordinates. None of that is reliably expressible as a JSON
Schema a small LLM can satisfy, so `validate_mission()` re-checks an
already schema-valid `Mission` in plain Python — deterministically, with
no LLM involved, so the same `Mission` always produces the same result
regardless of which provider generated it (or if a human typed it by
hand).

### What it checks

**Errors** (mission is unsafe/invalid — must not reach PX4):
- Sequence numbers are unique and in ascending order.
- Per-command required fields are present — e.g. `WAYPOINT` needs
  `latitude`/`longitude`/`altitude`, `LOITER` additionally needs
  `hold_time_s`, `TAKEOFF` needs `altitude`.
- Per-command forbidden fields are absent — e.g. `LAND` and
  `RETURN_TO_LAUNCH` shouldn't carry coordinates or altitude.
- The mission starts with `TAKEOFF` (exactly once — a second `TAKEOFF`
  is an error).
- The mission ends with `RETURN_TO_LAUNCH` or `LAND`.
- Nothing follows a terminating command (`RETURN_TO_LAUNCH`/`LAND`).

**Warnings** (suspicious, but doesn't fail the mission):
- A waypoint at `(0, 0)` — "Null Island" — almost always a placeholder,
  not a real destination.

**Deliberately still out of scope** (needs information this function
doesn't have — a real geofence polygon, a battery model, a takeoff
point to measure distance from): geofence radius against a real
boundary, battery-reserve/endurance estimates, distance-based
feasibility. Left for a later phase or for PX4/MAVSDK to enforce at
upload/execution time.

### The full pipeline: `generate_validated_mission()`

`app/mission/generator.py` now exposes two functions:
- `generate_mission()` — unchanged from Phase 3 (RAG + LLM, schema-valid
  `Mission` only, no rule validation). Kept for backward compatibility
  with existing callers/tests.
- `generate_validated_mission()` — the full pipeline matching the
  original spec's diagram (`LLM Output -> Schema Validation -> Rule
  Validation -> Valid/Invalid`). If `validate_mission()` finds errors,
  they're fed back to the LLM (same self-correction pattern
  `llm_client.py` already uses for schema failures) and it's asked for a
  corrected mission, up to `max_validation_attempts` times (default 2).
  It returns a `MissionGenerationResult(mission, validation, attempts)`
  and does **not** raise on a rule-invalid mission — that's a normal,
  expected outcome the caller checks via `.accepted` /
  `.validation.valid`. It still raises `LLMGenerationError` if the LLM
  itself is unreachable or can't produce a schema-valid response at all.

### Try it

```powershell
python -m app.mission.generator "Fly to a waypoint at 50 meters and land."
```

Expected output: the generated mission's JSON, followed by the
validation result, e.g.:
```json
{
  "valid": true,
  "errors": [],
  "warnings": []
}
```
If the LLM's first attempt breaks a rule, you'll see an `Attempt 1/2`
log line listing the errors, then a second attempt — this is expected
self-correction, not a bug.

### Run the Phase 5 tests

```powershell
pytest tests/test_phase5_validation.py tests/test_phase5_generator_integration.py -v
```

Both files are fully dependency-free (pure Pydantic/dataclass logic, no
LLM or RAG calls) — `test_phase5_validation.py` unit-tests every rule in
`validate_mission()` directly by constructing `Mission` objects by hand;
`test_phase5_generator_integration.py` tests `generate_validated_mission()`'s
retry-on-rule-violation orchestration with a fake LLM client (via
`monkeypatch`) so the retry logic is verified without needing a real
provider or the real RAG index.

## Common Phase 4/5 errors

| Symptom | Cause | Fix |
|---|---|---|
| `Mission must start with TAKEOFF, but the first item (#1) is WAYPOINT.` | LLM produced a mission that skips takeoff | Expected — this is exactly what the rule validator is for. If it happens every time for a given request, check the request phrasing / system prompt rules in `app/llm/prompts.py` |
| Mission still invalid after all validation attempts | The LLM couldn't self-correct within `max_validation_attempts` | Check `result.validation.errors` for what's still wrong; raise `max_validation_attempts`, or inspect the raw LLM output via the INFO logs |
| `AssertionError` about `mission is not None` (shouldn't happen) | Would only happen if `max_validation_attempts < 1` | Don't pass 0 or a negative number — the loop always needs at least one attempt |

## Phase 6 — Mission Conversion (MAVSDK)

```
Mission (schema-valid + rule-valid) -> to_mavsdk_mission_plan() -> ConvertedMission { plan: MissionPlan, return_to_launch_after_mission: bool }
```

`app/mission/converter.py` turns our own `Mission` model into the exact
shape MAVSDK's `mission` plugin needs to upload to PX4:
`mavsdk.mission.MissionPlan`, built from `mavsdk.mission.MissionItem`
objects. This phase involved checking MAVSDK's *current* (3.17.2, same
version pinned in `requirements.txt`) docs and source before writing
anything, and it surfaced three real gotchas worth understanding before
touching this file:

**1. `MissionItem`'s constructor is 14 positional arguments, verified
against the [MAVSDK-Python source](https://github.com/mavlink/MAVSDK-Python/blob/main/mavsdk/mission.py)
and the [official mission example](https://github.com/mavlink/MAVSDK-Python/blob/main/examples/mission.py):**
`latitude_deg, longitude_deg, relative_altitude_m, speed_m_s,
is_fly_through, gimbal_pitch_deg, gimbal_yaw_deg, camera_action,
loiter_time_s, camera_photo_interval_s, acceptance_radius_m, yaw_deg,
camera_photo_distance_m, vehicle_action`. Fields this project doesn't
use get `float("nan")` — the exact sentinel the official example itself
uses for "use the PX4 default", not a made-up placeholder.

**2. TAKEOFF/LAND need coordinates MAVSDK-side, even though our own
schema forbids them.** Per the [C++ API reference](https://mavsdk.mavlink.io/main/en/cpp/api_reference/structmavsdk_1_1_mission_1_1_mission_item.html),
`VehicleAction.Takeoff` means "vehicle will takeoff **and go to defined
waypoint**" and `VehicleAction.Land` means "when a waypoint is reached
vehicle will land **at current position**" — i.e. both actions' own
coordinates matter, they're not throwaway values. But
`app/mission/schema.py` deliberately forbids `latitude`/`longitude` on
TAKEOFF/LAND (so the LLM is never asked to invent a coordinate for "just
go up" or "just come down"). The converter resolves this by *inferring*
the coordinate: TAKEOFF borrows the next mission item's coordinates
(searching forward), LAND borrows the previous item's (searching
backward). A degenerate `TAKEOFF, LAND` mission with nothing in between
has nothing to borrow — for that case (and only that case),
`to_mavsdk_mission_plan()` accepts optional `home_latitude_deg`/
`home_longitude_deg` and falls back to those, raising
`MissionConversionError` if neither a borrowed nor a supplied coordinate
exists. Phase 7 is expected to read the real home position from
`Telemetry.home()` and pass it in.

**3. RETURN_TO_LAUNCH has no `MissionItem` at all.** MAVSDK handles it
as mission-plugin *behavior* — `Mission.set_return_to_launch_after_mission(True)`,
called once after upload — not as an item in the plan. A `Mission`
ending in `RETURN_TO_LAUNCH` converts to a plan with no item for it, and
`ConvertedMission.return_to_launch_after_mission = True` tells the
Phase 7 uploader to make that call. A `Mission` ending in `LAND` instead
gets an explicit `Land`-action item and the flag is `False`.

**Also fixed while verifying this phase:** `PX4_CONNECTION_URL`'s
default was `udp://:14540`, a placeholder from Phase 1 that turned out
not to be a documented MAVSDK connection scheme. Current MAVSDK docs
want an explicit direction — `udpin://0.0.0.0:14540` ("listen on all
interfaces"), which is what PX4 SITL expects on its default
MAVSDK-facing port 14540. Updated in `app/config.py` and `.env.example`,
with the Phase 1 test (`test_settings_load_with_defaults`) updated to
match.

**Assumption:** our schema has no per-waypoint speed field (out of
Phase 3/4 scope), but `MissionItem.speed_m_s` is required. Every
converted item uses `MISSION_DEFAULT_SPEED_MS` from `.env` (default 5.0
m/s) — a reasonable cruise speed for a small simulated multicopter, not
a verified spec value. Change it in `.env` any time.

### Try it

```powershell
python -c "
from app.mission.schema import Mission, MissionItem, MissionCommand
from app.mission.converter import to_mavsdk_mission_plan

mission = Mission(
    mission_name='Converter smoke test',
    mission_items=[
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
        MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=13.0827, longitude=80.2707, altitude=30),
        MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
    ],
)
result = to_mavsdk_mission_plan(mission)
print('items:', len(result.plan.mission_items))
print('return_to_launch_after_mission:', result.return_to_launch_after_mission)
"
```
Needs the real `mavsdk` package installed (`pip install -r requirements.txt`)
since this one isn't run through the test fakes. Expected output:
```
items: 2
return_to_launch_after_mission: True
```

### Run the Phase 6 tests

```powershell
pytest tests/test_phase6_converter.py -v
```
These do **not** need the real `mavsdk` package or a PX4 connection — a
fake `mavsdk.mission` module is injected into `sys.modules` for the
duration of each test (see the test file's docstring), so they run
anywhere, fast, and still verify the real conversion logic (coordinate
inference, the RTL/LAND branch, exact field values).

## Common Phase 6 errors

| Symptom | Cause | Fix |
|---|---|---|
| `MissionConversionError: ... has no waypoint after it to borrow coordinates from` | A `TAKEOFF, LAND` mission with no waypoints in between, and no home position supplied | Pass `home_latitude_deg`/`home_longitude_deg` (Phase 7 will do this automatically from `Telemetry.home()`), or have the LLM include at least one `WAYPOINT`/`LOITER` |
| `ModuleNotFoundError: No module named 'mavsdk'` calling `to_mavsdk_mission_plan()` directly | `mavsdk` isn't installed in this environment | `pip install -r requirements.txt` (the Phase 6 tests don't need this — they fake the import) |
| `AssertionError` in a Phase 6 test about "expected 14 positional args" | MAVSDK's `MissionItem` constructor changed shape in a newer `mavsdk` release | Re-verify the current signature against the MAVSDK-Python source (link in the Phase 6 section above) before assuming this converter is wrong |

## Phase 7/8 — PX4 SITL Connection & Mission Execution

```
Mission (validated) -> connect to PX4 SITL -> preflight checks -> convert (Phase 6, using PX4's real home position)
                     -> upload -> arm -> start -> monitor -> accepted/rejected result
```

**⚠️ Simulation only.** `app/mavlink/px4_client.py` is written and tested
against PX4 **SITL** (a simulator) only, per this project's standing
safety constraint. `PX4_CONNECTION_URL` should always point at a
simulated vehicle — this code (and this README) gives no instructions
for connecting to real hardware.

### Setting up PX4 SITL (Windows)

PX4 SITL doesn't run natively on Windows — the currently-documented
path is **WSL2** ([docs.px4.io/main/en/dev_setup/dev_env_windows_wsl](https://docs.px4.io/main/en/dev_setup/dev_env_windows_wsl)):

```powershell
# In an elevated (Administrator) PowerShell/cmd:
wsl --install
# Reboot if prompted, then open a WSL2 Ubuntu shell:
wsl
```
```bash
# Inside the WSL2 Ubuntu shell:
cd ~
git clone https://github.com/PX4/PX4-Autopilot.git --recursive
bash ./PX4-Autopilot/Tools/setup/ubuntu.sh
# The setup script installs kernel/toolchain dependencies that need a
# fresh shell to take effect:
exit
```
```powershell
wsl --shutdown
```
```bash
wsl
cd ~/PX4-Autopilot
make px4_sitl gz_x500   # Gazebo, multicopter (x500) -- the current default
# or: make px4_sitl     # jMAVSim, lighter-weight, fine for this project's purposes
```
Wait for the PX4 shell prompt (`pxh>`) and log lines showing the EKF has
converged before connecting — that usually takes several seconds after
first launch.

**The networking gotcha (read before debugging a `connect` timeout):**
PX4's offboard-API port (14540, what `PX4_CONNECTION_URL` targets) is
opened **inside the WSL2 VM**, not automatically on Windows' own
`localhost`. Two ways to reach it from this project running on Windows:
1. **Simplest — run this project inside the same WSL2 Ubuntu environment**
   as PX4 SITL (a second WSL2 terminal, same distro). Both sides then
   share `localhost`, and the default `.env` value
   (`udpin://0.0.0.0:14540`) just works. This is what the Phase 7/8
   section below assumes.
2. If you'd rather run this project natively on Windows, enable WSL2's
   **mirrored networking mode** (`.wslconfig` → `[wsl2]` →
   `networkingMode=mirrored`, Windows 11 23H2+) so `localhost` is
   actually shared between Windows and WSL2 — then the same default
   `.env` value works from Windows too. Without mirrored networking,
   you'd otherwise have to find WSL2's dynamic IP (`ip addr show eth0`
   inside WSL2) and put that in `PX4_CONNECTION_URL` instead of
   `0.0.0.0`, and redo it every time WSL2 restarts (it's a dynamic IP)
   — the PX4 docs note this exact caveat for QGroundControl, and it
   applies here too.

### Try it

With PX4 SITL running and showing `pxh>`:
```powershell
python -m app.mavlink.px4_client "Take off, fly to one waypoint at 30 meters, and return home."
```
This runs the FULL pipeline: RAG retrieval → LLM generation → schema
validation → rule validation → MAVSDK conversion → PX4 SITL connect →
preflight checks → upload → arm → start → monitor. Expected output ends
with something like:
```
Stage: complete
Accepted: True
Final progress: 2/2
```
Watch the PX4 SITL shell/Gazebo window — you should see the simulated
vehicle actually take off, fly to the waypoint, and return.

### Run the Phase 7/8 tests

```powershell
pytest tests/test_phase7_8_px4_client.py -v
```
These do **not** need PX4 SITL, `mavsdk`, or any network — like Phase
6's converter tests, they fake `mavsdk`/`mavsdk.action`/`mavsdk.mission`
at the `sys.modules` level with a `FakeSystem` shaped exactly like the
real MAVSDK plugin API, and cover the happy path plus one failure at
every stage (connect timeout, preflight timeout, upload rejected, arm
rejected, start rejected, mission-monitor timeout).

### Design notes

- **`run_mission_on_px4()` never raises for an expected rejection** — a
  connection timeout, a PX4-rejected mission upload, a failed arm, a
  stalled mission — it returns a `MissionExecutionResult(accepted,
  stage, error, progress_log, return_to_launch_after_mission)` instead.
  Same philosophy as Phase 5's `generate_validated_mission()`: a
  rejected SITL run is a normal outcome later phases (Phase 9's test
  harness, Phase 11's UI) need to display, not a crash.
- Every `asyncio.wait_for(...)` timeout exists because MAVSDK's
  connection/telemetry streams are async generators with no built-in
  timeout of their own — left unbounded, a PX4 SITL that never starts
  would hang this function forever rather than failing cleanly.
- `MissionError`/`ActionError` (raised by the mission/action plugins on
  failure) were verified against the current MAVSDK-Python source, not
  assumed — see the exact class definitions linked in
  `px4_client.py`'s docstring.

## Common Phase 7/8 errors

| Symptom | Cause | Fix |
|---|---|---|
| `Timed out ... waiting for PX4 SITL to connect` | PX4 SITL isn't running, or is running somewhere this process can't reach (the WSL2/Windows networking gotcha above) | Confirm `pxh>` is showing in the PX4 SITL shell; check `PX4_CONNECTION_URL` against where you're running this project (same WSL2 shell vs. Windows-native) |
| `PX4 SITL connected, but did not report a valid global+home position` | Checked too soon after SITL startup — the simulated EKF/GPS needs a few seconds to converge | Wait a bit after `pxh>` appears before running the script; rerun if it still times out |
| Mission `upload_mission()` fails mentioning "no landing defined" | A fixed-wing-specific PX4 mission-parsing rule (`MAV_CMD_DO_LAND_START` sequencing) — shouldn't affect this project's default multicopter (`gz_x500`) target | If you switch the SITL target to a fixed-wing airframe, see [this PX4 forum thread](https://discuss.px4.io/t/cannot-upload-mavsdk-python-mission-due-to-no-landing-defined/29562) — out of scope for this project's default setup |
| `arm()` fails / `ActionError` mentioning a pre-arm check | Normal PX4 safety behavior — e.g. GPS not settled yet, mission not uploaded, sensors not calibrated in this SITL instance | Check the PX4 SITL shell's own log output for the specific pre-arm failure reason (it's usually printed there in plain language) |
| Mission uploads and arms but never starts moving in Gazebo | Gazebo window not actually open/rendering, or a `gz_x500` vs `jmavsim` mismatch between what you built and what you're looking at | Confirm the Gazebo (or jMAVSim) simulator window is open and showing the vehicle; check which `make px4_sitl <target>` you actually ran |

## RAMR/CV Novelty — Phase 0: Pre-Implementation Audit

Before starting new work on top of Phase 1–8, the existing implementation was audited against the RAMR (Real-Time Adaptive Mission Replanner) + CV Inspection-Quality Feedback novelty and against a specific 22-phase implementation plan. That audit — architecture diagram, module dependency map, reusable/needs-extension/missing-component lists, MAVSDK/PX4 feasibility research (e.g. mission `upload_mission()` always resets the current item to 1 — no partial patch — which is why RAMR's mid-flight update needs pause → rebuild → revalidate → reconvert → upload → resume, not a live edit), and a proposed backend architecture — lives in `docs/RAMR_CV_Audit.md`. No implementation code was written until this audit was reviewed and confirmed.

## RAMR Novelty — Phase 1: Mission State Management

**What it is:** a centralized `MissionStateManager` matching the Phase 1 spec exactly — tracks `mission_id`, `original_mission`, `active_mission` (a *separate* field from the original, replaced wholesale once a replan is applied), `completed_waypoints`/`current_waypoint`/`remaining_waypoints`, `mission_status` (the full 10-state lifecycle: `CREATED → VALIDATED → UPLOADED → RUNNING → REPLANNING → UPDATING → RUNNING → ... → COMPLETED/FAILED/ABORTED`, plus `RETURNING_HOME`), a separate `replanning_status` (`NONE`/`TRIGGERED`/`IN_PROGRESS`/`APPLIED`/`FAILED`), `adaptation_count`, and `mission_version`. Provides the required getter API (`get_current_waypoint()`, `get_completed_waypoints()`, `get_remaining_waypoints()`, `get_active_mission()`, `get_original_mission()`) so the backend can always answer "where is the drone in the mission?" and "what waypoints remain?"

This is a rewrite of an earlier, differently-shaped first version of this module (built against my own inferred design before the exact spec was given) — that version is fully replaced, not layered on top of.

**Files created/rewritten:**
- `app/mission/state.py` — `MissionStatus` (10 states), `ReplanningStatus`, `MissionState` dataclass, `MissionStateManager` class, `MissionStateError`. Pure in-memory bookkeeping — no MAVSDK/asyncio/I/O dependency, same isolation property as `validator.py`. Also keeps a `remaining_route()` helper (not part of the spec, kept as scaffolding for the future Adaptive Mission Replanner phase).
- `tests/test_phase_mission_state.py` — 20 unit tests covering every state transition (including invalid ones), the full replanning loop (`start_replanning → begin_update → apply_replan`, and the failure/rollback path via `fail_replan`), progress tracking, and every getter.

**Files modified (additive only):**
- `app/mavlink/px4_client.py` — unchanged from the previous phase; the optional `on_progress` callback added there still has the same `(current, total) -> None` signature, so `MissionStateManager.record_progress` still plugs into it directly.
- `tests/test_phase7_8_px4_client.py` — updated the one integration test added in the first version of this phase to walk through the manager's actual lifecycle (`start → mark_validated → mark_uploaded → mark_running`) before recording progress, matching the real state machine.

**Existing components reused (unchanged):** `app/mission/schema.py`'s `Mission`/`MissionItem`; the fake-MAVSDK test harness in `test_phase7_8_px4_client.py`; every Phase 1–8 file.

**New functionality added:** the full mission-state lifecycle your spec defines, a working replanning loop (trigger → in-progress → applied-or-failed, with `mission_version`/`adaptation_count` tracked correctly and rollback-on-failure verified), and the five required getters.

**How to run it:**
```bash
python -c "
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager

mission = Mission(mission_name='Demo', mission_items=[
    MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
    MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=2.0, altitude=30),
    MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
])
manager = MissionStateManager()
manager.start(mission)
manager.mark_validated(); manager.mark_uploaded(); manager.mark_running()
manager.record_progress(current=2, total=3)
print('current waypoint:', manager.get_current_waypoint())
print('remaining:', manager.get_remaining_waypoints())
print(manager.snapshot().as_dict())
"
```
Or wire it into a live PX4 SITL run: `run_mission_on_px4(mission, on_progress=manager.record_progress)` (after `mark_running()`).

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_mission_state.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 20/20 new tests pass; full suite (93 tests total) passes.

**Known limitations:** tracks exactly one mission at a time (matches `run_mission_on_px4()`'s own single-mission-per-call reality); nothing persists this state to disk yet (that's later persistence-layer scope, not this phase); nothing actually calls `start_replanning()`/`apply_replan()` from a live trigger yet — the replanning loop is implemented and tested, but the Trigger Engine and Adaptive Mission Replanner that would call it don't exist yet (later phases); the simulated-mission test (spec's own test requirement — "verify that mission state updates correctly as waypoint progress changes") is covered by `test_record_progress_splits_completed_remaining_and_sets_current_waypoint` and `test_getters_match_snapshot_after_progress`, not yet by a live PX4 SITL run from this environment (no SITL reachable here).

## RAMR Novelty — Phase 2: Drone State Management

**What it is:** one centralized, real-time `DroneStateManager` matching the Phase 2 spec exactly — tracks latitude/longitude/altitude, battery percentage, velocity (raw NED + a derived ground-speed scalar), heading, flight mode, armed state, current waypoint, mission progress, and a timestamp, fed by concurrently consuming MAVSDK's `telemetry.position()/.battery()/.velocity_ned()/.heading()/.flight_mode()/.armed()` and `mission.mission_progress()` streams. This is the shared source of truth every future module (Trigger Engine, CV analyzer, Battery Feasibility Checker, etc.) is expected to read from via `manager.snapshot()` rather than subscribing to MAVSDK itself.

This extends (not replaces) the first version of this module, which already had the right shape (`DroneState`/`DroneStateManager` pure state + one `subscribe_drone_state()` MAVSDK-facing coroutine) but was missing velocity, heading, current waypoint, and mission progress — those are additive fields/streams on top of what was already correct.

**Files extended:**
- `app/mavlink/drone_state.py` — added `velocity_north_m_s`/`velocity_east_m_s`/`velocity_down_m_s`, a derived `ground_speed_m_s` (horizontal magnitude — arithmetic on values already received, not a new stream), `heading_deg`, `current_waypoint`, and `mission_progress` to `DroneState`; added `update_velocity()`, `update_heading()`, `update_mission_progress()` to `DroneStateManager`; added `_watch_velocity`, `_watch_heading`, `_watch_mission_progress` to `subscribe_drone_state()`. Everything from the previous version (position/battery/flight_mode/armed) is unchanged.
- `tests/test_phase_drone_state.py` — extended with tests for every new field/update method, plus a `subscribe_drone_state()` test covering all 7 streams together, and a test proving `mission_progress`/`current_waypoint` correctly stay `None` (not a fabricated `0`) when no mission is running yet — matches real PX4 behavior (`mission_progress()` produces nothing until a mission starts).

**Files modified:** none outside `app/mavlink/drone_state.py`/its test file. `app/mavlink/px4_client.py` is still untouched — see the module's docstring for why `_monitor_mission()`'s own direct `mission_progress()` consumption is a deliberate, documented exception to the "one shared subscriber" rule (it's pre-existing Phase 7/8 execution logic, not a new module) rather than an oversight.

**Existing components reused (unchanged):** `Position`/`Battery`/`FlightMode`/`armed()` field names and behavior from the first version of this phase; velocity/heading field names (`VelocityNed.north_m_s/east_m_s/down_m_s`, `Heading.heading_deg`) freshly verified against the current `MAVSDK-Proto` `telemetry.proto` (RPC `SubscribeVelocityNed`/`SubscribeHeading`, mirrored by the Python plugin as `velocity_ned()`/`heading()`) rather than assumed; `mission_progress()`'s `(current, total)` semantics reused unchanged from `px4_client.py`'s `_monitor_mission()`.

**New functionality added:** the complete field list your spec asked for, all funneling through one `DroneStateManager` as the shared source of truth.

**How to run it (spec's own test: print/expose current drone state while PX4 SITL is running):**
```bash
python -m app.mavlink.drone_state 10   # streams live telemetry for 10s against a running PX4 SITL, printing DroneState.as_dict() every second
```
Or embed it in your own script:
```python
manager = DroneStateManager()
task = asyncio.create_task(subscribe_drone_state(drone, manager))
...
print(manager.snapshot().as_dict())   # -> lat/lon/altitude/battery/velocity/heading/flight_mode/armed/current_waypoint/mission_progress/timestamp
task.cancel()
```

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_drone_state.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 17/17 tests in this file pass; full suite (97 tests total) passes.

**Known limitations:** `subscribe_drone_state()` and `run_mission_on_px4()` each open their own `System()` connection when run standalone — they aren't literally sharing one MAVSDK session yet during an active mission (unifying them needs the "longer-lived session object" wrapper flagged as later, separate work — Step 9's Phase E); no Trigger Engine reads this state yet (that's a later phase); `current_waypoint`/`mission_progress` will stay `None` unless a mission is actually running at the same time telemetry is being observed (verified by test, matches real PX4 behavior — not a bug); not exercised against a live PX4 SITL instance from this sandbox — you should run `python -m app.mavlink.drone_state` yourself (ideally while a mission from `python -m app.mavlink.px4_client` is running on a second terminal) to see `current_waypoint`/`mission_progress` populate with real values.

## RAMR Novelty — Phase 3: Event and Trigger Infrastructure

**What it is:** a common event-driven trigger architecture — `ReplanningEvent` (one event shape every trigger source uses), an `EventBus` (synchronous pub/sub dispatcher), and a `TriggerEngine` that decides, per event type + severity, whether adaptive replanning should be activated, producing an `AdaptiveReplanningRequest` when it is. Matches the spec's own test exactly: publish `BATTERY_DEVIATION` → `TriggerEngine` → `AdaptiveReplanningRequest`.

**Files created (new package `app/trigger/`):**
- `app/trigger/events.py` — `EventType` (all 8 types from the spec: `BATTERY_DEVIATION`, `MISSED_WAYPOINT`, `MISSION_DEVIATION`, `SAFETY_EVENT`, `OPERATOR_REQUEST`, `IMAGE_BLUR_DETECTED`, `BAD_EXPOSURE_DETECTED`, `COVERAGE_GAP_DETECTED`), `EventSeverity` (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`, not explicitly named in the spec but needed for the TriggerEngine's decision — see the module docstring), `EventSource` (`TELEMETRY`/`COMPUTER_VISION`/`OPERATOR`/`SAFETY_MONITOR`), and `ReplanningEvent` — the common event model with exactly the fields specified (event_id, mission_id, event_type, severity, timestamp, drone_state, mission_state, metadata, source).
- `app/trigger/bus.py` — `EventBus`: `subscribe(callback, event_type=None)` (type-specific or wildcard), `publish(event)` (never raises — one bad subscriber can't block the others), `history()`.
- `app/trigger/engine.py` — `TriggerEngine` (`handle_event()`, `decision_log()`, optional `on_replan_requested` callback), `AdaptiveReplanningRequest`, `TriggerDecision`, `DEFAULT_ACTIVATION_THRESHOLDS`, and a `_demo_cli()` that runs the spec's own manual test.
- `tests/test_phase_trigger.py` — 25 tests: event model/severity-ordering, the bus (type-specific delivery, wildcard delivery, multiple subscribers, exception isolation, history), the engine (below/at/above threshold, unrecognized event type, per-type defaults including `OPERATOR_REQUEST` always activating and `SAFETY_EVENT` requiring `HIGH`, custom threshold overrides, the callback, the decision log), and the full chain test matching the spec's TEST section verbatim.

**Files modified:** none. This is a self-contained new package — nothing in Phases 0–2 needed to change for events to exist; a future phase (telemetry threshold logic, or a CV module) will be what actually calls `bus.publish()`.

**Existing components reused:** `DroneState` (`app/mavlink/drone_state.py`) and `MissionState` (`app/mission/state.py`) are used AS-IS as the `drone_state`/`mission_state` fields on `ReplanningEvent` — no new "event snapshot" type was invented.

**New functionality added:** a working, tested decide-whether-to-replan pipeline, ready for real trigger sources (telemetry thresholds, a future CV module, an operator UI) to plug into by calling `bus.publish()` — none of which exist yet; per your instruction, no CV/camera code was added in this phase, only the event *types* CV will eventually publish.

**How to run it (spec's own test):**
```bash
python -m app.trigger.engine
```
Prints the published `BATTERY_DEVIATION` event, the `TriggerEngine`'s decision, and the resulting `AdaptiveReplanningRequest` — no PX4/LLM/network needed, fully offline.

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_trigger.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 25/25 new tests pass; full suite (118 tests total) passes.

**Known limitations:** nothing publishes real events yet — no telemetry-threshold logic watches `DroneStateManager` and calls `bus.publish()` on a real battery deviation, no CV module exists (by instruction), no operator UI exists. `DEFAULT_ACTIVATION_THRESHOLDS` are simple, documented placeholders (e.g. a real battery-reserve computation is separate, later scope), overridable per `TriggerEngine` instance. Nothing consumes `AdaptiveReplanningRequest` yet (the Adaptive Mission Replanner is a later phase) — this phase stops at producing a well-formed request. Fully verified via the offline demo/tests; nothing here touches MAVSDK/PX4 so there's no "live SITL" caveat for this phase.

## RAMR Novelty — Phase 4: Basic Telemetry-Based RAMR

**What it is:** `AdaptiveMissionReplanner`, a fully deterministic (no LLM) real-time replanner handling exactly the three "simple triggers" your spec named — `BATTERY_DEVIATION`, `MISSED_WAYPOINT`, `MISSION_DEVIATION` — and producing a `Modified Remaining Mission` for each. The battery handler implements your own worked example precisely: given the drone's current position, remaining waypoints, battery, and minimum reserve, it walks the remaining route waypoint-by-waypoint, checking at each step whether (distance so far + distance home from there) still fits inside the battery budget, and stops as soon as it doesn't — producing `WP4 → RTH` when only one more waypoint is safely reachable, or `WP4 → WP5 → RTH` when the whole remaining route still fits, exactly the two outcomes your spec describes.

**Files created (new package `app/replanning/`):**
- `app/replanning/replanner.py` — `AdaptiveMissionReplanner` (`.replan(event, mission_state, drone_state, home_latitude_deg=, home_longitude_deg=)`), `RAMRResult`/`RAMRStatus` (`MODIFIED`/`UNCHANGED`/`UNSUPPORTED`/`UNHANDLED_EVENT_TYPE`), a haversine great-circle distance helper, and a `_demo_cli()` running your exact WP1..WP5-at-WP3 scenario end to end offline.
- `tests/test_phase_replanner.py` — 20 tests: both of the spec's own battery outcomes (trim to one waypoint / keep full route), immediate RTH when already below reserve, missing-data → `UNSUPPORTED` (position/battery/home each tested separately), custom threshold overrides, missed-waypoint dropping (including when the missed item is the last one), mission-deviation severity gating (HIGH/CRITICAL → RTH, LOW/MEDIUM → unchanged), confirmation that CV and other out-of-scope event types are correctly left `UNHANDLED_EVENT_TYPE` (not silently mishandled), and the full chain test matching the spec's SUCCESS CRITERIA.

**Files extended (additive only):**
- `app/mission/validator.py` — added `validate_remaining_mission(items)` and a private `_check_remaining_mission_ordering()` helper. This was a genuine, documented gap the Phase 0 audit had already flagged: the existing `validate_mission()` requires a mission to start with TAKEOFF, which is correct for a full ground-launch mission but wrong for a *remaining*, mid-flight sub-mission — the drone is already airborne. The new function reuses every other check (sequence numbers, per-command required/forbidden fields, must-end-in-terminator, Null Island) unchanged; `validate_mission()` itself was not touched, and all its existing tests still pass.
- `app/config.py` — added `min_battery_reserve_percent` (default 0.15) and `battery_consumption_percent_per_km` (default 0.02), documented as simple, overridable placeholder estimates, not a validated energy model.
- `tests/test_phase5_validation.py` — added 5 tests for `validate_remaining_mission()`, appended without changing any existing test.

**Existing components reused:** `MissionState`/`MissionStateManager` (Phase 1) for original/active mission and remaining waypoints; `DroneState` (Phase 2) for live position/battery; `ReplanningEvent`/`EventType`/`EventSeverity` (Phase 3) as the trigger input; `Mission`/`MissionItem` schema (Phase 3-4 original) for the output shape; the newly-extended validator for safety-checking the output before returning it.

**New functionality added:** a working, tested, fully offline decide-and-replan pipeline for the three basic triggers — genuinely new capability, not previously present anywhere in the project.

**How to run it (matches your spec's own example):**
```bash
python -m app.replanning.replanner
```
Prints the telemetry event, the RAMR status/reason, and the resulting modified remaining mission — output for the default demo values is exactly `WP4 → RTH` (one waypoint, then return), matching your first example.

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_replanner.py tests/test_phase5_validation.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 20/20 new replanner tests + 5/5 new validator tests pass; full suite (139 tests total) passes.

**Known limitations:** the battery-consumption model is a flat percent-per-km placeholder (documented in `app/config.py`), not a real flight-energy model — refining it (wind, payload, climb/descent cost) is explicitly out of scope for "basic telemetry-based RAMR." Home position isn't tracked by `DroneStateManager` yet, so `replan()` takes it as an explicit parameter rather than silently assuming one — `px4_client.py`'s existing `_get_home_position()` is the natural live source once wired up. `MISSED_WAYPOINT` requires the caller to supply `metadata["missed_sequence"]` — nothing yet automatically detects a missed waypoint from telemetry (that's a Trigger Engine/Telemetry Monitor wiring concern, not this phase's). `MISSION_DEVIATION` at LOW/MEDIUM severity deliberately takes no action (documented, not a bug) since a real corrective response needs reasoning this deterministic phase doesn't have — that's explicitly future, LLM-assisted scope. Nothing here uploads the modified mission back to PX4 yet (the mid-flight pause/reupload/resume mechanism is separate, flagged-as-high-risk later work per the Phase 0 audit). Fully verified via offline tests/demo; nothing in this phase touches MAVSDK/PX4 directly, so there's no "live SITL" caveat here.

## RAMR Novelty — Phase 5: Replanning Context Builder

**What it is:** `ReplanningContextBuilder`, which assembles a `ReplanningContext` — a focused snapshot for a replanning decision, exactly the fields your OUTPUT example lists: current drone position, current waypoint, completed waypoints, remaining waypoints, trigger reason, battery status, safety constraints, mission priority, and relevant historical information. Built from `CURRENT STATE + REMAINING MISSION + TRIGGER EVENT`, as instructed — it deliberately does NOT copy the full `original_mission`/`active_mission` objects onto the output context, only the derived, focused slice (current/completed/remaining waypoints) those missions already produce via `MissionState`.

**Files created:**
- `app/replanning/context_builder.py` — `ReplanningContextBuilder.build(event, mission_state, drone_state, *, safety_constraints=, mission_priority=, mission_history=)`, plus the supporting `ReplanningContext`, `DronePosition`, `BatteryStatus`, and `SafetyConstraints` dataclasses (`SafetyConstraints.from_settings()` reuses the exact same config values the validator and RAMR already enforce — `min_altitude_m`/`max_altitude_m`/`max_waypoints`/`min_battery_reserve_percent` — no separate/parallel safety numbers).
- `tests/test_phase_context_builder.py` — 14 tests: current/completed/remaining waypoints pulled correctly from `MissionState`, drone position capture, trigger-reason formatting (type/severity/source/metadata), battery sufficiency in all three states (above reserve / below reserve / unknown), default vs. overridden safety constraints, mission-priority default and override, mission-history filtering by `mission_id`, a dedicated test asserting `ReplanningContext` has NO `original_mission`/`active_mission` field (enforcing "do not rebuild the entire original mission context" as a real, checked constraint, not just a comment), and the full-chain test matching the spec's SUCCESS CRITERIA.

**Files modified:** none. Purely additive, reusing `MissionState` (Phase 1), `DroneState` (Phase 2), `ReplanningEvent` (Phase 3), and existing `settings` values — no changes to any earlier phase.

**Existing components reused:** `MissionState.current_waypoint`/`.completed_waypoints`/`.remaining_waypoints`/`.mission_id` (Phase 1) used directly, not recomputed; `DroneState.latitude_deg`/`.longitude_deg`/`.relative_altitude_m`/`.battery_remaining_percent`/`.battery_voltage_v` (Phase 2); `ReplanningEvent.event_type`/`.severity`/`.source`/`.metadata` (Phase 3) for the trigger reason; `settings.min_altitude_m`/`.max_altitude_m`/`.max_waypoints`/`.min_battery_reserve_percent` (existing config, extended in Phase 4) for default safety constraints.

**New functionality added:** a single, focused, reusable snapshot-building step that will sit between "a trigger fired" (Phase 3) and "compute a corrective route" (Phase 4's `AdaptiveMissionReplanner`, or a future LLM-assisted replanner) — not wired into either yet (see limitations).

**How to run it:**
```bash
python -c "
from app.mavlink.drone_state import DroneState
from app.mission.schema import Mission, MissionCommand, MissionItem
from app.mission.state import MissionStateManager
from app.replanning.context_builder import ReplanningContextBuilder
from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent

mission = Mission(mission_name='Demo', mission_items=[
    MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
    MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=2.0, altitude=30),
    MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
])
manager = MissionStateManager()
manager.start(mission); manager.mark_validated(); manager.mark_uploaded(); manager.mark_running()
manager.record_progress(current=2, total=3)

drone_state = DroneState(latitude_deg=1.0, longitude_deg=2.0, battery_remaining_percent=0.12)
event = ReplanningEvent(event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.HIGH, source=EventSource.TELEMETRY)

context = ReplanningContextBuilder().build(event, manager.snapshot(), drone_state)
print(context.as_dict())
"
```

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_context_builder.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 14/14 new tests pass; full suite (153 tests total) passes.

**Known limitations:** `mission_priority` has no real source anywhere in this project yet — it's an optional caller-supplied string defaulting to `"NORMAL"`, not derived from anything (honestly documented, not fabricated as if it meant something today). `relevant_history` has no real history store to query — no mission-history persistence exists yet (later, separate scope), so `mission_history` is an optional caller-supplied list the builder only filters, never generates. `geofence` on `SafetyConstraints` is `None` — no geofence polygon/radius exists anywhere in this project (a gap the Phase 0 audit already flagged; the battery-reserve half of that gap was closed in Phase 4). Not yet wired into `AdaptiveMissionReplanner` or the `TriggerEngine` — this phase builds the context object and stops there, as scoped.

## RAMR Novelty — Phase 6: RAG + LLM Adaptive Reasoning

**What it is:** `AdaptiveReasoningEngine`, which extends the EXISTING RAG + LLM system (no separate RAG system was created) to produce a structured, LLM-assisted replanning proposal in response to a trigger event. Implements the NEW FLOW exactly: `Trigger Event -> ReplanningContext -> Relevant RAG Retrieval -> LLM -> Structured Revised Mission Proposal`. The LLM receives every input your spec lists — trigger reason, current drone state, remaining mission, battery constraints, safety constraints, relevant RAG information, and historical mission information — all read directly off Phase 5's `ReplanningContext`. The output is `RevisedMissionProposal`: remaining waypoints, corrective waypoints, skipped waypoints, recommended action, reason, confidence — schema-constrained the same way `Mission` (Phase 3) is, so the LLM cannot emit a malformed shape. The LLM does NOT control the drone: this module never imports `app.mavlink.px4_client` or anything PX4/MAVSDK-facing — it only ever returns a proposal object.

**Files created:**
- `app/replanning/adaptive_reasoner.py` — `RevisedMissionProposal` (Pydantic model: `remaining_waypoints`, `corrective_waypoints`, `skipped_waypoints`, `recommended_action` (a closed `RecommendedAction` enum), `reason`, `confidence`), `RecommendedAction`, `ReasoningResult` (proposal + validation + attempts + accepted), and `AdaptiveReasoningEngine.propose(context, *, rag_k=, max_attempts=)`.
- `tests/test_phase_reasoner.py` — 14 tests: schema enforcement (confidence bounds, unknown `recommended_action` rejected), `proposed_route()` concatenation order, valid-on-first-try / invalid-then-valid-retry / still-invalid-after-all-attempts orchestration (mirroring `generator.py`'s existing retry-loop tests), a bare recommendation with no waypoints validating trivially, the retrieval query and prompts actually reaching the (faked) LLM client, prompt-content checks on the new `app/llm/prompts.py` additions, and a full-chain test matching the spec's TEST (a simulated `BATTERY_DEVIATION` event produces a valid structured proposal).

**Files modified:**
- `app/llm/prompts.py` — additive only: added `REPLANNING_SYSTEM_PROMPT` and `build_replanning_user_prompt(context, rag_context)` alongside the untouched Phase 3 `SYSTEM_PROMPT`/`build_user_prompt()`.

**Existing components reused (per your REUSE list, literally):**
- **RAG pipeline / vector database** — `app.rag.retriever.retrieve()` and `.format_context()` (Phase 2), the exact same functions `app/mission/generator.py` already calls for ground-launch mission generation. No second retriever, no second Chroma collection.
- **LLM client** — `app.llm.llm_client.get_llm_client()` / `.generate_structured()` (Phase 3), the same abstraction Phase 3's mission generation uses — switching `LLM_PROVIDER` in `.env` changes both identically.
- **Mission planning schema** — proposed waypoints are `MissionItem` (Phase 3/4), unchanged; `RevisedMissionProposal` wraps `MissionItem` lists rather than inventing a new waypoint shape.
- **`ReplanningContext`** (Phase 5) is the sole source of trigger reason, drone state, remaining mission, battery/safety constraints, and history — nothing is recomputed.
- **`validate_remaining_mission()`** (RAMR novelty Phase 4 addition to Phase 5's validator) rule-checks the proposed route (`corrective_waypoints + remaining_waypoints`, concatenated as one continuous sequence) before the proposal is reported `accepted` — the same "schema validation, then rule validation, with LLM self-correction feedback" defense-in-depth pipeline shape `generate_validated_mission()` already established, applied here to a replanning proposal instead of a fresh mission.

**New functionality added:** a complete RAG+LLM reasoning step that turns a `ReplanningContext` into a structured, rule-checked `RevisedMissionProposal` — schema-constrained generation (via the shared LLM client), an independent rule-violation retry loop (up to `MAX_REASONING_ATTEMPTS`, default 2) that feeds validator errors back to the LLM for self-correction, and a documented sequencing convention (corrective waypoints numbered first, then remaining waypoints, as one continuous route) enforced both in the system prompt and in validation.

**How to run it:**
```bash
python -m app.replanning.adaptive_reasoner
```
Needs the configured LLM provider reachable (Groq: `GROQ_API_KEY` in `.env` + internet; Ollama: `ollama serve` running with the model pulled — see the Phase 3 section above) and the RAG index built (`python -m app.rag.retriever --build`, if not already done). Simulates a `BATTERY_DEVIATION` event at waypoint 3 of 5 with battery at 10% and prints the resulting `RevisedMissionProposal` JSON plus its validation result.

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_reasoner.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 14/14 new tests pass; full suite (167 tests total) passes. All of `test_phase_reasoner.py` runs offline (RAG and the LLM client are faked) — no network, no Groq/Ollama, no Chroma index needed for the automated tests. `python -m app.replanning.adaptive_reasoner` (the manual demo) DOES need a real LLM provider and RAG index, same as `python -m app.mission.generator` already does.

**Known limitations:** the proposal is a RECOMMENDATION only — nothing in this project yet consumes `ReasoningResult.proposal` to actually update a `MissionState` or re-plan a live PX4 mission; that merge/apply step (and deciding how it interacts with Phase 4's deterministic `AdaptiveMissionReplanner` — e.g. LLM reasoning as a fallback when the deterministic engine returns `UNSUPPORTED`/`UNHANDLED_EVENT_TYPE`, or vice versa) is left for a later phase, as scoped. `corrective_waypoints` coordinates are only as good as what the LLM is told to ground them in (the remaining mission + RAG context) — there is no independent geometric check that a corrective waypoint is actually near the location it claims to correct. `relevant_history` is passed straight through Phase 5's builder, which still has no real mission-history store to query (documented there already). The rule-violation retry loop only re-validates the CONCATENATED route; it does not independently sanity-check that `skipped_waypoints` refer to sequence numbers that actually existed in the remaining mission the LLM was shown.

## RAMR Novelty — Phase 7: Replanning Safety Validation

**What it is:** `ReplanningSafetyValidator`, which reuses the EXISTING safety validator to check every adaptive mission change before it could ever reach MAVSDK. Implements the New Architecture exactly: `Replanning Event -> Revised Mission Proposal -> Waypoint Processing -> Existing Safety Validator -> Battery Feasibility Check -> Approved / Rejected`. Validates everything your VALIDATE list asks for — Altitude and Coordinates (already enforced by the `MissionItem` schema itself, not re-implemented), Speed (the single configured mission speed against a new safety ceiling), Geofence and Building boundaries (new `CircularGeofence`/`no_fly_zones` support added to Phase 5's previously-empty `SafetyConstraints.geofence` placeholder), Safety boundaries, Battery reserve and Return-to-home capability (reusing Phase 4's haversine/battery-budget model), and Corrective waypoint safety (action/waypoint-list consistency, plus a geographic "must be grounded near a known waypoint" check). If invalid, the adaptation is rejected and a deterministic (non-LLM) fallback action is chosen: continue the original mission if it's still safe, else return to home if that's still feasible, else abort.

**Files created:**
- `app/replanning/safety_validator.py` — `SafetyValidationStatus`, `FallbackAction`, `BatteryFeasibility`, `SafetyValidationResult`, `UnsafeMissionError`, `enforce_before_mavsdk()` (the hard gate — raises unless a result is `APPROVED`), and `ReplanningSafetyValidator.validate(proposal, context, mission_state, *, home_latitude_deg=, home_longitude_deg=)`.
- `tests/test_phase_safety_validator.py` — 21 tests: safe-proposal approval (with and without an explicit waypoint list), reuse of the existing rule validator (missing fields, a TAKEOFF mid-flight), the new speed ceiling, geofence in/out, no-fly-zone rejection, battery-reserve rejection and an unknown-data case that reports `None` rather than guessing, all four corrective-waypoint-safety checks (action/list consistency ×3, geographic grounding both failing and passing), all three fallback actions (continue original / return to home / abort), the `enforce_before_mavsdk()` gate in both directions, and a full-chain test matching the spec's SUCCESS CRITERIA.

**Files modified:**
- `app/replanning/context_builder.py` — additive: added `CircularGeofence` (a simple center+radius circle — not a real polygon engine) and a `no_fly_zones: list[CircularGeofence]` field on `SafetyConstraints`, filling in the `geofence` placeholder Phase 5 deliberately left as `None`. Both still default to unset/empty — nothing is fabricated.
- `app/config.py` — additive: `max_safe_speed_ms` (default 12.0 m/s), the ceiling Phase 7 checks the existing `mission_default_speed_ms` against (documented as an assumption, same honesty standard as `mission_default_speed_ms` itself).

**Existing components reused:** `app.mission.validator.validate_remaining_mission()` (Phase 4/5) is called completely unchanged for sequencing/field/ordering rules; `MissionItem`'s own Pydantic range constraints (Phase 3/4) for altitude/lat/lon, not re-checked; `RevisedMissionProposal.proposed_route()` (Phase 6) for waypoint processing; the haversine great-circle formula and `settings.min_battery_reserve_percent`/`.battery_consumption_percent_per_km` (Phase 4's `app/replanning/replanner.py`, imported directly rather than duplicated) for battery feasibility; `ReplanningContext` (Phase 5) for trigger/drone/battery/safety data; `MissionState.remaining_waypoints` (Phase 1) for the "original mission" the fallback logic compares against.

**New functionality added:** the safety gate between "an LLM (or any other source) proposed a change" and "MAVSDK could receive it" — a deterministic approve/reject decision with a full accounting of why, plus a deterministic fallback choice (no LLM in this decision, matching this project's standing safety-critical-actions rule) when rejected. `enforce_before_mavsdk()` is a concrete, testable choke point, not just a documented convention.

**How to run it:**
```bash
python -m app.replanning.safety_validator
```
Builds a mission, a mid-flight `MissionState`, a `ReplanningContext`, and a hand-built `RevisedMissionProposal` (no LLM/network needed), then prints the full `SafetyValidationResult` as JSON.

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_safety_validator.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 21/21 new tests pass; full suite (188 tests total) passes. All tests are pure Python, no network/LLM/PX4 needed.

**Known limitations:** `geofence`/`no_fly_zones` are simple circles, not real polygons — good enough to prove the validation pipeline works and to reject an obviously-out-of-bounds proposal, but not a substitute for a real GIS/building-footprint check a production system would need. Nothing in this project yet SUPPLIES a real geofence or building-boundary data source (both default to unset) — a future phase would need to add that data, not this validator's logic. `max_safe_speed_ms` checks the single project-wide `mission_default_speed_ms` (the schema still has no per-waypoint speed — see `app/mission/converter.py`), not a true per-segment speed profile. This module is not yet wired into `AdaptiveReasoningEngine` (Phase 6) or `AdaptiveMissionReplanner` (Phase 4) as an automatic post-processing step — it exists and is fully tested, but nothing calls it automatically yet; that wiring (and deciding whether Phase 4's deterministic replanner also needs to pass through it, given its own output is already deterministic and battery-checked) is left for a later phase, as scoped.

## RAMR Novelty — Phase 8: Battery Feasibility for Replanning

**What it is:** `BatteryFeasibilityChecker`, a dedicated, standalone battery-feasibility component — extracted (per your "CREATE OR EXTEND" instruction) from the inline calculation Phase 7's `safety_validator.py` had already been doing, so there is exactly ONE implementation of this math in the project, not two drifting copies. Implements your IMPORTANT RULE exactly — battery feasibility is calculated for `Revised Mission + Return To Home` — and reproduces your worked example precisely: Current Battery 30%, Required Remaining Mission 12%, Required RTH 10%, Reserve 5% → Total Required 27% → `FEASIBLE`; if Total Required > Available Battery → `NOT_FEASIBLE`.

**Files created:**
- `app/replanning/battery_feasibility.py` — `BatteryFeasibilityStatus` (`FEASIBLE`/`NOT_FEASIBLE`), `BatteryFeasibilityResult`, and `BatteryFeasibilityChecker` with two methods: `check(current_battery_percent=, remaining_mission_distance_km=, corrective_waypoint_distance_km=, return_to_home_distance_km=, battery_reserve_percent=)` — pure percent math, matches your INPUT list and worked example directly, no `MissionItem`/`DroneState` dependency; and `check_from_positions(...)` — a convenience wrapper that derives those three distances from real drone position + waypoint coordinates (walking corrective waypoints, then remaining waypoints, then the leg home, the same order Phase 6/7 already established).
- `tests/test_phase_battery_feasibility.py` — 10 tests: the spec's worked example reproduced exactly (`FEASIBLE`), the same shape with less battery (`NOT_FEASIBLE`), the boundary case (`Total Required == Available` is still `FEASIBLE`, not a strict `>`), corrective-waypoint distance actually contributing to the total, defaults pulling from `settings`, `check_from_positions()` walking corrective→remaining→home correctly and ignoring items with no coordinates (e.g. `RETURN_TO_LAUNCH`), and the SUCCESS CRITERIA case (insufficient battery for a safe return → `NOT_FEASIBLE`).

**Files modified:**
- `app/replanning/safety_validator.py` — `_check_battery_feasibility()` now DELEGATES to `BatteryFeasibilityChecker.check_from_positions()` (called twice: once for the full proposed route + RTH, once for a direct RTH from the drone's current position) instead of recomputing the same haversine/percent math inline. Phase 7's public result shape (`BatteryFeasibility`, `SafetyValidationResult`) is UNCHANGED, so every one of Phase 7's 21 existing tests still passes without modification — only the internals were extended to reuse the new dedicated component, exactly as your "CREATE OR EXTEND" phrasing allows.

**Existing components reused:** the haversine great-circle distance formula and flat per-km battery cost model from `app.replanning.replanner` (Phase 4, imported directly rather than duplicated a third time); `settings.min_battery_reserve_percent`/`.battery_consumption_percent_per_km` (Phase 4's config, unchanged); `MissionItem` (Phase 3/4) for waypoint coordinates in `check_from_positions()`.

**New functionality added:** a single, directly-testable source of truth for "can this revised mission plus the trip home actually be flown on the battery available" — reusable by anything in the project (Phase 7's safety validator today; a future automatic-replanning pipeline tomorrow) without re-deriving the math.

**How to run it:**
```bash
python -m app.replanning.battery_feasibility
```
Prints the spec's own worked example (`FEASIBLE` at 30% battery) followed by the same shape at a lower battery level (`NOT_FEASIBLE`), as JSON.

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_battery_feasibility.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 10/10 new tests pass; full suite (198 tests total) passes, including all 21 pre-existing Phase 7 `test_phase_safety_validator.py` tests unmodified.

**Known limitations:** still the same flat per-km consumption-rate model Phase 4 established (no wind, payload, climb cost, or a real endurance curve) — this phase's job was to formalize and consolidate the FEASIBLE/NOT_FEASIBLE decision and its inputs, not to build a more accurate energy model. `check_from_positions()` assumes waypoints are flown in list order with no obstacle-avoidance detour, i.e. straight-line great-circle legs between consecutive coordinates, same assumption Phase 4/7 already made.

## RAMR Novelty — Phase 9: Mid-Flight Mission Update Manager

**THIS IS A CRITICAL PHASE — read the "What was NOT verified" note below before relying on this.**

**What it is:** `MissionUpdateManager`, which safely replaces a PX4 vehicle's active mission mid-flight with an already safety-approved revised route (Phase 7's output), and resumes execution from the drone's current position — never from WP1. Implements all 10 of your RESPONSIBILITIES: detects an active replan and rejects a concurrent one, reads current progress/completed waypoints straight off the existing `MissionState` (Phase 1), receives the already-generated remaining mission (Phase 6/7's output — this module does not call the LLM), pauses the vehicle into a safe hover before touching anything, uploads the revised mission, structurally guarantees no restart from WP1, resumes execution, and verifies mission progress after the update.

**Research performed (per your explicit instruction, before writing any code):** every MAVSDK Mission API claim in this module's docstring is checked against the current MAVSDK-Python plugin reference and the MAVSDK missions guide, not assumed:
- [Mission — MAVSDK-Python docs](http://mavsdk-python-docs.s3-website.eu-central-1.amazonaws.com/plugins/mission.html) — exact documented behavior of `pause_mission()` (puts the vehicle into HOLD, hovering), `clear_mission()`, `upload_mission()`, `set_current_mission_item()` (confirms index 0 means "restart from the beginning of WHATEVER PLAN IS CURRENTLY LOADED" — the key fact this design leans on), `start_mission()`, `mission_progress()`.
- [Missions — MAVSDK C++ Guide](https://mavsdk.mavlink.io/main/en/cpp/guide/missions.html) — confirms `start_mission()` is the one documented call to "start or resume a paused mission."
- [MAVSDK-Python issue #442](https://github.com/mavlink/MAVSDK-Python/issues/442) — an open feature request asking for exactly "pause, redirect, resume from next waypoint," with no confirmed working pattern attached — independent evidence your spec's caution here is warranted, not overcautious.
- [PX4-Autopilot issue #22491](https://github.com/PX4/PX4-Autopilot/issues/22491) — a real, closed PX4 bug report: mission altitude is computed relative to the vehicle's CURRENT home position, and re-reading/moving home mid-flight measurably corrupted altitude (in the reported case, down to a crash). Directly drove a design decision: `apply_update()` NEVER re-queries `telemetry.home()` — it requires the SAME home position the original mission upload already used, passed in by the caller.

**What was NOT verified (read this before trusting this "reliably," per your DO NOT PROCEED instruction):** this cloud sandbox has no PX4 SITL / Gazebo / jMAVSim instance available — there is no flight simulator this environment can run. Every PX4-touching test in this entire project (going back to the original Phase 7/8) has only ever run against a hand-built fake `mavsdk` module here; real PX4 SITL testing has only happened on YOUR machine. This module is no different: `tests/test_phase_mission_update_manager.py` proves this module calls the documented MAVSDK API in the right order with the right data — it CANNOT prove PX4 actually behaves that way in real flight, because that behavior is not even fully documented by MAVSDK/PX4 themselves (see the research above). **`app/mavlink/mission_update_manager.py`'s `_demo_cli()` is written specifically so you can run the spec's own TEST SCENARIO (WP1→WP2→WP3→WP4→WP5, trigger at WP3, updated route CURRENT POSITION→NEW WAYPOINT→WP4→WP5→RTH) against your real PX4 SITL setup and visually confirm — via QGroundControl or the SITL console — that the SUCCESS CRITERIA actually hold before this is used for anything further.**

**Files created:**
- `app/mavlink/mission_update_manager.py` — `MissionUpdateResult`, `MissionUpdateManager` (`is_update_in_progress()`, `apply_update()`), plus `_demo_cli()` for real-SITL manual verification.
- `tests/test_phase_mission_update_manager.py` — 15 tests: the full happy-path call order (pause → clear → upload → start, matching your TEST SCENARIO exactly), the SUCCESS CRITERIA checked directly (no WP1/old-waypoint coordinates in the uploaded plan), the concurrency guard rejecting (not queuing) a simultaneous request, the `MissionState`-level guard rejecting an update when the mission isn't `RUNNING`, the defensive TAKEOFF-item rejection, every failure/restore path (pause failure, upload failure with successful restore, upload failure with failed restore leaving the vehicle paused, resume failure with restore, verification timeout, verification against a mismatched `total`), and 3 tests covering a bugfix found while designing RAMR novelty Phase 10's orchestrator (see that phase's section below): `return_to_launch_after_mission` now correctly defaults to the CONVERTED mission's own derived value (matching the original `px4_client.py`'s precedent) instead of silently requiring/ignoring a caller-supplied one, and the restore-on-failure path now also sets that flag (it was missing entirely before).

**Files modified:** none. Purely additive — reuses `MissionStateManager` (Phase 1, including its previously-unused `remaining_route()` scaffolding, now actually used for restore), `app.mission.converter.to_mavsdk_mission_plan()` (Phase 6, unchanged), and consumes Phase 7's `enforce_before_mavsdk()` output as its `approved_mission` input.

**Existing components reused:** `MissionStateManager.start_replanning()`/`.begin_update()`/`.apply_replan()`/`.fail_replan()`/`.remaining_route()` (Phase 1 — the entire replanning state machine already built for exactly this, now driven for the first time); `to_mavsdk_mission_plan()` (Phase 6, for both the real update and the restore path); the safety-approved `Mission` from RAMR novelty Phase 7 (this module never generates or safety-checks a route itself — reused, not reimplemented, keeping the standing rule that a safety-critical action doesn't depend solely on the LLM).

**New functionality added:** the actual mid-flight application step the whole RAMR pipeline (Phases 3–7) has been building toward — pause, clear, upload, resume, verify — with an explicit, tested failure-recovery path (restore the pre-update remaining mission if a failure happens after the old one was already cleared) rather than leaving the vehicle in an undefined state on error.

**How to run it:**
```bash
python -m app.mavlink.mission_update_manager
```
Needs PX4 SITL already running, connected, and an original mission already uploaded/armed/RUNNING near WP3 (this script does not fly the drone there for you — see the module's own docstring). **This is the manual SITL verification run your spec asks for.**

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_mission_update_manager.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 15/15 new tests pass; full suite (213 tests total at the time this phase was completed) passes.

**Known limitations:** the big one is stated above in bold — this module's call sequence is grounded in documentation research and cited sources, and its ORCHESTRATION logic is fully unit-tested against a faithful fake, but genuine PX4-SITL-in-the-loop behavior (does the vehicle actually hover cleanly on pause, does re-upload-while-paused actually work as documented, does it resume smoothly with no visible backwards flight) has NOT been observed in this environment and MUST be verified on your machine before this is considered done, per your own "DO NOT PROCEED UNTIL THIS WORKS RELIABLY" instruction. Additionally: `clear_mission()`'s necessity before `upload_mission()` is not confirmed either way by the docs (this module calls it anyway, as defense-in-depth, documented as a decision rather than a certainty); `set_current_mission_item()` is deliberately never called (a researched decision, not an oversight — see the module docstring); the restore-on-failure path itself is unverified against real SITL for the same reason as the main path.

**Bugfix found during Phase 10 integration (documented here since it's a Phase 9 file):** while wiring `MissionUpdateManager.apply_update()` into RAMR novelty Phase 10's orchestrator, two related bugs surfaced: (1) `return_to_launch_after_mission` was a REQUIRED parameter that, when supplied, was used directly — silently ignoring `to_mavsdk_mission_plan()`'s own derived value, unlike the original `px4_client.py`'s established precedent of always trusting the converter's derived value; (2) `_attempt_restore_previous_mission()` never called `set_return_to_launch_after_mission()` at all, so a restored mission could keep a stale RTL flag from the failed update attempt. Both are fixed: the parameter is now optional (`bool | None = None`) and defaults to the converted mission's own value unless explicitly overridden, and the restore path now sets the flag too. All 12 pre-existing tests still passed unmodified after the fix (they all happened to exercise the override path); 3 new tests were added specifically for the default/override/restore behavior.

## RAMR Novelty — Phase 10: RAMR Integration Testing

**What it is:** `RAMROrchestrator` (`app/replanning/orchestrator.py`), which wires up the FINAL FLOW your spec drew, component by component, end to end: `PX4 → Telemetry → DroneStateManager → TriggerEngine → AdaptiveMissionReplanner → ReplanningContextBuilder → RAG + LLM → Revised Mission Proposal → Safety Validator → Battery Checker → MissionUpdateManager → MAVSDK → PX4`. Every box in that diagram is an EXISTING component from an earlier phase — Phases 3–9 — reused unchanged; this module only sequences the calls between them and adapts return shapes where two phases' outputs don't already line up.

**Design choice worth calling out — reading the diagram's own ordering literally:** the diagram draws `AdaptiveMissionReplanner` (Phase 4, deterministic, "DO NOT USE THE LLM YET") BEFORE `ReplanningContextBuilder`/`RAG + LLM` (Phase 5/6). `handle_event()` therefore always tries the deterministic replanner FIRST for a triggered event, and only builds a `ReplanningContext` and calls the LLM (`AdaptiveReasoningEngine.propose()`) when the deterministic replanner reports it doesn't know how to handle this event (`UNSUPPORTED`/`UNHANDLED_EVENT_TYPE`). A deterministic `MODIFIED` or `UNCHANGED` result is trusted and the LLM is never invoked for that event — this both matches the diagram and keeps this project's standing "deterministic before LLM" bias for anything with a known, simple rule.

**Two gaps left open by Phases 7–9 that this phase closes**, because integrating the full loop is what surfaces them:
1. A REJECTED `SafetyValidationResult` carries a `fallback_action` (CONTINUE_ORIGINAL_MISSION / RETURN_TO_HOME / ABORT), but nothing before Phase 10 ever executed it. `RAMROrchestrator` now does: CONTINUE_ORIGINAL_MISSION is a true no-op (the drone is already flying the original mission), while RETURN_TO_HOME/ABORT are carried out with a direct, deterministic MAVSDK Action-plugin call (`drone.action.return_to_launch()` / `.land()` — the same `.action` plugin `app/mavlink/px4_client.py` already uses for `arm()`). The fallback DECISION is still made deterministically by Phase 7's `_choose_fallback()`; Phase 10 only carries it out — the LLM is never involved, consistent with the standing "safety-critical actions must not depend solely on the LLM" rule.
2. An APPROVED `SafetyValidationResult` with `approved_mission is None` (a bare action-only proposal — e.g. plain RETURN_TO_LAUNCH/LAND/ABORT_MISSION/HOLD_POSITION with no waypoint items, which `MissionUpdateManager.apply_update()` has no way to accept) is now executed the same way via the Action plugin, rather than silently doing nothing. If no `drone` connection is supplied to `handle_event()` (e.g. a caller only wants the decision, not execution), this is reported honestly as `OrchestrationStage.APPROVED_ACTION_ONLY` instead of crashing or being silently dropped.

**"Multiple simultaneous triggers"** is guarded two ways, both reused from earlier phases: `MissionUpdateManager.is_update_in_progress()` (Phase 9) is checked before any work starts for a new event, and `MissionUpdateManager`'s own internal `asyncio.Lock` (Phase 9) is the final word for a true race — which is why `RAMROrchestrator` holds and reuses ONE `MissionUpdateManager` instance across every `handle_event()` call rather than a fresh one per call.

**"Replanning while near mission completion"** is a new, deliberately simple rule (your spec names the scenario but not exact numeric criteria): `handle_event()` stops immediately, before touching the replanner/LLM/safety validator at all, whenever every item left in `MissionState.remaining_waypoints` is already a terminal command (LAND/RETURN_TO_LAUNCH) — there is nothing left to safely replan.

**Files created:**
- `app/replanning/orchestrator.py` — `OrchestrationStage`, `ReplanSource`, `OrchestrationResult`, `RAMROrchestrator.handle_event()`, plus `_demo_cli()` for a fake-MAVSDK end-to-end walkthrough of Scenario 1 and instructions for real-SITL verification.
- `tests/test_phase_orchestrator.py` — 10 tests covering all 6 of your TEST SCENARIOS (battery deviation → applied; missed waypoint → dropped and applied; mission deviation, both HIGH/CRITICAL → RTH-applied and MEDIUM → no-change; an invalid LLM proposal → rejected before ever reaching MAVSDK; two concurrent triggers → exactly one applied, the other rejected not queued; replanning near mission completion → skipped) plus 4 extra: a below-threshold event never triggers, a bare action-only approval executes via the Action plugin, and that same case is reported honestly (not silently skipped) when no `drone` is supplied.

**Files modified:** `app/mavlink/mission_update_manager.py` and `tests/test_phase_mission_update_manager.py` — see the RTL-flag bugfix note in the Phase 9 section above (found while building this phase).

**Existing components reused:** `TriggerEngine.handle_event()` (Phase 3), `AdaptiveMissionReplanner.replan()` (Phase 4), `ReplanningContextBuilder.build()` (Phase 5), `AdaptiveReasoningEngine.propose()` (Phase 6), `ReplanningSafetyValidator.validate()` + `enforce_before_mavsdk()`'s own approval contract (Phase 7, which already calls Phase 8's `BatteryFeasibilityChecker` internally), `MissionUpdateManager.apply_update()`/`.is_update_in_progress()` (Phase 9). Nothing here reimplements any of them — `orchestrator.py` never contains its own battery math, rule validation, or MAVSDK mission-upload sequencing.

**New functionality added:** the actual autonomous glue between every phase — deciding which engine (deterministic vs. LLM) gets to propose a change, guaranteeing a proposal cannot reach MAVSDK unvalidated regardless of its source, and — closing the two gaps above — actually carrying out a rejection's fallback action or an approved bare action via MAVSDK, not just deciding what should happen.

**How to run it:**
```bash
python -m app.replanning.orchestrator
```
Runs TEST SCENARIO 1 (battery deviation) through the complete FINAL FLOW against an in-process fake MAVSDK System — no PX4 SITL needed to see the pipeline connect end to end. Prints the full `OrchestrationResult` and the fake mission plugin's call log.

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_orchestrator.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 10/10 new tests pass; full suite (223 tests total) passes.

**Known limitations:** same standing gap as Phase 9 — no PX4 SITL is available in this sandbox, so `tests/test_phase_orchestrator.py` proves the full pipeline calls every component in the right order with the right data against faithful fakes, but does NOT prove real PX4/MAVSDK behaves this way in flight; `_demo_cli()` is offered for your own SITL verification the same way Phase 9's is. `drone.action.hold()` (used for a bare `HOLD_POSITION` recommendation) and `drone.action.land()` (used as both "abort" and "land") are cited from MAVSDK's documented Action plugin API but likewise unverified against a real flight controller here. The mapping from a deterministic `RAMRResult` into a `RevisedMissionProposal` (recommended action, `confidence=1.0`) is this module's own reasonable choice, not something any earlier phase's spec defines — `RevisedMissionProposal` was designed in Phase 6 for LLM output and this is its first reuse for a non-LLM source. Mission priority and mission history are still passed through unset by default (same gap Phase 5 already documented — no real priority/history store exists in this project).

## RAMR/CV Novelty — Phase 11: Camera Integration

**What it is:** `CameraStreamManager` (`app/vision/camera_stream.py`), which tags raw camera frames with the current mission ID, current waypoint, drone position, and a timestamp — producing your `InspectionFrame` output — and dispatches each one to subscribers. Implements the ARCHITECTURE diagram literally: `PX4 / Gazebo Camera → CameraStreamManager → InspectionQualityAnalyzer`. Per your explicit instruction, camera events are **not** connected to RAMR/the Trigger Engine in this phase — `InspectionQualityAnalyzer` here is a minimal receiver that proves frames arrive correctly tagged, not a CV engine.

**"Reuse existing data structures" followed:** `InspectionFrame.drone_position` is the SAME `DronePosition` dataclass `ReplanningContextBuilder` already defined (Phase 5) — not a new, parallel position type. `mission_id`/`waypoint_id` are read off the same `MissionState` (Phase 1) every other module reads mission/waypoint identity from, the same way `ReplanningContextBuilder` does.

**A deliberate non-choice, documented rather than guessed:** `InspectionFrame.frame` is typed `Any`, not `numpy.ndarray`/`bytes`/anything else. Nothing earlier in this project takes a dependency on an image library, and your own ARCHITECTURE line draws the source as a Gazebo camera *topic*, not a Python object this project controls the shape of — so `CameraStreamManager` tags and routes whatever object a real frame source produces without ever decoding or assuming its contents. No `opencv-python`/`numpy`/`Pillow` dependency was added to `requirements.txt` — none is needed yet, since nothing here reads pixel data; the first phase that actually analyzes image quality should add one then.

**Files created:**
- `app/vision/camera_stream.py` — `InspectionFrame`, `CameraStreamManager` (`capture_frame()` for one already-pulled frame, `.stream()` for a continuous `AsyncIterator[Any]` feed, `.subscribe()`), `iter_frames()` helper (wraps a plain list into the async-iterator shape `.stream()` expects), `InspectionQualityAnalyzer` (minimal receiver — `receive_frame()`, `frames_for_waypoint()`, `frames_for_mission()`), plus `_demo_cli()`.
- `tests/test_phase_camera_stream.py` — 15 tests: `InspectionFrame.as_dict()` shape (and that the raw `frame` payload itself is excluded), frame tagging correctness against a live `MissionState`/`DroneState`, the fallback when no current waypoint is tracked yet, subscriber fan-out and one-subscriber-raising resilience (mirrors `EventBus`'s own contract), `.stream()` re-reading LIVE state for every frame (not a stale snapshot from when streaming started) and respecting `max_frames`, the analyzer's per-waypoint/per-mission filtering, and a full-pipeline SUCCESS CRITERIA test.

**Files modified:** none. Purely additive — new `app/vision/` package.

**Existing components reused:** `DronePosition` (Phase 5's `context_builder.py`, unchanged); `MissionState`/`MissionStateManager.snapshot()` (Phase 1); `DroneState` (Phase 2); the subscriber-fan-out-with-resilience pattern from `EventBus` (Phase 3, reimplemented identically here rather than differently, not imported directly since it's typed around `ReplanningEvent` specifically).

**New functionality added:** the first piece of this project's Camera/CV track (per `docs/RAMR_CV_Audit.md`'s own roadmap) — a tested, working pipeline that pulls raw frames from a pluggable source and gets them, correctly tagged and associated with mission waypoints, to a downstream consumer. `app.trigger.events.EventType`'s `IMAGE_BLUR_DETECTED`/`BAD_EXPOSURE_DETECTED`/`COVERAGE_GAP_DETECTED` (already defined since Phase 3) remain unused — connecting them is explicitly future scope, not this phase's.

**How to run it:**
```bash
python -m app.vision.camera_stream
```
Runs a synthetic 3-frame stream (plain bytes standing in for real image data — no PX4/Gazebo needed) through the full pipeline and prints each tagged `InspectionFrame` plus the analyzer's received count.

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_camera_stream.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 15/15 new tests pass; full suite (238 tests total) passes.

**Known limitations:** this sandbox has no PX4 SITL/Gazebo instance (same standing gap as every earlier PX4-touching phase — see `docs/RAMR_CV_Audit.md` item 9/G) — there is no real Gazebo camera topic this code has ever pulled a frame from, and `_demo_cli()`'s synthetic byte-string frames are explicitly not a substitute for that. Wiring an actual Gazebo camera plugin/topic subscriber into the `frame_source: AsyncIterator[Any]` shape `.stream()` expects is new work for your machine, same as PX4 SITL setup itself was. `InspectionQualityAnalyzer` does no real image analysis (by design, per "DO NOT CONNECT CAMERA EVENTS TO RAMR YET") — blur/exposure/coverage-gap detection and wiring into the Trigger Engine are both explicitly future phases' scope.

## RAMR/CV Novelty — Phase 12: Basic Computer Vision Quality Analysis

**What it is:** `InspectionQualityAnalyzer` (`app/vision/camera_stream.py`) is EXTENDED with real image quality checks — blur detection (OpenCV Laplacian Variance) and exposure detection (under/overexposed via mean pixel intensity) — producing your `InspectionQualityResult` (mission_id, waypoint_id, blur_score, exposure_score, quality_status, timestamp). `receive_frame()` (Phase 11) now runs this analysis automatically on every frame it gets, while every Phase 11 method (`frames_for_waypoint()`, `received_count`, etc.) keeps working unchanged — Phase 11's own tests still pass against the extended class.

**Reuse, not a rewrite:** per your CREATE line and this project's standing rule ("don't rewrite completed phases unless necessary"), this phase extends the SAME `InspectionQualityAnalyzer` class Phase 11 already created as the ARCHITECTURE diagram's downstream box, rather than building a second, competing class of the same name.

**Blur:** `detect_blur()` computes `cv2.Laplacian(gray, cv2.CV_64F).var()` — exactly the "lightweight method such as OpenCV Laplacian Variance" your spec names, and the same formula commonly cited for this technique (e.g. PyImageSearch's "Blur detection with OpenCV"). `is_blurry` is `blur_score < settings.blur_variance_threshold` (default 100.0, a documented, tunable heuristic — see `app/config.py`).

**Exposure:** `detect_exposure()` computes mean grayscale pixel intensity (0-255) as `exposure_score`, classified `UNDEREXPOSED`/`NORMAL`/`OVEREXPOSED` against configurable thresholds (defaults 60.0/200.0 — see `app/config.py`). Deliberately a simple global-mean heuristic, matching your own "simple and reliable... start with" framing, not a claim it's the most robust possible method (a small blown-out region in an otherwise well-exposed frame would be missed — documented as a known limitation).

**`quality_status`** (`GOOD`/`BLURRY`/`TOO_DARK`/`TOO_BRIGHT` — your SUCCESS CRITERIA's exact 4 categories) combines both checks with a documented priority: `BLURRY` wins over a simultaneous exposure problem, since your OUTPUT section lists blur before exposure and an out-of-focus frame is the more fundamental defect for an inspection photo.

**Frame decoding:** accepts either an already-decoded `numpy.ndarray` (grayscale or color) or raw encoded image bytes (JPEG/PNG/etc., via `cv2.imdecode`) — the two shapes a real frame source plausibly produces. Anything else, including Phase 11's own non-image placeholder byte strings, raises `FrameDecodeError`; `receive_frame()` catches exactly that, logs it, and still records the frame (with `result=None`) rather than losing it or crashing the pipeline — so Phase 11's existing tests and demo keep working unmodified.

**Files created:**
- `tests/test_phase_quality_analysis.py` — 20 tests using REAL synthetic images (numpy + OpenCV, JPEG-encoded where relevant), covering your own TEST USING list: normal, artificially blurred (`cv2.GaussianBlur`), darkened, and overexposed images. Includes a parametrized test asserting all 4 SUCCESS CRITERIA categories classify correctly, threshold configurability, `FrameDecodeError` for undecodable input, the blurry-takes-priority rule, and that `receive_frame()`'s Phase 11 behavior (frame recording, `frames_for_waypoint()`) is fully preserved.

**Files modified:**
- `app/vision/camera_stream.py` — added `ExposureStatus`, `QualityStatus`, `BlurDetectionResult`, `ExposureDetectionResult`, `InspectionQualityResult`, `FrameDecodeError`, `_to_grayscale()`, and extended `InspectionQualityAnalyzer` with `detect_blur()`/`detect_exposure()`/`analyze()`/`results()`/`results_for_waypoint()`/`results_for_mission()`; `receive_frame()` now calls `analyze()` automatically. `_demo_cli()` now builds 4 real synthetic JPEG images (one per SUCCESS CRITERIA category) instead of Phase 11's placeholder byte strings.
- `app/config.py` — added `blur_variance_threshold` (100.0), `underexposed_mean_threshold` (60.0), `overexposed_mean_threshold` (200.0), documented as tunable heuristic defaults, not validated against a real camera dataset.
- `requirements.txt` — added `numpy`/`opencv-python-headless` (this phase is the first to actually decode/analyze pixel data; Phase 11 deliberately did not add either).

**Existing components reused:** `InspectionFrame`/`CameraStreamManager` (Phase 11, unchanged); `settings` singleton (`app/config.py`, same pattern as every other tunable threshold in this project — `min_battery_reserve_percent`, `max_safe_speed_ms`, etc.).

**New functionality added:** the first real image-content analysis anywhere in this project — a tested, working blur+exposure classifier that correctly separates all 4 of your SUCCESS CRITERIA categories on synthetic test images built specifically to exercise each one.

**How to run it:**
```bash
python -m app.vision.camera_stream
```
Builds 4 real synthetic JPEG images (normal/blurred/darkened/overexposed) and runs them through the full pipeline, printing each one's `InspectionQualityResult`.

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_quality_analysis.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 20/20 new tests pass; full suite (258 tests total) passes.

**Known limitations:** the three thresholds are documented heuristic defaults (see `app/config.py`), not validated against a real inspection-camera dataset or your actual Gazebo/PX4 camera's sensor characteristics — expect to retune them once real frames are available. Exposure detection is a single global-mean heuristic (a small blown-out or crushed region in an otherwise well-exposed frame would be missed — a histogram/percentile-based method would be more robust but is out of scope for "simple and reliable... start with"). `InspectionQualityAnalyzer` still does not publish anything to `app.trigger.bus.EventBus`/`TriggerEngine` — `IMAGE_BLUR_DETECTED`/`BAD_EXPOSURE_DETECTED` remain unused event types (Phase 3), exactly as Phase 11 left them; wiring quality results into RAMR is still a later phase's scope. Standing sandbox limitation, inherited unchanged: no real Gazebo camera has ever fed a frame into this pipeline — only real, but synthetic, JPEG-encoded test images.

## RAMR/CV Novelty — Phase 13: CV Event Generation

**What it is:** `CVEventGenerator` (`app/vision/event_generator.py`) converts a Phase 12 `InspectionQualityResult` into a standard `ReplanningEvent` — implementing the ARCHITECTURE diagram's last two boxes: `Inspection Quality Result → CV Event Generator → ReplanningEvent → TriggerEngine`. This is the phase that finally connects camera/CV output to the Trigger Engine, superseding Phase 11's "DO NOT CONNECT CAMERA EVENTS TO RAMR YET" instruction — this phase's own spec explicitly draws that connection and names it as the SUCCESS CRITERIA.

**Decoupled by design, reusing Phase 3 as-is:** `CVEventGenerator` does not itself hold an `EventBus`/`TriggerEngine` reference or publish anything — it only produces a `ReplanningEvent`. The caller hands that event to `TriggerEngine.handle_event()` or `EventBus.publish()`, exactly the same decoupled pattern telemetry already uses (Phase 3). Nothing about `TriggerEngine`/`EventBus` needed to change for CV events to work — they were already generic.

**Mapping (per your EVENTS list):** `GOOD` → no event; `BLURRY` → `IMAGE_BLUR_DETECTED`; `TOO_DARK`/`TOO_BRIGHT` → `BAD_EXPOSURE_DETECTED`; anything unmapped (nothing today can produce this — reserved for a future quality check) → `INSPECTION_QUALITY_FAILURE`, a new `EventType` this phase adds. At most one event per result, matching your SUCCESS CRITERIA's "a bad image produces **a** valid event" wording.

**Matches your example field-for-field:** `source=COMPUTER_VISION`, `severity=MEDIUM` (configurable, and already matches `TriggerEngine`'s existing Phase-3 default threshold for these event types, so a CV event activates replanning with zero extra configuration), `waypoint_id` (new, additive, default-`None` field added to `ReplanningEvent` itself — reuses this project's existing `MissionItem.sequence`/`InspectionFrame.waypoint_id` convention rather than inventing a new ID scheme; every pre-existing caller of `ReplanningEvent` keeps working unchanged), and `metadata` containing exactly `blur_score`, `camera_information`, and `image_quality_information` (plus `exposure_score` alongside `blur_score`, so an exposure-triggered event is just as self-describing as a blur-triggered one).

**Files created:**
- `app/vision/event_generator.py` — `CVEventGenerator.generate_event()`, plus `_demo_cli()` proving the full chain end to end: a blurry synthetic image → `InspectionQualityResult` → `ReplanningEvent` → `TriggerEngine.handle_event()` → an `AdaptiveReplanningRequest`.
- `tests/test_phase_cv_event_generator.py` — 16 tests: the mapping for all 4 `QualityStatus` values (including the `GOOD`→no-event and unmapped→fallback cases), metadata shape, `waypoint_id`/`mission_id` propagation, severity configurability, `camera_information` enrichment with/without an `InspectionFrame`, and — your SUCCESS CRITERIA directly — a blurry and a mis-exposed image's event each reaching and activating a real `TriggerEngine`, plus the same thing via a real `EventBus.publish()`.

**Files modified:**
- `app/trigger/events.py` — added `EventType.INSPECTION_QUALITY_FAILURE`, and an additive `waypoint_id: int | None = None` field on `ReplanningEvent` (every existing constructor call across the whole project still works unmodified — verified by the full suite).
- `app/trigger/engine.py` — added `INSPECTION_QUALITY_FAILURE` to `DEFAULT_ACTIVATION_THRESHOLDS` (MEDIUM, same as the other CV event types).

**Existing components reused:** `EventSource.COMPUTER_VISION`, `EventType.IMAGE_BLUR_DETECTED`/`BAD_EXPOSURE_DETECTED` (all defined since Phase 3, unused until now), `TriggerEngine`/`EventBus` (Phase 3, unchanged), `InspectionQualityResult`/`InspectionFrame` (Phase 11/12, unchanged).

**New functionality added:** the first real bridge from CV output into RAMR's trigger layer — a bad inspection photo can now genuinely activate adaptive replanning, closing the loop the Phase 0 audit originally flagged as entirely missing. Wiring this further downstream (into `RAMROrchestrator`, Phase 10) is still explicitly out of scope — this phase only goes as far as its own diagram and SUCCESS CRITERIA: reaching the `TriggerEngine`.

**How to run it:**
```bash
python -m app.vision.event_generator
```
Runs a synthetic blurry image through the full chain and prints the `InspectionQualityResult`, the generated `ReplanningEvent`, and the resulting `TriggerEngine` decision/`AdaptiveReplanningRequest`.

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_cv_event_generator.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 16/16 new tests pass; full suite (274 tests total) passes.

**Known limitations:** `camera_information`'s exact shape is this module's own reasonable choice — your spec names the key, not its contents. Severity is a single fixed value per event type, not scaled by how bad the underlying score is. `INSPECTION_QUALITY_FAILURE`'s fallback branch is currently unreachable by any real code path (no existing quality check produces an unmapped status) — it's there for forward compatibility, exercised directly in tests via a fake result. This phase does not wire CV events into `RAMROrchestrator` (Phase 10) — only as far as the `TriggerEngine`, per this phase's own scope.

## RAMR/CV Novelty — Phase 14: CV-Triggered Adaptive Replanning

**What it is:** closes exactly the gap Phase 13 left open. `AdaptiveMissionReplanner` (`app/replanning/replanner.py`, Phase 4) gets a THIRD deterministic handler, `_handle_cv_quality_event()`, alongside its existing battery-deviation and missed-waypoint handlers — dispatched for `IMAGE_BLUR_DETECTED`, `BAD_EXPOSURE_DETECTED`, and `INSPECTION_QUALITY_FAILURE`. Per your own instruction ("Computer Vision is NOT a separate mission planning system. It must become a trigger source for RAMR."), this is implemented literally: a CV event is now handled by the exact same `replan()` entry point, and flows through the exact same FINAL FLOW (`TriggerEngine → AdaptiveMissionReplanner → [Safety Validator → Battery Checker] → MissionUpdateManager → MAVSDK`) every telemetry event already used. **`RAMROrchestrator` (Phase 10) required zero changes** — it already dispatched generically on event type; it was never CV-specific to begin with.

**How the 7 questions your spec lists are answered, deterministically:**
1. *Does the area need revisiting?* — implicitly yes, whenever this handler runs: `TriggerEngine`'s existing MEDIUM severity threshold (set since Phase 3) already filters out anything not worth replanning for before `replan()` is ever called.
2. *Can the mission continue without correction?* — yes, but only as a fallback when question 3 says the correction is unaffordable; the mission is never blocked or abandoned over an inspection gap.
3. *Is battery sufficient for the correction?* — computed with the exact same haversine-distance battery-budget math `_handle_battery_deviation()` (Phase 4) already uses: cost of the original remaining route + RTH, plus the extra detour distance to revisit the flagged waypoint, checked against the same `min_battery_reserve_percent`/`battery_consumption_percent_per_km` budget.
4. *Where should the corrective waypoint be inserted?* — right after the drone's current position (immediate) or just before the closing `RETURN_TO_LAUNCH` (deferred).
5/6. *Revisit immediately, or later?* — `event.severity`: HIGH/CRITICAL → immediate; MEDIUM (the lowest severity that can reach this handler) → deferred to the end of the route.
7. *Should the mission return home?* — yes, immediately, if the battery can't even complete the already-planned remaining route regardless of any correction — a pre-existing feasibility problem takes priority over the inspection gap.

**The corrective waypoint itself** is a `LOITER` `MissionItem` at the flagged waypoint's own coordinates/altitude, holding for the new `settings.cv_revisit_hold_time_s` (5.0s default) — LOITER is the only "hover in place" command this project's schema already has, reused rather than adding a new field to `MissionItem` (which forbids extra fields) to mark something as "corrective."

**Files modified:**
- `app/replanning/replanner.py` — added `_handle_cv_quality_event()` and dispatched the 3 CV event types to it from `replan()`; updated the module docstring with the full Phase 14 design (see the file itself for the complete question-by-question writeup).
- `app/config.py` — added `cv_revisit_hold_time_s: float = 5.0` (documented placeholder, overridable in `.env`).
- `tests/test_phase_replanner.py` — the old Phase-4-era `test_cv_event_types_are_unhandled_in_this_phase` test (which asserted `IMAGE_BLUR_DETECTED`/`BAD_EXPOSURE_DETECTED` were unhandled) is replaced by `test_coverage_gap_detected_is_unhandled_in_this_phase`, since that's no longer true and `COVERAGE_GAP_DETECTED` is the one CV event type still genuinely out of scope (no coverage-gap detector exists anywhere in this project).

**Files created:**
- `tests/test_phase_cv_triggered_replanning.py` — 13 tests: one per question/branch above (`UNSUPPORTED` for a missing `waypoint_id`/unknown waypoint/missing drone or home position, the RTH branch, the battery-insufficient-for-correction branch, immediate vs. deferred insertion, all 3 CV event types dispatching correctly), plus **one full end-to-end test that is the literal SUCCESS CRITERIA**: a real blurry synthetic image runs through the real Phase 11/12 camera pipeline, `CVEventGenerator` (Phase 13) produces a real `IMAGE_BLUR_DETECTED` event, and `RAMROrchestrator` (Phase 10, unmodified) carries it all the way through the `TriggerEngine`, this phase's new replanner handler, the Safety Validator, and a fake MAVSDK `System` — asserting a LOITER revisit waypoint was actually uploaded. A companion test confirms a GOOD image produces no event and therefore no adaptation at all.

**Existing components reused:** `AdaptiveMissionReplanner`/`RAMRResult`/`RAMRStatus` (Phase 4), the haversine distance helper and battery-budget formula (Phase 4), `validate_remaining_mission()` (Phase 4/5), `RAMROrchestrator` (Phase 10, completely unchanged), `TriggerEngine`/`EventBus` (Phase 3, unchanged), `CVEventGenerator`/`InspectionQualityAnalyzer`/`CameraStreamManager` (Phase 11-13, unchanged), `settings` singleton pattern (`app/config.py`).

**New functionality added:** the first genuine CV → safe mission adaptation loop in this project — a bad inspection photo can now cause a real, validated, MAVSDK-applied change to the drone's route, not just a `TriggerEngine` decision that goes nowhere (which is exactly where Phase 13 stopped).

**How to run it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_cv_triggered_replanning.py::test_cv_quality_failure_causes_safe_mission_adaptation_end_to_end -v -s
```
(No standalone `_demo_cli()` was added for this phase — Phase 10's own `python -m app.replanning.orchestrator` already demonstrates the FINAL FLOW end to end for a telemetry event, and the full-chain test above is this phase's demonstration for a CV event; it's easiest to read/run as a test rather than a duplicate CLI script.)

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_cv_triggered_replanning.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 13/13 new tests pass; full suite (287 tests total) passes.

**Known limitations:** the detour-cost estimate (`extra_km` in `_handle_cv_quality_event()`) is a simple point-to-point distance heuristic, not a real path planner — same documented-placeholder spirit as the rest of this module's battery math (see that method's docstring). The corrective LOITER waypoint always targets the EXACT coordinates of the originally-flagged waypoint (not, e.g., a slightly offset re-shoot angle) — reasonable for "revisit the same spot," but not something any spec asked for beyond that. Same standing sandbox limitation as every MAVSDK-touching phase before it: no real PX4 SITL has verified this in this sandbox — the full-chain test proves the pipeline calls every component in the right order with the right data using a fake MAVSDK `System`, not real flight behavior.

## Improvement — Real-World Coordinate & Building Resolution

**What it is:** a new capability layered on top of Phases 1–14 (nothing in those phases was rewritten) that lets the system understand real-world locations from natural language, e.g. *"Inspect AB1 of VIT Chennai at 30 meters."* New flow: `NLP/Mission Parser → Location Resolution Layer → Verified Real-World Location → (existing) RAG + Historical Mission Context → (existing) AI Mission Planner → (existing) Waypoint Generator → (existing) Safety Validation → MAVSDK → PX4`.

**Integration approach (per the spec's own "inspect before changing" workflow):** this project's "existing NLP/mission parsing architecture" turned out to be its schema-constrained LLM generation (`app/llm/llm_client.py`'s `generate_structured()`, Phase 3) — there was never a separate regex/spaCy parser. So entity extraction is implemented the same way: a second, independent schema-constrained generation call (`app.mission.intent_parser.parse_mission_intent()`) using a new `MissionIntent` schema and a new prompt pair, added additively to `app/llm/prompts.py`. The "existing waypoint generator" turned out to be the LLM itself (`generate_validated_mission()`, Phase 3-5) — there is no separate deterministic waypoint-generation module in this project — so "integrating with the existing waypoint generator" meant giving that same function ONE new optional keyword argument, not building a new one.

**Files created:**
- `app/location/` (new package):
  - `models.py` — `ResolvedLocation`, `LocationResolutionResult`, `MatchDetails`, and the `ResolutionType`/`LocationSource`/`LocationConfidence`/`LocationResolutionStatus` enums (matches the spec's own conceptual field list).
  - `geocoding_service.py` — `NominatimGeocodingService`, the only file that talks to OpenStreetMap/Nominatim's `/search` HTTP API directly (same "one file owns the external dependency" shape as `llm_client.py`/`px4_client.py`). Handles success, no results, multiple results, timeout, connection failure, rate-limiting (429), and invalid/malformed responses, each as a distinct `GeocodingError` subclass or a plain empty/short result list, per the spec's own list.
  - `campus_resolver.py` — Step 1 of hierarchical resolution: institution/campus name → best-matching real-world campus.
  - `building_resolver.py` — Step 2: building name → real coordinates, tried in the spec's own priority order (OpenStreetMap first, scoped to the resolved campus's bounding box; the existing RAG knowledge base second, via a new but currently-unpopulated lookup path — see Known limitations).
  - `location_cache.py` — `LocationCache`, a JSON-file-persisted (same idea as Chroma's `chroma_persist_path`), TTL'd, invalidate-able cache — required by Nominatim's own usage policy, and this improvement's own spec section 11/13.
  - `geofence.py` — small dependency-free geometry helpers (haversine distance, point-in-bounding-box, point-in-polygon) plus the new `validate_mission_against_location()` check (spec section 10).
  - `location_resolver.py` — `LocationResolver`, the orchestrator: cache → OpenStreetMap (campus, then building) → RAG fallback → `LOCATION_NOT_RESOLVED`. Computes `confidence` (HIGH/MEDIUM/LOW) from the exact factors the spec's section 7 lists. **Never** falls back to a campus centroid for an unresolved building, and **never** fabricates a coordinate — verified explicitly by tests.
- `app/mission/intent_parser.py` — `MissionIntent` schema + `parse_mission_intent()` (see "Integration approach" above).
- `app/mission/location_aware_generator.py` — `generate_mission_from_natural_language()`, the new top-level entry point wiring intent extraction → location resolution → the **unmodified** `generate_validated_mission()` → the new location-specific safety check.
- `tests/test_phase_location_resolution.py` — 20 tests, including this improvement's own spec section 17 TEST 1 through TEST 6, verbatim.

**Files modified (both changes are purely additive):**
- `app/mission/generator.py` — `generate_mission()`/`generate_validated_mission()` each gained ONE new optional keyword argument, `location_context: str | None = None`. Every existing call site (both `@pytest.mark.live_llm` tests, and every fake-LLM unit test in `tests/test_phase5_generator_integration.py`) is completely unaffected — confirmed by the full suite still passing unmodified.
- `app/llm/prompts.py` — added `INTENT_SYSTEM_PROMPT`/`build_intent_user_prompt()` (new, additive prompt pair — `SYSTEM_PROMPT`/`build_user_prompt()`, Phase 3's original pair, is untouched).
- `app/config.py` — added Nominatim connection settings (`nominatim_base_url`/`nominatim_user_agent`/`nominatim_timeout_s`/`nominatim_min_request_interval_s`), `location_geofence_radius_km`, and `location_cache_path`/`location_cache_ttl_hours` — all documented, overridable in `.env`, same pattern as every other tunable in this project.
- `requirements.txt` — added `requests` (the only new runtime dependency this improvement needs).

**Existing components reused, unmodified:** `app.llm.llm_client.get_llm_client()`/`generate_structured()` (Phase 3), `app.rag.retriever.retrieve()`/`format_context()` (Phase 2, used both by the unmodified mission generator and as `BuildingResolver`'s RAG-fallback source), `app.mission.validator.validate_mission()` (Phase 5, still the first and mandatory check on every generated mission), `app.mission.schema.Mission`/`MissionItem` (unchanged — no new fields were added to accommodate this improvement).

**How the spec's hard rules are honored:**
- *Never hard-code/fabricate/guess coordinates:* grep `app/location/` — there is no latitude/longitude literal anywhere in the resolution code itself (the only numeric coordinates in this whole change are inside `tests/test_phase_location_resolution.py`'s own fake fixture data, clearly commented as such).
- *Never silently use the campus center as a building location:* `LocationResolver.resolve()` returns `LOCATION_NOT_RESOLVED` — never a substituted campus point — when a requested building can't be found anywhere in the priority list. Verified by `test_TEST3_unknown_building_is_not_resolved`.
- *Never bypass safety validation:* `generate_mission_from_natural_language()` calls `generate_validated_mission()` completely unmodified — Phase 5's `validate_mission()` runs first, unconditionally — and only THEN runs the new location-specific check as one additional, later gate. Verified by `test_TEST2_altitude_extracted_and_waypoint_generator_and_safety_run`'s second half (a rule-valid mission in the wrong place is still rejected).
- *Stop mission generation on an ambiguous or unresolved location:* verified by `test_TEST3_*`/`test_TEST4_*` — the fake mission-generation LLM client is never even installed in those tests, so a call to it would fail the test with an unrelated error, proving the pipeline genuinely never reaches that step.
- *Never guess on an API failure:* verified by `test_TEST5_api_failure_falls_back_to_cache_then_stops_safely` — both the "no cache available" (stops safely) and "cache available" (serves the cached, previously-verified value) halves.
- *Preserve RAMR + CV:* nothing in `app/trigger/`, `app/replanning/`, or `app/vision/` was touched. `test_TEST6_ramr_and_cv_still_work_after_location_layer` is a smoke test on top of the full existing suite (307 tests total) still passing unmodified.

**How to run it:**
```bash
# Needs a reachable LLM provider (Groq/Ollama) AND real internet access to Nominatim --
# neither has been exercised together from inside this sandbox, see Known limitations.
python -m app.mission.location_aware_generator "Inspect AB1 of VIT Chennai at 30 meters."

# Individual pieces, for isolated testing on a machine with real connectivity:
python -m app.location.geocoding_service "VIT Chennai"
python -m app.location.location_resolver "VIT Chennai" AB1
python -m app.mission.intent_parser "Inspect AB1 of VIT Chennai at 30 meters."
```

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_location_resolution.py -v
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not live_llm and not heavy"   # full suite, backward-compat check
```

**Expected output:** 20/20 new tests pass; full suite (307 tests total) passes, confirming Phase 1–14 are unaffected.

**Known limitations, stated plainly:**
- **Real Nominatim connectivity has NOT been verified from inside this sandbox** — this sandbox's outbound HTTPS only reaches an allowlisted set of hosts, and `nominatim.openstreetmap.org` is not on it (confirmed: a direct request from here gets a proxy-level 403, the exact same category of restriction Phase 9/10's real-PX4-SITL work has always had). `geocoding_service.py`'s HTTP request/response handling follows Nominatim's actual documented API faithfully, and every test exercises it against a fake HTTP layer proving the request is built correctly and every response/error shape (including timeout/connection/rate-limit/invalid-JSON) is handled — but real connectivity is unverified. **Run `python -m app.location.geocoding_service "VIT Chennai"` on your own machine with real internet access to confirm it**, per this project's standing pattern of being explicit about what has and hasn't actually been exercised against the real external system.
- **The existing RAG knowledge base (`data/documents/`) currently contains no building-level location data at all** — `BuildingResolver.resolve_via_rag()`'s lookup path is real and tested (against a fake retriever), but in this project's CURRENT state it will never actually find anything, because no document in `data/documents/` mentions a building coordinate. This is intentional, not a bug: fabricating a plausible-looking "buildings.md" with invented coordinates for AB1/VIT Chennai would violate the spec's own "never fabricate coordinates" rule just as much as hard-coding it in Python would. If/when you add a real, verified building-coordinate document to `data/documents/`, this path starts working with zero code changes (rebuild the RAG index with `python -m app.rag.retriever --build`).
- **The location-specific safety check (`geofence.py`) is a NEW, separate check, not an extension of Phase 7's `ReplanningSafetyValidator`** — a mission generated through this improvement and later handed to `RAMROrchestrator` for adaptive replanning does NOT automatically carry its resolved campus/building geofence into `SafetyConstraints.geofence` (Phase 7's own geofence field, previously always `None` for lack of any geofence data source) — wiring that through would touch `ReplanningContextBuilder`, which this improvement's own rules said not to modify unnecessarily. Documented as a natural next step, not implemented here.
- **Confidence scoring (`_compute_confidence()`) is this module's own deterministic, documented formula** — the spec names the factors (institution/campus/building match, coordinate validity, campus relationship, geometry availability, source reliability) but not a precise scoring algorithm; the implementation here is a reasonable, testable interpretation, not something derived from an external standard.
- **Building search query phrasing is limited to a couple of non-hardcoded variants** (the raw building name/code, and the same name with "building" appended) scoped to the campus's bounding box — real-world OSM building tagging is inconsistent enough that a genuinely thorough search (address components, alternate names, fuzzy matching) is a larger effort than this improvement's "smallest clean extension" scope called for.

## Feature — PDF File Support for the RAG Builder

**What it is:** the existing document-ingestion layer (Phase 2's "RAG Builder" — the combination of `app/rag/document_loader.py`, `app/rag/embeddings.py`, and `app/rag/retriever.py`) now also accepts `.pdf` files alongside the original `.md`/`.txt`, with no other part of the RAG pipeline rebuilt or replaced.

**A note on "the RAG Builder" before describing the change:** this project has no web frontend or upload endpoint at all — `app/ui/__init__.py` is an empty Phase 1 placeholder, and no Streamlit (or other) app exists anywhere in the codebase despite `streamlit` being listed in `requirements.txt`. "Uploading" a document today, for every file type including the pre-existing `.md`/`.txt` ones, means placing the file in `data/documents/` and running `python -m app.rag.retriever --build`. PDF support was added to that same, only-existing mechanism; inventing a new web UI/backend was out of scope ("make the smallest UI change necessary" / "do not redesign the entire UI") and would not have been reusing anything that already existed.

**Integration approach (per the spec's own "inspect before changing" workflow):** the exact, and only, integration point is `app/rag/document_loader.py`. `app/rag/embeddings.py` (text-in, vectors-out, source-agnostic) and `app/rag/retriever.py` (`build_index()`/`retrieve()`, which already only depend on whatever `Chunk` objects `load_and_chunk_documents()` produces and whatever keys `Chunk.as_metadata_dict()` includes — confirmed by reading both files in full) needed **zero** changes. The existing, dependency-free `chunk_text()` function is reused completely unmodified for PDFs too — called once per PDF page rather than once per whole document, which is what makes per-chunk page metadata (spec section 4) fall out for free instead of requiring a second chunking system.

**Files created:**
- `tests/test_phase_pdf_ingestion.py` — 8 tests (7 structural + 1 `@pytest.mark.heavy`), covering this feature's own spec TEST 1 through TEST 7. Builds real PDFs on the fly with `reportlab` (a genuine multi-page text PDF and a genuine PDF containing an actual `reportlab.platypus.Table`) rather than mocking extraction — matching this project's existing preference for real synthetic test data (see e.g. Phase 12's synthetic JPEG frames).

**Files modified:**
- `app/rag/document_loader.py`:
  - `SUPPORTED_EXTENSIONS` gained `.pdf` (the single, backend-authoritative source of truth for what's accepted — there is no separate frontend validation to keep in sync, since there is no frontend).
  - `Chunk` gained one new optional field, `page: int | None = None` (defaults to `None`, so every existing `.md`/`.txt` chunk's shape/behavior is unchanged), and `as_metadata_dict()` now also always includes `file_type`, and includes `page` only when it's not `None` (Chroma's metadata values can't be `None`).
  - `load_raw_documents()` is now scoped to `.md`/`.txt` only (a new `_TEXT_EXTENSIONS` constant) — its own logic and behavior for those two types is byte-for-byte unchanged; it simply no longer tries to `.read_text()` a binary `.pdf` file.
  - Three new functions handle the PDF-specific path: `_load_pdf_document()` (opens a PDF with `pdfplumber`, extracts text + tables per page, returns `(page_number, cleaned_text)` tuples — raises the new `PDFExtractionError` for anything that prevents opening the file at all), `_render_table_as_text()` (renders a `pdfplumber`-detected table into simple pipe-delimited text, e.g. `"Building | Floors | Height\nAB1 | 7 | 45 m"`, spliced into that page's text before chunking — per spec section 6, tables are preserved for retrieval, never silently discarded), and `_clean_pdf_text()` (minimal whitespace normalization, scoped to the PDF path only).
  - `load_and_chunk_documents()` now also walks `documents_dir` a second time for `.pdf` files, calling `_load_pdf_document()` and then the existing `chunk_text()` once per page. PDF chunk IDs are `"{filename}::page-{N}::chunk-{i}"` — close enough to the original `"{filename}::chunk-{i}"` convention to stay readable, and stable/deterministic across reruns, so Chroma's existing upsert-by-id behavior in `retriever.build_index()` is the only duplicate-handling mechanism needed (per spec section 9 — no second dedup system was built).
- `requirements.txt` — added `pdfplumber` (the single PDF library used, chosen over `pypdf` because it also does page-aware `extract_text()` and `extract_tables()` with no second dependency — per spec section 3's "do not introduce multiple PDF libraries").

**Existing components reused, unmodified:** `app/rag/embeddings.py` (`Embedder`/`get_embedder()`), `app/rag/retriever.py` (`build_index()`/`retrieve()`/`format_context()`/Chroma upsert-by-id dedup), `chunk_text()` itself (called by both the `.md`/`.txt` path and, unmodified, by the new PDF path), the existing `.md`/`.txt` loading code path in full.

**How to run it:**
```bash
# Place a PDF alongside the existing .md/.txt files, then rebuild the index --
# the exact same command every other supported file type already uses:
cp VIT_Chennai_Campus_Building_Details.pdf data/documents/
python -m app.rag.retriever --build

# Query it through the existing, unmodified retriever:
python -m app.rag.retriever "What is the height of AB1?"
```

**How to test it:**
```bash
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/test_phase_pdf_ingestion.py -v -m "not heavy"
PYTHONPATH=/usr/local/lib/python3.11/dist-packages pytest tests/ -v -m "not heavy and not live_llm"   # full suite, backward-compat check
```

**Expected output:** 7/7 non-heavy PDF tests pass; full suite (314 tests total, up from 307) passes, confirming every prior phase/improvement is unaffected. The one `@pytest.mark.heavy` PDF test (`test_pdf_end_to_end_retrieval` — builds a real Chroma index from a generated PDF and retrieves from it through the unmodified `retriever.retrieve()`) follows the exact same pattern as the pre-existing heavy tests in `tests/test_phase2_rag.py` and was reviewed but **could not be executed in this sandbox**, because `sentence-transformers`/`chromadb` are not installed here and there is no PyPI access to install them (the same standing sandbox limitation this project's `heavy` pytest marker exists for) — run it on a machine with those installed to confirm end-to-end.

**Known limitations, stated plainly:**
- **No OCR / scanned-PDF support.** Per spec section 7's own instruction to prioritize normal text-based PDFs first: if a PDF's pages yield no extractable text at all (e.g. a scanned/image-only PDF), the file is skipped with a clear warning log (`"...no extractable text was found... may be a scanned/image-based PDF requiring OCR, which this project does not yet implement..."`) rather than silently indexed empty or crashing the whole `build_index()` run. Adding OCR later would mean a library such as `pytesseract` plus a system Tesseract install (a real new dependency, not yet added since it wasn't determined to be necessary) or a cloud OCR API.
- **Table detection is `pdfplumber`'s own heuristic `extract_tables()`** — it works well on PDFs with real, ruled/bordered tables (verified by `test_pdf_table_content_is_preserved_in_chunks` against a genuine `reportlab` table), but is not guaranteed to detect every visually-table-like layout (e.g. tables with no visible borders). Per spec section 6, this is documented as a known limitation rather than papered over or invented around.
- **No artificial file-size cap.** The spec named "very large PDF" as a case to consider but gave no threshold; rather than invent a number, very large PDFs are processed exactly like any other PDF today (memory/time will scale with page count) — a real cap, if needed, is a follow-up with a stated, justified value.
- **No UI change was made**, for the reason explained above (none exists to change). `SUPPORTED_EXTENSIONS` is the single place a future UI would read from to list "✓ TXT ✓ DOCX ✓ PDF"-style supported types.
- **The heavy end-to-end retrieval test is unexecuted in this sandbox** (see "Expected output" above) — the same category of limitation as the location-resolution improvement's unverified real-Nominatim connectivity, stated explicitly rather than silently assumed to work.

## Improvement — Location Resolution, Remaining Phases A–D

Gaps found when the location-resolution spec was re-audited against the code, and closed without repeating anything already built:

- **A. Confidence gate + building-name variants.** A location that resolves with confidence below `LOCATION_MIN_CONFIDENCE` (default `MEDIUM`) now stops planning (`LOCATION_LOW_CONFIDENCE`), per spec §7. `app/location/building_names.py` expands `AB1` ↔ `Academic Block 1` / `AB-1` / `AB 1` (from `BUILDING_ALIAS_PREFIXES`, a naming convention only — never a coordinate) for both map and RAG searches.
- **B. Building facts from the existing RAG** (`app/location/building_knowledge.py`). Reads a building's height from indexed documents with a source + page citation. With your `data/documents/Vit_building_details.pdf`, AB1 → **36 m (page 6, Table 1)**, AB2 → 40 m. Unknown buildings stay unknown; conflicting values keep the larger (cautious) one. `RetrievedChunk` gained an optional `page` field (default `None`, backward compatible).
- **C. Perimeter inspection ring** (`app/location/inspection_geometry.py`). A 360° ring of reference points around the real footprint at `INSPECTION_STANDOFF_M` (15 m), handed to the existing LLM planner as context. For a point-only location the footprint radius is an explicit, flagged assumption (`BUILDING_UNKNOWN_FOOTPRINT_RADIUS_M`, 60 m) — never presented as measured.
- **D. Location safety** (`app/location/location_safety.py`): roof clearance (a waypoint over AB1 at 30 m is rejected — AB1 is 36 m, needs ≥ 46 m with `BUILDING_ROOF_CLEARANCE_M` 10 m), initial battery/RTH feasibility via the unchanged Phase 8 checker (only when a live battery reading + home are given; otherwise reported as *not checked*), and `safety_constraints_for_location()` to pass the resolved geofence to the unchanged `RAMROrchestrator.handle_event(..., safety_constraints=...)`.

Tests: `tests/test_phase_location_remaining.py` (21).

## Phase 15 — Google Maps Location Resolution + Mission Map

**Audit findings (before coding):** the project has **no React dashboard, no Leaflet, no HTTP backend, no GeoJSON files and no mission-history store** — `app/ui/` was an empty placeholder and `streamlit` was listed but unused. Location resolution already existed (`app/location/`, OpenStreetMap/Nominatim) with a provider seam. So Phase 15 adds Google as a **second provider behind the existing `LocationResolver`** (no second resolver), and the smallest possible map UI: one static page + a standard-library server (no new framework, no new Python dependency).

**Files created**
- `app/location/google_places_service.py` — Google provider with the same `search()` interface and error types as the Nominatim service. Places API (New) Text Search for places; Geocoding API v4 *SearchDestinations* for real building outlines (`displayPolygon`, only when `structureType == BUILDING`).
- `app/location/providers.py` — `LOCATION_PROVIDER` = `auto` (Google if `GOOGLE_MAPS_API_KEY` is set, else OSM) / `google` / `openstreetmap`.
- `app/ui/mission_view.py` — `MissionViewStore`: the JSON the map draws, derived only from the real result, the live `MissionStateManager` (so RAMR replans appear automatically) and `DroneState`.
- `app/ui/map_server.py` — localhost-only `http.server`: `/`, `/api/mission-view`, `/api/maps-config` (browser key only — the server key is never served).
- `app/ui/static/mission_map.html` — Google Maps JavaScript API page (dynamic bootstrap loader, `AdvancedMarkerElement`). Polls once a second; refits only on a new plan or new mission version.
- `scripts/plan_mission.py` — submit missions; the map opens and updates by itself.
- `tests/test_phase15_google_maps.py` (22), `tests/test_phase15_map_page.py` (4, headless Chromium).

**Files modified (additive):** `app/config.py` (provider/key/map settings), `app/location/models.py` (`GOOGLE_PLACES` source, `GeometryType` POINT/POLYGON/MULTIPOLYGON, optional `place_id`/`formatted_address`/`geometry_parts`), `app/location/geocoding_service.py` (`GeocodingAuthError`, optional candidate fields, provider labels), `app/location/building_resolver.py` (candidate validation + ambiguity), `app/location/location_resolver.py` (provider-agnostic labels/messages, AMBIGUOUS on multiple matches), `app/location/location_cache.py` (persists the new fields), `.env.example`.

**Candidate validation — a Google result is never accepted just because it was returned.** A building candidate must (1) lie inside the resolved campus, (2) have a *name* matching the requested building, and (3) not be the campus itself (Google's fuzzy search often returns the university for "AB1, VIT Chennai"). Two different matching places > `BUILDING_AMBIGUITY_DISTANCE_M` (40 m) apart → `AMBIGUOUS`, never a silent pick.

**Map colours:** red = target footprint / inspection region · blue = planned path + waypoints · green = completed waypoints + flown track · yellow = RAMR replanning / corrective waypoints · dark dot = drone. A point-only target is a red "T" pin — a polygon is never invented.

**API configuration (Google Cloud Console)**
1. Enable **Places API (New)**, **Geocoding API**, **Maps JavaScript API**; billing must be enabled.
2. Server key → `GOOGLE_MAPS_API_KEY`; restrict to Places API (New) + Geocoding API.
3. Browser key → `GOOGLE_MAPS_BROWSER_API_KEY`; restrict to Maps JavaScript API and HTTP referrers `http://127.0.0.1:8765/*`, `http://localhost:8765/*`.
4. Keys go in `.env` only (already in `.gitignore`).

**How to run**
```bash
python -m app.location.google_places_service "VIT Chennai"           # check the server key
python scripts/plan_mission.py "Inspect AB1 of VIT Chennai at 30 meters" --home <LAT> <LON>
python -m app.rag.retriever --build                                   # once, so the PDF building facts are searchable
```
**How to test:** `pytest tests/ -m "not heavy and not live_llm"` → **361 passed** (previous 314 + 21 + 22 + 4).

**Known limitations, stated plainly**
- **Not verified against live Google APIs from this sandbox** (no network to Google here). Request/response handling follows Google's current documented shapes and is tested against fakes of them; run `python -m app.location.google_places_service "VIT Chennai"` on your machine to confirm.
- **Google may only know the campus, not "AB1".** Then the result is `LOCATION_NOT_RESOLVED` — by design. Building outlines (v4 SearchDestinations) are not available for every building/region; without one the target stays a POINT and the inspection ring uses the flagged 60 m footprint assumption.
- **Browser test uses a recording stand-in for Google's JS library** — it proves the page issues the right draw/fit calls, not Google's own rendering.
- **Live telemetry/RAMR on the map** needs the flight code to share its `MissionStateManager`/drone-state provider via `MissionViewStore.attach()`; `plan_mission.py` plans and visualizes but does not fly.
- **Google terms:** coordinates may be cached ≤ 30 days (the existing cache TTL is exactly 720 h — keep it ≤ 720 while using Google); place IDs may be cached indefinitely.

### Phase 15.1 — Recognizing the building and its real boundary

When Google returns only a point (or doesn't know "AB1" at all), the building's **real outline** is now taken from OpenStreetMap building footprints (Overpass API, `app/location/building_footprints.py`), recognized in one of two ways:
1. **By name tag:** a mapped building inside the campus tagged `name`/`ref`/`alt_name`… matching the request ("AB1", "Academic Block-1", "Academic Block I"…). Source: OPENSTREETMAP, confidence computed as usual.
2. **By one coordinate:** a point already known to be the building (a Google result for it, or one line `Academic Block 1 (AB1): latitude=…, longitude=…` in a document under `data/documents/`) → the footprint that contains it. A point ≤ `FOOTPRINT_SNAP_DISTANCE_M` (15 m) outside the nearest outline is snapped to it, unless another outline is about as close; reported as a warning.

Never done: picking the building nearest the campus centre (that would be a guess). Names are compared after normalizing punctuation/spacing, and every candidate the provider returned is printed with why it was accepted or rejected. If OpenStreetMap hasn't mapped the building's outline, the target stays a POINT. Settings: `BUILDING_FOOTPRINT_PROVIDER` (`openstreetmap`/`off`), `OVERPASS_URL`, `OVERPASS_TIMEOUT_S`. Tests: `tests/test_phase15_footprints.py` (12). Full suite: **374 passed**.

### Phase 15.3–15.4 — OpenStreetMap first, then "click the building once"

- **15.3:** the building's one coordinate is asked from OpenStreetMap (Nominatim) first, then Google (`BUILDING_POINT_PROVIDERS=openstreetmap,google`); the outline containing that point becomes the boundary. A second Overpass server is tried when the first is busy (HTTP 504).
- **15.4:** if no map provider names the building (the real result for VIT Chennai's AB1: OpenStreetMap and Google both return nothing for "AB1"/"Academic Block 1"), the map opens on the campus with its OpenStreetMap outlines and asks for **one click on the building**. The click is the coordinate; the real outline around it is marked; the selection is saved to `data/verified_buildings.json` (source `USER_VERIFIED`, HIGH confidence with an outline, MEDIUM without); the mission is then planned through the unchanged pipeline. Every later request for that building is automatic. Clicks outside the campus are refused; an unmapped spot stays a point. Delete the entry to re-pick.
- Tests: `tests/test_phase15_map_pick.py` (7) + one browser test. Full suite: **387 passed**.

### Phase 15.5 — Street addresses and landmarks

Requests like "Inspect 12 Anna Salai, Chennai" or "Inspect Chennai Central railway station" now go through ONE lookup (`LocationResolver.resolve_address`) instead of the campus → building hierarchy. The intent parser has a new `address` field (only used when no institution/campus is named, so "AB1 of VIT Chennai" is unaffected). Google is asked first, then OpenStreetMap. A result that only names a whole city/area/street is rejected as too general (never its centre); the same address matching places > 1 km apart is AMBIGUOUS; the real OpenStreetMap outline around the point becomes the boundary when mapped (HIGH), otherwise it stays a point (MEDIUM). Cached like any other location. Tests: `tests/test_phase15_addresses.py` (9).

## Web UI — Mission Control

One page in the browser to **type a request → plan it → check it on the map → connect to the drone → confirm → fly**, with live telemetry, flight progress, Hold and Return to Launch.

```bash
pip install starlette uvicorn          # once (also listed in requirements.txt)
python -m app.api.main                 # opens http://127.0.0.1:8000
```

**How it works.** `app/api/main.py` (Starlette, the framework FastAPI is built on, + uvicorn) only maps HTTP routes onto `MissionControlService` (`app/api/service.py`), which runs the **existing, unchanged** pipeline: intent parser → location resolution (Google/OSM, click-once pick) → RAG → LLM planner → Phase 5 rules → location safety checks. Flying uses one long-lived MAVSDK connection (`app/mavlink/drone_link.py`) that streams telemetry and flies the accepted mission with the existing `execute_mission_on_system()` (split out of `run_mission_on_px4()`, whose behaviour is unchanged). The page (`app/ui/static/mission_control.html`) shares its map drawing with the CLI map (`app/ui/static/mission_map.js`). When a drone is connected, plans use its real home position and battery.

**Safety rules (enforced in the backend, not the browser):** only the most recent, *accepted* plan can be sent; an explicit confirmation is required; one flight at a time (no re-planning or disconnecting mid-flight); a mission whose first waypoint is more than 900 m from PX4's home is refused before upload, with the `PX4_HOME_LAT/LON` hint for SITL; the LLM never talks to the drone; the server binds to localhost only.

**Drone address:** PX4 SITL `udpin://0.0.0.0:14540` (default), Pixhawk over USB `serial://COM5:57600` — editable in the page.

**Tests:** `tests/test_ui_mission_control.py` (15 — service rules + routes, simulated drone) and `tests/test_ui_mission_control_page.py` (headless Chromium: type → plan → connect → cancel → confirm → fly → Return to Launch). A bug found on the way and fixed: a failed flight crashed the background task (`is_terminal` is a property) and left the UI stuck on "Starting"; a safety net now reports any unexpected flight error as "Stopped".

**Not verified here:** a real PX4 SITL or Pixhawk (the drone is simulated in the tests), and live Google Maps (a stand-in records the drawing calls). RAMR/CV replanning is not wired into the web flight yet — the map *would* show it (it reads the live `MissionStateManager`), but the UI flight does not start the RAMR orchestrator.

## Boundary Fix — Outline-Following Ring + Neighbour Avoidance

- **Ring shape.** With a real outline, the inspection ring now follows the building (its convex hull pushed out by the 15 m standoff, rounded corners) instead of a circle around the farthest corner. Long buildings get more points (one every 25 m, max 24). Point-only buildings keep the assumed 60 m circle.
- **Neighbours.** Other OpenStreetMap outlines near the target are treated as obstacles (`app/location/obstacles.py`). Height comes from the OSM `height` tag, else `building:levels` × 3.5 m (marked as an estimate), else unknown.
- **Ring points near a neighbour** are pulled toward the target (not closer than 8 m). If that's not enough, the point gets a minimum altitude (neighbour height + 10 m), or is marked **BLOCKED** when the neighbour's height is unknown.
- **Safety check.** Every straight leg between waypoints is checked: crossing the target below roof + 10 m, or passing within 5 m of a neighbour below its height + 10 m, rejects the mission. Unknown neighbour height = rejected. If the outline lookup fails, a warning says neighbours were *not* checked.
- **Map.** The ring is drawn as its real shape; neighbours in orange (white = height not mapped).
- Settings: `INSPECTION_MAX_POINT_SPACING_M`, `INSPECTION_MAX_RING_POINTS`, `NEIGHBOUR_CLEARANCE_M`, `INSPECTION_MIN_STANDOFF_M`, `NEIGHBOUR_SEARCH_MARGIN_M`, `OSM_METRES_PER_LEVEL`.
- Limits: the hull bridges the inside corner of L/U-shaped buildings (safe, but not hugging them); trees, poles and wires are not in the data; real Overpass data not verified from the dev sandbox.
- Tests: `tests/test_boundary_fix.py` (16).

## Building Outlines from Microsoft GlobalMLBuildingFootprints

Source: https://github.com/microsoft/GlobalMLBuildingFootprints (building footprints © Microsoft, CDLA Permissive 2.0).

- **Flow for "Inspect AB1 building, VIT Chennai":** the existing resolver gets AB1's point (OpenStreetMap/Google, as before). Its zoom-9 tile is looked up in Microsoft's `dataset-links.csv`. The polygon that **contains** that point is AB1's outline. If no polygon contains it, the nearest one within 15 m is used, unless two are about equally close. Microsoft's footprints have **no names**, so the point is what identifies the building. Nothing is guessed: if no polygon matches, OpenStreetMap is tried, and if that fails too the target stays a point.
- **Sources and order:** `BUILDING_FOOTPRINT_PROVIDER=microsoft,openstreetmap` (default). Name-tag matching (OSM only) still runs when the map provider has no point for the building.
- **Download once:** each tile (~70 km across) is downloaded the first time and stored as a SQLite file with a spatial index in `data/ms_footprints/`. Later lookups are local. You can pre-download one: `python scripts/fetch_ms_footprints.py 12.8406 80.1534 --check`.
- **Per-footprint data:** ML `confidence` (0–1) and `height` (average, -1 = none) are kept with the location (`geometry_meta`). Neighbour heights from Microsoft are labelled as ML estimates. `MS_FOOTPRINTS_MIN_CONFIDENCE` ignores weak detections (default 0, accept all).
- **Mission:** the ring follows the detected outline (boundary fix), starts at the point nearest home, and is spaced ≤ 25 m. After the plan is validated, each inspection waypoint gets a heading toward the **nearest wall**, a gimbal pitch aimed at mid-height when the height is documented (level otherwise), and a photo (`app/mission/inspection_camera.py`). These are set by code and never asked of the LLM. They are sent to PX4 as MAVSDK `yaw_deg` / `gimbal_pitch_deg` / `TAKE_PHOTO`.
- **PX4 note:** the nose follows the mission yaw only with `MIS_YAWMODE = 0`. With the default, it points at the next waypoint. The default SITL quad has no gimbal or camera, so those fields are ignored there.
- **Output to the backend:** `POST /api/plan` and `GET /api/plans/{id}` return `inspection`: the building's geocoded point, centre, detected polygon, outline source/id/confidence, ring spacing, and the final waypoints with yaw/gimbal/photo. This is the same mission that `/api/drone/send` uploads.
- **Map:** the existing Google Maps page (`app/ui/static/`) draws the detected outline in red, plus the ring, waypoints and path, with the Microsoft attribution. The project has no React/Leaflet front end.
- **Not verified from the dev sandbox:** real downloads (the storage host is blocked here). Tests use the dataset's published format: `tests/test_ms_footprints.py` (14).

## Simulation — Request to ArduCopter Simulator, Automatically

`python scripts/simulate.py "Inspect Ripon Building, Chennai at 40 meters"`

1. The planner plans the mission as usual (it stays the brain).
2. It starts Mission Planner's ArduCopter simulator (`Documents\Mission Planner\sitl\ArduCopter.exe`, default parameters from `sitl\default_params\copter.parm`) with a quadcopter at the first waypoint (or `--home LAT LON`), and opens Mission Planner.
3. It connects on TCP 5760 (the simulator waits for this), waits for GPS, uploads the mission, arms in GUIDED, switches to AUTO and starts it, then prints progress until the copter lands.
4. **Watching in Mission Planner:** it has no command-line option to connect over TCP, so once: connection list → TCP → Connect → 127.0.0.1 → 5762.

Inspection points become: fly to the point, then hold (`LOITER_TIME`, 3 s) while `CONDITION_YAW` turns to the wall, `DO_MOUNT_CONTROL` tilts the gimbal and `DO_DIGICAM_CONTROL` takes the photo (Copter ignores a waypoint's own yaw field). ArduPilot keeps home as item 0, so a home placeholder is sent first. Ctrl+C switches to RTL.

First run: open Mission Planner → Simulation → Multirotor once so it downloads the simulator, then close it. Settings: `ARDUPILOT_SITL_DIR`, `MISSION_PLANNER_EXE`, `SITL_MODEL`, `SITL_HOME_ALT_M`, `SITL_INSPECTION_HOLD_S`. Uses pymavlink (already in requirements). Not run against the real simulator from the dev sandbox; `tests/test_ardupilot_sitl.py` (5) checks the conversion and the upload handshake.

## Verified so far

- [x] Phase 1 — Environment & project setup
- [x] Phase 2 — RAG system (ChromaDB + sentence-transformers knowledge base)
- [x] Phase 3 — LLM mission generation (Groq default / Ollama alternative, schema-constrained structured output)
- [x] Phase 4/5 — Deterministic rule validation (sequencing, per-command required/forbidden fields, mission start/end ordering, Null Island warning) + LLM self-correction retry loop
- [x] Phase 6 — Mission conversion to MAVSDK `MissionPlan`/`MissionItem` objects (coordinate inference for TAKEOFF/LAND, RETURN_TO_LAUNCH-as-behavior, verified against current MAVSDK docs/source)
- [x] Phase 7/8 — PX4 SITL connection, mission upload, arm/start/monitor, accepted/rejected result (simulation only)
- [x] RAMR novelty Phase 0 — pre-implementation audit (`docs/RAMR_CV_Audit.md`)
- [x] RAMR novelty Phase 1 — Mission State Management (`app/mission/state.py`)
- [x] RAMR novelty Phase 2 — Drone State Management (`app/mavlink/drone_state.py`)
- [x] RAMR novelty Phase 3 — Event and Trigger Infrastructure (`app/trigger/`)
- [x] RAMR novelty Phase 4 — Basic Telemetry-Based RAMR (`app/replanning/replanner.py`)
- [x] RAMR novelty Phase 5 — Replanning Context Builder (`app/replanning/context_builder.py`)
- [x] RAMR novelty Phase 6 — RAG + LLM Adaptive Reasoning (`app/replanning/adaptive_reasoner.py`)
- [x] RAMR novelty Phase 7 — Replanning Safety Validation (`app/replanning/safety_validator.py`)
- [x] RAMR novelty Phase 8 — Battery Feasibility for Replanning (`app/replanning/battery_feasibility.py`)
- [x] RAMR novelty Phase 9 — Mid-Flight Mission Update Manager (`app/mavlink/mission_update_manager.py`) — ⚠️ orchestration logic fully tested against a fake; real PX4 SITL-in-the-loop verification still required on your machine, see that phase's README section
- [x] RAMR novelty Phase 10 — RAMR Integration Testing (`app/replanning/orchestrator.py`) — all 6 TEST SCENARIOS covered against fakes; ⚠️ same real-PX4-SITL verification gap as Phase 9, inherited here since this phase's `MissionUpdateManager`/MAVSDK calls are the same untested-against-real-hardware ones
- [x] RAMR/CV novelty Phase 11 — Camera Integration (`app/vision/camera_stream.py`) — pipeline verified against a synthetic frame source; ⚠️ no real Gazebo camera topic has ever been pulled from in this sandbox — see that phase's README section
- [x] RAMR/CV novelty Phase 12 — Basic Computer Vision Quality Analysis (`app/vision/camera_stream.py`, extended) — blur/exposure classification verified correct on all 4 SUCCESS CRITERIA categories using real synthetic images; ⚠️ thresholds not tuned against a real camera dataset, see that phase's README section
- [x] RAMR/CV novelty Phase 13 — CV Event Generation (`app/vision/event_generator.py`) — a bad-quality image (blurry, too dark, too bright) reliably produces a `ReplanningEvent` that reaches and activates the `TriggerEngine`, verified directly and via a real `EventBus.publish()`; ⚠️ not yet wired into `RAMROrchestrator` (Phase 10) — CV events are produced and correctly evaluated by the `TriggerEngine`, but nothing in this project currently constructs an `InspectionFrame`/runs it through the CV pipeline automatically during a live mission, see that phase's README section
- [x] RAMR/CV novelty Phase 14 — CV-Triggered Adaptive Replanning (`app/replanning/replanner.py`, extended) — a CV quality-failure event now flows all the way through `TriggerEngine → AdaptiveMissionReplanner → Safety Validator → Battery Checker → MissionUpdateManager → MAVSDK` with zero changes to `RAMROrchestrator`, verified end-to-end with a real blurry synthetic image and a fake MAVSDK `System`; ⚠️ same real-PX4-SITL verification gap as Phases 9/10 — see that phase's README section
- [x] Location resolution remaining phases A–D (confidence gate, name variants, RAG building height, perimeter ring, roof clearance/battery/RAMR geofence)
- [x] Phase 15 — Google Maps provider + auto-updating mission map (⚠️ live Google APIs not verified from sandbox)
- [x] Improvement — Real-World Coordinate & Building Resolution (`app/location/` + `app/mission/intent_parser.py` + `app/mission/location_aware_generator.py`) — natural language ("Inspect AB1 of VIT Chennai") resolves through hierarchical campus→building lookup to a verified location before the existing RAG/LLM/safety pipeline runs, with zero fabricated/hard-coded coordinates; all 6 of the improvement's own spec TEST 1-6 scenarios pass; ⚠️ real Nominatim connectivity not verified from this sandbox (network-restricted, same category as the PX4 SITL gap), and the RAG-knowledge-base building fallback path is real but currently unpopulated (no building coordinates exist yet in `data/documents/`) — see that section's README notes

## Coming next

Original Phase 9–12 plan (automated testing / SQLite logging / Streamlit UI / experimental comparison) is superseded by the RAMR + CV novelty's phase-by-phase plan, of which Phases 1–10 plus RAMR/CV Phases 11–14 and the location-resolution improvement (above) are the first implemented steps. **Real PX4 SITL verification (Phase 9's `python -m app.mavlink.mission_update_manager`, and Phase 10's `python -m app.replanning.orchestrator`, against your real SITL setup) still has not been confirmed on your machine**, and **real Nominatim connectivity for the new location-resolution improvement has likewise not been confirmed** (`python -m app.location.geocoding_service "VIT Chennai"` on a machine with real internet access) — per this project's standing pattern, please run both when you get a chance and confirm the SUCCESS CRITERIA hold. Waiting for your confirmation before making any additional changes, per this improvement's own STEP 10.
