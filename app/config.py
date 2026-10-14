"""
Central application configuration.

This is the one addition I made to the folder structure you proposed: a
single `Settings` object that every later module (rag/, llm/, mission/,
mavlink/, database/) imports instead of each one calling `os.getenv(...)`
on its own. Reasons:

1. One source of truth for every `.env` key — Phase 3's LLM_PROVIDER swap,
   Phase 5's altitude limits, Phase 7's PX4_CONNECTION_URL all live here,
   so nothing drifts out of sync with `.env.example`.
2. Pydantic validates types and required fields at startup (fail fast) --
   you get a clear crash *before* the app runs, not a cryptic `None`
   three modules deep during a PX4 SITL test.
3. Every later phase can just do:
       from app.config import settings
   No new pattern to learn per phase.

Nothing here is PX4/MAVSDK/LLM-specific logic — it only loads and
validates configuration. Phase 2+ modules will import `settings` and use
its fields.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute path to the project root (the directory containing this app/ package).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    All configuration for the project, loaded from `.env` (see
    `.env.example` for the documented template) with sane defaults so the
    app still boots — in a degraded, clearly-logged way — if `.env` is
    missing a key added by a later phase.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM (Phase 3) ---
    # "groq" is the default: it's a free hosted API (fast inference,
    # no local RAM/VRAM needed), which sidesteps the local machine's RAM
    # ceiling that `ollama` hit during testing. `ollama` stays fully
    # implemented and configurable -- switch LLM_PROVIDER back to it any
    # time local/offline inference is preferred.
    llm_provider: str = "groq"
    llm_model: str = "openai/gpt-oss-20b"
    ollama_host: str = "http://localhost:11434"
    groq_api_key: str = ""
    openai_api_key: str = ""

    # --- RAG (Phase 2) ---
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    chroma_persist_dir: str = "./data/chroma_db"
    chroma_collection_name: str = "drone_mission_docs"
    rag_top_k: int = 4

    # --- Mission validation (Phase 5) ---
    min_altitude_m: float = 2
    max_altitude_m: float = 120
    max_waypoints: int = 50

    # --- PX4 SITL / MAVSDK (Phase 6-8) ---
    # "udpin://0.0.0.0:14540" -- verified against the current MAVSDK docs
    # (mavsdk.mavlink.io/main/en/cpp/guide/connections.html) while writing
    # Phase 6: the old bare "udp://:14540" form used as a placeholder in
    # earlier phases is NOT a documented scheme for this MAVSDK version
    # (3.17.2, matching requirements.txt) -- current MAVSDK wants an
    # explicit direction, `udpin://` for "listen for the autopilot to
    # connect to us", which is what PX4 SITL does on its default
    # MAVSDK-facing port 14540. Fixed here before any Phase 7 code tries
    # to actually call `drone.connect(system_address=...)` with it.
    px4_connection_url: str = "udpin://0.0.0.0:14540"
    px4_connect_timeout_s: int = 30

    # --- Mission conversion (Phase 6) ---
    # Our Mission schema deliberately has no per-waypoint speed (Phase 3/4
    # scope), but MAVSDK's MissionItem requires a speed_m_s for every
    # item. This is a documented assumption, not something verified
    # against any spec -- a reasonable cruise speed for a small
    # multicopter, overridable in .env without touching code.
    mission_default_speed_ms: float = 5.0

    # --- RAMR novelty Phase 4 -- deterministic battery-feasibility estimator ---
    # Deliberately simple placeholders (documented as such in
    # app/replanning/replanner.py), NOT a validated energy/physics model --
    # min_battery_reserve_percent is the floor RAMR will not plan below,
    # and battery_consumption_percent_per_km is a flat per-distance cost
    # used only to decide how many remaining waypoints are still safely
    # reachable before RTH is forced. Both are fractions (0.15 = 15%),
    # matching the 0..1 `remaining_percent` convention already used by
    # MAVSDK's Battery telemetry (see app/mavlink/drone_state.py).
    min_battery_reserve_percent: float = 0.15
    battery_consumption_percent_per_km: float = 0.02

    # --- RAMR novelty Phase 7 -- replanning safety validation ---
    # No per-waypoint speed exists anywhere in this project's schema (see
    # app/mission/converter.py's docstring, point 1) -- every mission
    # flies at the single configured `mission_default_speed_ms` above.
    # `max_safe_speed_ms` is the ceiling Phase 7's safety validator checks
    # that configured speed against. A documented assumption (a
    # reasonable cruise-speed ceiling for a small multicopter), not a
    # verified vehicle limit -- overridable in .env without touching code.
    max_safe_speed_ms: float = 12.0

    # --- RAMR/CV novelty Phase 12 -- basic image quality analysis ---
    # All three are documented heuristic defaults, not validated against
    # any real inspection-camera dataset (see app/vision/camera_stream.py):
    #   - blur_variance_threshold: OpenCV Laplacian-variance blur score
    #     BELOW this is classified blurry. 100.0 is the commonly-cited
    #     starting point for this exact technique (variance of the
    #     Laplacian as a focus measure) -- e.g. PyImageSearch's "Blur
    #     detection with OpenCV" -- but the same source is explicit that
    #     it needs re-tuning per camera/scene, so this is overridable in
    #     .env, not a universal constant.
    #   - underexposed_mean_threshold / overexposed_mean_threshold: mean
    #     grayscale pixel intensity (0-255 scale) below/above which a
    #     frame is classified TOO_DARK/TOO_BRIGHT. 60/200 are reasonable,
    #     documented starting points (leaving a wide 60-200 "normal" band
    #     out of a 0-255 range), not derived from any cited source or
    #     validated dataset -- overridable in .env.
    blur_variance_threshold: float = 100.0
    underexposed_mean_threshold: float = 60.0
    overexposed_mean_threshold: float = 200.0

    # --- RAMR/CV novelty Phase 14 -- CV-triggered adaptive replanning ---
    # How long (seconds) the corrective revisit waypoint
    # (`AdaptiveMissionReplanner._handle_cv_quality_event()` in
    # app/replanning/replanner.py) hovers over the flagged location --
    # modeled as a LOITER mission item (the only "hover and hold"
    # command this project's schema already has, see app/mission/schema.py)
    # so a fresh, hopefully-in-focus/correctly-exposed frame can be
    # captured. A documented, reasonable default (long enough for a
    # camera to settle and grab another frame), not a value from any
    # spec or camera datasheet -- overridable in .env.
    cv_revisit_hold_time_s: float = 5.0

    # --- Location resolution improvement -- real-world coordinate & building resolution ---
    # Nominatim's usage policy (https://operations.osmfoundation.org/policies/nominatim/)
    # REQUIRES a descriptive User-Agent identifying the application (and
    # ideally a contact) -- not a browser-style UA string. The default
    # below is a placeholder; overridable in .env, and you should put a
    # real contact email in it before making real requests, per that
    # policy. Also required by the same policy: no more than 1 request/
    # second, enforced by `nominatim_min_request_interval_s` in
    # app/location/geocoding_service.py.
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    nominatim_user_agent: str = (
        "drone-mission-planner/1.0 (set NOMINATIM_USER_AGENT in .env to your own "
        "app name + contact email, per Nominatim's usage policy)"
    )
    nominatim_timeout_s: float = 10.0
    nominatim_min_request_interval_s: float = 1.0

    # How far (km) a mission's coordinate-bearing waypoints may be from a
    # resolved building/campus location before the new location-specific
    # safety check (app/location/geofence.py) rejects the mission -- a
    # documented, reasonable default for a close-in building inspection,
    # NOT a real campus boundary (this project has no verified polygon
    # geofence data source beyond whatever OpenStreetMap returns for a
    # specific resolved building). Overridable in .env; does not replace
    # or loosen `app.replanning.safety_validator`'s own geofence check
    # (Phase 7), which is unrelated and still the final authority once a
    # mission is airborne and being replanned.
    location_geofence_radius_km: float = 0.5

    # Where resolved locations are cached (app/location/location_cache.py)
    # -- a plain JSON file, same persistence idea as `chroma_persist_dir`
    # above, so repeated requests for the same building don't re-hit
    # Nominatim every time (its usage policy explicitly asks callers to
    # cache). `location_cache_ttl_hours` is a documented, reasonable
    # default (buildings essentially never move) -- overridable.
    location_cache_path: str = "./data/location_cache.json"
    location_cache_ttl_hours: float = 720.0  # 30 days

    # --- Location resolution improvement, remaining phases (A-D) ---
    # Phase A: minimum confidence a resolved location must reach before
    # mission planning continues (spec section 7: "Only continue to
    # mission planning when the location is sufficiently verified").
    # One of HIGH / MEDIUM / LOW. MEDIUM by default: RAG-sourced
    # locations are capped at MEDIUM (see location_resolver.py), so they
    # still pass; a LOW-confidence OSM match does not.
    location_min_confidence: str = "MEDIUM"
    # Phase A: campus building-code abbreviations, used ONLY to widen the
    # NAME search (e.g. "AB1" -> also search "Academic Block 1") in
    # OpenStreetMap and the RAG knowledge base. A naming convention, never
    # a coordinate -- nothing here can place a building anywhere by itself.
    # Overridable in .env as JSON, e.g. BUILDING_ALIAS_PREFIXES='{"AB": "Academic Block"}'.
    building_alias_prefixes: dict[str, str] = {"AB": "Academic Block"}
    # Phase B/D: vertical clearance (m) required above a building's known
    # roof height for any waypoint that is over (or, for a point-only
    # resolution, possibly over) the building. Documented assumption,
    # not from a regulation -- overridable.
    building_roof_clearance_m: float = 10.0
    # Phase C/D: when only a POINT (no footprint polygon) is known for a
    # building, its footprint is ASSUMED to fit within this radius (m) of
    # that point. Deliberately generous so safety checks err on the
    # cautious side; an explicit, documented assumption -- not a
    # measurement. A real OSM footprint always replaces it.
    building_unknown_footprint_radius_m: float = 60.0
    # Phase C: horizontal standoff (m) between the building footprint and
    # the perimeter-inspection reference ring, and how many evenly-spaced
    # reference points that ring has (8 = one every 45 degrees).
    inspection_standoff_m: float = 15.0
    inspection_ring_points: int = 8
    # Boundary fix: the ring follows the outline, so a long building gets
    # more points -- one at least every `inspection_max_point_spacing_m`
    # along the ring, never more than `inspection_max_ring_points`.
    inspection_max_point_spacing_m: float = 25.0
    inspection_max_ring_points: int = 24
    # Neighbouring buildings: a ring point / flight leg must stay more than
    # `neighbour_clearance_m` (horizontal) from any other mapped outline,
    # or fly above it. To get clear, a point may be pulled toward the target
    # but never closer than `inspection_min_standoff_m`.
    neighbour_clearance_m: float = 5.0
    inspection_min_standoff_m: float = 8.0
    # How far beyond the ring to look for neighbouring outlines (m).
    neighbour_search_margin_m: float = 40.0
    # Unknown heights: a neighbouring building with no mapped height no
    # longer stops planning. Its height is ASSUMED to be
    # `unknown_building_assumed_height_m`, the route near it is kept at least
    # that + roof clearance high, and a warning says it was assumed. Set
    # NEIGHBOUR_UNKNOWN_HEIGHT_BLOCKS=true in .env for the old strict
    # behaviour (refuse to fly next to a building of unknown height).
    neighbour_unknown_height_blocks: bool = False
    unknown_building_assumed_height_m: float = 30.0
    # A waypoint/leg below a neighbour's KNOWN height + roof clearance:
    # false (default) = reported as a warning, the plan stays accepted and
    # downloadable; true = the plan is rejected (old strict behaviour).
    neighbour_clearance_blocks: bool = False
    # OSM buildings with only `building:levels`: height = levels x this.
    # A stated estimate (typical storey + slab), labelled as such.
    osm_metres_per_level: float = 3.5
    # Camera orientation (app/mission/inspection_camera.py): waypoints within
    # this distance (m) of the building's outline are treated as inspection
    # points -- they face the nearest wall and take a photo.
    inspection_camera_max_distance_m: float = 60.0
    # Structural scan (app/mission/structural_scan.py): the building is flown
    # as several rings at increasing altitude, from the base up to the top of
    # the facade. Top = mapped building height minus a margin; if the height
    # is unknown, the requested altitude or a stated default is used (warned).
    structural_base_altitude_m: float = 5.0
    structural_level_spacing_m: float = 10.0
    structural_top_margin_m: float = 2.0
    structural_default_height_m: float = 30.0
    structural_max_levels: int = 5
    structural_gimbal_pitch_deg: float = -10.0
    # Road scan (app/mission/road_scan.py): fly along a named road from the map
    # centreline, camera looking down and forward along the road. Altitude is the
    # requested one or road_scan_altitude_m (warned). Trees, poles and wires are
    # NOT in the map data, so the scan always asks for a visual check first.
    road_search_radius_m: float = 400.0
    road_scan_altitude_m: float = 30.0
    road_scan_spacing_m: float = 25.0
    road_scan_max_length_m: float = 600.0
    road_scan_gimbal_pitch_deg: float = -75.0

    # --- Phase 15 -- Google Maps location resolution + mission map ---
    # Which geocoding provider `LocationResolver` uses. "auto" (default) =
    # Google when GOOGLE_MAPS_API_KEY is set, otherwise OpenStreetMap/
    # Nominatim (the earlier improvement's provider, unchanged). "google"
    # forces Google (a missing key then fails safely as LOCATION_NOT_RESOLVED);
    # "openstreetmap" forces Nominatim.
    location_provider: str = "auto"
    # SERVER-side key: Places API (New) + Geocoding API. Used only by the
    # Python backend, never sent to the browser. Restrict it in Google
    # Cloud Console to those two APIs (+ your IP if possible). Never commit it.
    google_maps_api_key: str = ""
    # BROWSER key: Maps JavaScript API only. It is necessarily visible to the
    # browser (every Maps JS key is), so it must be a SEPARATE key restricted
    # by HTTP referrer to http://localhost:<map_server_port>/* and to the
    # Maps JavaScript API only.
    google_maps_browser_api_key: str = ""
    # Map ID required by Advanced Markers. "DEMO_MAP_ID" is Google's documented
    # testing ID; create your own Map ID for anything beyond development.
    google_maps_map_id: str = "DEMO_MAP_ID"
    google_maps_timeout_s: float = 10.0
    # Optional CLDR region code to bias Google results (e.g. "IN"); empty = no bias.
    google_maps_region_code: str = ""
    # Two name-matching building candidates further apart than this (m)
    # inside the same campus are treated as AMBIGUOUS rather than picking one.
    building_ambiguity_distance_m: float = 40.0
    # Building outline sources, in order (comma-separated):
    #   "microsoft"     = Microsoft GlobalMLBuildingFootprints (ML-detected
    #                     from satellite imagery; unnamed, so matched by the
    #                     building's geocoded point -- app/location/ms_footprints.py)
    #   "openstreetmap" = OSM via Overpass (also recognizes buildings by name tag)
    #   "off"           = never fetch outlines (points stay points).
    building_footprint_provider: str = "microsoft,openstreetmap"
    # Microsoft GlobalMLBuildingFootprints: the index CSV (comma-separated
    # mirrors, tried in order -- the repo README says hosting moved to
    # bfppub.blob.core.windows.net; the older URL is kept as a fallback),
    # where downloaded tiles are kept (one SQLite file per level-9 tile),
    # download timeout, and how long the cached index is trusted.
    ms_footprints_index_url: str = (
        "https://bfppub.blob.core.windows.net/%24web/2026-08-13/dataset-links.csv,"
        "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
    )
    ms_footprints_cache_dir: str = "data/ms_footprints"
    ms_footprints_timeout_s: float = 120.0
    ms_footprints_index_max_age_days: float = 30.0
    # Footprints below this ML confidence (0-1) are not used as the target's
    # outline. 0 = accept all (each footprint's confidence is still shown).
    ms_footprints_min_confidence: float = 0.0
    # Comma-separated; the next endpoint is tried when one is busy (HTTP 504) or unreachable.
    overpass_url: str = "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter"
    overpass_timeout_s: float = 25.0
    # A building point at most this far (m) outside the nearest outline is
    # snapped to it (Google pins often sit at an entrance), unless another
    # outline is about as close -- then it is NOT snapped.
    footprint_snap_distance_m: float = 15.0
    # Phase 15.3: which map providers are asked for the BUILDING's one
    # coordinate, in order. OpenStreetMap first (it often names campus
    # buildings that Google doesn't), then Google. The campus itself still
    # comes from LOCATION_PROVIDER.
    building_point_providers: str = "openstreetmap,google"
    # Phase 15.4: buildings the user verified by clicking them once on the
    # mission map (app/location/verified_buildings.py). Not a cache -- kept
    # until you delete the entry or the file.
    verified_buildings_path: str = "./data/verified_buildings.json"
    # Phase 15.5: two results for the same street address/landmark further
    # apart than this (m) are different places -> AMBIGUOUS, not a silent pick.
    address_ambiguity_distance_m: float = 1000.0
    # --- Web UI (mission control: app/api/main.py, Starlette + uvicorn; localhost only) ---
    ui_host: str = "127.0.0.1"
    ui_port: int = 8000
    # PX4 refuses a mission whose first waypoint is farther than MIS_DIST_1WP
    # (900 m by default) from home -- checked before upload, with advice.
    max_first_waypoint_distance_m: float = 900.0

    # --- ArduCopter simulator (Mission Planner's SITL), app/sim/ardupilot_sitl.py ---
    # Folder holding ArduCopter.exe + default_params\copter.parm; empty = Documents\Mission Planner\sitl.
    ardupilot_sitl_dir: str = ""
    sitl_work_dir: str = "data/sitl"          # simulator's own files (eeprom, logs) -- kept apart from Mission Planner's
    sitl_model: str = "quad"
    sitl_speedup: int = 1
    sitl_home_alt_m: float = 10.0             # home altitude above sea level (the simulated ground is flat)
    sitl_connection: str = "tcp:127.0.0.1:5760"   # the planner's link; Mission Planner can watch on TCP 5762
    sitl_inspection_hold_s: float = 3.0       # seconds held at each inspection point (turn, aim, photo)
    sitl_yaw_rate_deg_s: float = 30.0
    sitl_mission_timeout_s: float = 1200.0
    # MissionPlanner.exe; empty = the usual install folders under Program Files.
    mission_planner_exe: str = ""
    # Local mission-map server (app/ui/map_server.py). Binds to localhost only.
    map_server_host: str = "127.0.0.1"
    map_server_port: int = 8765

    # --- Database (Phase 10) ---
    database_url: str = "sqlite:///./data/missions.db"

    # --- General ---
    log_level: str = "INFO"

    @property
    def documents_dir(self) -> Path:
        """Where Phase 2's document loader reads source knowledge-base files from."""
        return PROJECT_ROOT / "data" / "documents"

    @property
    def chroma_persist_path(self) -> Path:
        """Resolved, project-root-relative path for the Chroma vector store."""
        p = Path(self.chroma_persist_dir)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def verified_buildings_full_path(self) -> Path:
        p = Path(self.verified_buildings_path)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def location_cache_full_path(self) -> Path:
        """Resolved, project-root-relative path for the location resolution cache (see app/location/location_cache.py)."""
        p = Path(self.location_cache_path)
        return p if p.is_absolute() else (PROJECT_ROOT / p)


# Import this singleton everywhere: `from app.config import settings`
settings = Settings()
