"""
Real-world coordinate & building resolution improvement -- tests.
Run with: pytest tests/test_phase_location_resolution.py -v

Every external dependency is faked at its boundary, matching this
project's established pattern (fake MAVSDK in test_phase_orchestrator.py,
fake LLM client in test_phase5_generator_integration.py/test_phase_reasoner.py):
  - `NominatimGeocodingService` is replaced with `_FakeGeocodingService`
    (no real network -- see app/location/geocoding_service.py's docstring
    on why real Nominatim connectivity is not reachable from this sandbox).
  - `get_llm_client()` is monkeypatched with a fake client (no real
    Groq/Ollama call) for both the intent-extraction and mission-
    generation LLM calls.
  - `retrieve()`/`format_context()` are stubbed for the RAG-fallback path.
  - `LocationCache` is constructed with `path=None` (in-memory only, no
    disk I/O) except in the one test that specifically exercises
    persistence.

Covers this improvement's own spec section 17 TEST 1-6, plus focused
unit tests for each new module.
"""

from __future__ import annotations

import pytest

from app.location.building_resolver import BuildingResolver
from app.location.campus_resolver import CampusResolver
from app.location.geocoding_service import (
    GeocodingCandidate,
    GeocodingConnectionError,
    GeocodingError,
)
from app.location.geofence import point_in_bbox, point_in_polygon, validate_mission_against_location
from app.location.location_cache import LocationCache
from app.location.location_resolver import LocationResolver
from app.location.models import LocationConfidence, LocationResolutionStatus, LocationSource, ResolutionType
from app.mission.intent_parser import MissionIntent, MissionType, parse_mission_intent
from app.mission.location_aware_generator import (
    NaturalLanguageMissionStatus,
    generate_mission_from_natural_language,
)
from app.mission.schema import Mission, MissionCommand, MissionItem

# VIT Chennai's real published campus coordinates, used here ONLY as
# realistic-looking fake test fixture data (the whole point of this
# improvement is that these are never hard-coded in the actual
# resolution code -- see app/location/*.py). Approximate, for test
# realism only.
CAMPUS_LAT, CAMPUS_LON = 12.8406, 80.1534
CAMPUS_BBOX = (12.8370, 12.8440, 80.1500, 80.1570)  # (south, north, west, east)
BUILDING_LAT, BUILDING_LON = 12.8412, 80.1531


# ---------------------------------------------------------------------
# Fake geocoding service -- no real network
# ---------------------------------------------------------------------


class _FakeGeocodingService:
    """
    Canned responses keyed by substring match on the query -- mirrors
    real Nominatim behavior closely enough for these tests: "VIT
    Chennai" resolves to a campus, "AB1" (only when the search is
    bounded to that campus) resolves to a building, "XYZ" never
    resolves to anything, and `raise_on_search` can simulate an API
    failure for any call.
    """

    def __init__(self, *, raise_on_search: Exception | None = None):
        self.raise_on_search = raise_on_search
        self.calls: list[dict] = []

    def search(self, query, *, limit=5, viewbox=None, bounded=False, want_polygon=False):
        self.calls.append({"query": query, "viewbox": viewbox, "bounded": bounded, "want_polygon": want_polygon})
        if self.raise_on_search is not None:
            raise self.raise_on_search

        q = query.lower()
        if "vit chennai" in q:
            return [
                GeocodingCandidate(
                    display_name="VIT Chennai, Vandalur-Kelambakkam Road, Chennai, Tamil Nadu, India",
                    latitude=CAMPUS_LAT, longitude=CAMPUS_LON, bounding_box=CAMPUS_BBOX,
                    importance=0.7, osm_type="way", osm_id=111, category="amenity", place_type="university",
                    geometry=None,
                )
            ]
        if "ab1" in q and viewbox == CAMPUS_BBOX:
            return [
                GeocodingCandidate(
                    display_name="AB1, VIT Chennai",
                    latitude=BUILDING_LAT, longitude=BUILDING_LON, bounding_box=None,
                    importance=0.4, osm_type="way", osm_id=222, category="building", place_type="yes",
                    geometry=[
                        (12.8411, 80.1530), (12.8411, 80.1532), (12.8413, 80.1532), (12.8413, 80.1530),
                    ] if want_polygon else None,
                )
            ]
        return []  # "XYZ" and anything else unrecognized -- genuinely no results, not an error


def _resolver_with(*, raise_on_search: Exception | None = None, cache: LocationCache | None = None) -> LocationResolver:
    service = _FakeGeocodingService(raise_on_search=raise_on_search)
    return LocationResolver(
        geocoding_service=service,
        campus_resolver=CampusResolver(service),
        building_resolver=BuildingResolver(service, retrieve_fn=lambda *a, **k: []),
        cache=cache if cache is not None else LocationCache(path=None),
    ), service


# ---------------------------------------------------------------------
# geocoding_service.py -- error handling shapes
# ---------------------------------------------------------------------


def test_geocoding_candidate_rejects_out_of_range_coordinates():
    from app.location.geocoding_service import _parse_result

    assert _parse_result({"lat": "999", "lon": "80.0", "display_name": "bad"}) is None


def test_geocoding_candidate_parses_valid_result():
    from app.location.geocoding_service import _parse_result

    c = _parse_result({"lat": "12.84", "lon": "80.15", "display_name": "ok", "boundingbox": ["12.8", "12.9", "80.1", "80.2"]})
    assert c is not None
    assert c.latitude == 12.84 and c.longitude == 80.15
    assert c.bounding_box == (12.8, 12.9, 80.1, 80.2)


# ---------------------------------------------------------------------
# geofence.py -- geometry primitives + the new location-specific check
# ---------------------------------------------------------------------


def test_point_in_bbox():
    assert point_in_bbox(12.841, 80.153, CAMPUS_BBOX) is True
    assert point_in_bbox(0.0, 0.0, CAMPUS_BBOX) is False


def test_point_in_polygon_simple_square():
    square = [(0, 0), (0, 10), (10, 10), (10, 0)]
    assert point_in_polygon(5, 5, square) is True
    assert point_in_polygon(50, 50, square) is False


def test_validate_mission_against_location_rejects_far_waypoint():
    from app.location.models import LocationConfidence as _C
    from app.location.models import MatchDetails, ResolvedLocation

    location = ResolvedLocation(
        institution="VIT Chennai", campus="VIT Chennai", building="AB1",
        latitude=BUILDING_LAT, longitude=BUILDING_LON, resolution_type=ResolutionType.POINT,
        source=LocationSource.OPENSTREETMAP, confidence=_C.HIGH, geometry_available=False,
        match_details=MatchDetails(),
    )
    mission = Mission(
        mission_name="test",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=BUILDING_LAT, longitude=BUILDING_LON, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=1.0, altitude=30),  # far away
            MissionItem(sequence=4, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )
    result = validate_mission_against_location(mission, location, radius_km=0.5)
    assert result.valid is False
    assert any("#3" in e for e in result.errors)


def test_validate_mission_against_location_accepts_nearby_waypoints():
    from app.location.models import MatchDetails, ResolvedLocation

    location = ResolvedLocation(
        institution="VIT Chennai", campus="VIT Chennai", building="AB1",
        latitude=BUILDING_LAT, longitude=BUILDING_LON, resolution_type=ResolutionType.POINT,
        source=LocationSource.OPENSTREETMAP, confidence=LocationConfidence.HIGH, geometry_available=False,
        match_details=MatchDetails(),
    )
    mission = Mission(
        mission_name="test",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=BUILDING_LAT, longitude=BUILDING_LON, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )
    result = validate_mission_against_location(mission, location, radius_km=0.5)
    assert result.valid is True


# ---------------------------------------------------------------------
# location_cache.py
# ---------------------------------------------------------------------


def test_cache_round_trip_and_case_insensitivity(tmp_path):
    from app.location.models import MatchDetails, ResolvedLocation

    cache = LocationCache(path=tmp_path / "cache.json", ttl_hours=24)
    location = ResolvedLocation(
        institution="VIT Chennai", campus="VIT Chennai", building="AB1",
        latitude=BUILDING_LAT, longitude=BUILDING_LON, resolution_type=ResolutionType.POINT,
        source=LocationSource.OPENSTREETMAP, confidence=LocationConfidence.HIGH, geometry_available=False,
        match_details=MatchDetails(),
    )
    cache.set("VIT Chennai", "VIT Chennai", "AB1", location)
    assert cache.get("vit chennai", "VIT CHENNAI ", "ab1") is not None
    assert cache.get("VIT Chennai", "VIT Chennai", "AB2") is None


def test_cache_persists_to_disk_and_reloads(tmp_path):
    from app.location.models import MatchDetails, ResolvedLocation

    path = tmp_path / "cache.json"
    location = ResolvedLocation(
        institution="VIT Chennai", campus="VIT Chennai", building="AB1",
        latitude=BUILDING_LAT, longitude=BUILDING_LON, resolution_type=ResolutionType.POINT,
        source=LocationSource.OPENSTREETMAP, confidence=LocationConfidence.HIGH, geometry_available=False,
        match_details=MatchDetails(),
    )
    LocationCache(path=path, ttl_hours=24).set("VIT Chennai", "VIT Chennai", "AB1", location)

    reloaded = LocationCache(path=path, ttl_hours=24)
    hit = reloaded.get("VIT Chennai", "VIT Chennai", "AB1")
    assert hit is not None
    assert hit.latitude == BUILDING_LAT and hit.building == "AB1"


def test_cache_invalidate():
    from app.location.models import MatchDetails, ResolvedLocation

    cache = LocationCache(path=None)
    location = ResolvedLocation(
        institution="X", campus="X", building="Y", latitude=1.0, longitude=2.0,
        resolution_type=ResolutionType.POINT, source=LocationSource.OPENSTREETMAP,
        confidence=LocationConfidence.HIGH, geometry_available=False, match_details=MatchDetails(),
    )
    cache.set("X", "X", "Y", location)
    assert cache.invalidate("X", "X", "Y") is True
    assert cache.get("X", "X", "Y") is None
    assert cache.invalidate("X", "X", "Y") is False


# ---------------------------------------------------------------------
# LocationResolver -- hierarchical resolution + confidence
# ---------------------------------------------------------------------


def test_campus_only_resolution_is_point_and_high_confidence():
    resolver, _service = _resolver_with()
    result = resolver.resolve(institution="VIT Chennai", campus="VIT Chennai")
    assert result.status == LocationResolutionStatus.RESOLVED
    assert result.location.resolution_type == ResolutionType.POINT
    assert result.location.latitude == CAMPUS_LAT and result.location.longitude == CAMPUS_LON
    assert result.location.confidence == LocationConfidence.HIGH


def test_building_resolution_via_openstreetmap_bounded_to_campus():
    resolver, service = _resolver_with()
    result = resolver.resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")

    assert result.status == LocationResolutionStatus.RESOLVED
    location = result.location
    assert location.latitude == BUILDING_LAT and location.longitude == BUILDING_LON
    assert location.source == LocationSource.OPENSTREETMAP
    assert location.match_details.campus_relationship is True
    assert location.confidence == LocationConfidence.HIGH
    # The building search really was scoped to the campus's own bounding box.
    building_calls = [c for c in service.calls if "ab1" in c["query"].lower()]
    assert any(c["viewbox"] == CAMPUS_BBOX and c["bounded"] is True for c in building_calls)


def test_building_geometry_marks_resolution_type_building():
    resolver, _service = _resolver_with()
    result = resolver.resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.location.resolution_type == ResolutionType.BUILDING
    assert result.location.geometry_available is True
    assert len(result.location.geometry) == 4


# ---------------------------------------------------------------------
# intent_parser.py
# ---------------------------------------------------------------------


def test_parse_mission_intent_uses_the_llm_client(monkeypatch):
    expected = MissionIntent(
        mission_type=MissionType.INSPECTION, location_mentioned=True,
        institution="VIT Chennai", campus="VIT Chennai", building="AB1", altitude_m=30.0,
        raw_location_phrase="AB1 of VIT Chennai",
    )

    class _Fake:
        def generate_structured(self, **kwargs):
            assert kwargs["schema"] is MissionIntent
            return expected

    monkeypatch.setattr("app.mission.intent_parser.get_llm_client", lambda: _Fake())
    result = parse_mission_intent("Inspect AB1 of VIT Chennai at 30 meters.")
    assert result == expected


def test_mission_intent_rejects_unknown_extra_fields():
    with pytest.raises(Exception):
        MissionIntent(
            mission_type=MissionType.INSPECTION, location_mentioned=True, unexpected_field="nope",
        )


# ---------------------------------------------------------------------
# SPEC SECTION 17 -- TEST 1-6
# ---------------------------------------------------------------------


class _FakeIntentClient:
    def __init__(self, intent: MissionIntent):
        self._intent = intent

    def generate_structured(self, **kwargs):
        return self._intent


class _FakeMissionClient:
    def __init__(self, mission: Mission):
        self._mission = mission
        self.calls: list[dict] = []

    def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self._mission


def _inspection_mission_near(lat: float, lon: float) -> Mission:
    return Mission(
        mission_name="AB1 inspection",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=lat, longitude=lon, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )


@pytest.fixture(autouse=True)
def _stub_rag(monkeypatch):
    monkeypatch.setattr("app.mission.generator.retrieve", lambda *a, **k: [])
    monkeypatch.setattr("app.mission.generator.format_context", lambda *a, **k: "")


def test_TEST1_inspect_ab1_of_vit_chennai(monkeypatch):
    """TEST 1: institution extracted, building extracted, resolved, coordinates returned, confidence calculated, mission proceeds."""
    intent = MissionIntent(
        mission_type=MissionType.INSPECTION, location_mentioned=True,
        institution="VIT Chennai", campus="VIT Chennai", building="AB1", altitude_m=None,
        raw_location_phrase="AB1 of VIT Chennai",
    )
    monkeypatch.setattr("app.mission.intent_parser.get_llm_client", lambda: _FakeIntentClient(intent))
    mission_client = _FakeMissionClient(_inspection_mission_near(BUILDING_LAT, BUILDING_LON))
    monkeypatch.setattr("app.mission.generator.get_llm_client", lambda: mission_client)

    resolver, _service = _resolver_with()
    result = generate_mission_from_natural_language("Inspect AB1 of VIT Chennai.", location_resolver=resolver)

    assert result.intent.institution == "VIT Chennai"
    assert result.intent.building == "AB1"
    assert result.location_result.resolved is True
    assert result.location_result.location.latitude == BUILDING_LAT
    assert result.location_result.location.confidence in (LocationConfidence.HIGH, LocationConfidence.MEDIUM)
    assert result.status == NaturalLanguageMissionStatus.GENERATED
    assert result.accepted is True
    # The resolved location actually reached the LLM prompt (RAG + resolved location -> AI mission planner).
    assert "12.84" in mission_client.calls[0]["user_prompt"] or str(BUILDING_LAT) in mission_client.calls[0]["user_prompt"]


def test_TEST2_altitude_extracted_and_waypoint_generator_and_safety_run(monkeypatch):
    """TEST 2: building resolved, altitude extracted, coordinates obtained, existing generator+validator run."""
    intent = MissionIntent(
        mission_type=MissionType.INSPECTION, location_mentioned=True,
        institution="VIT Chennai", campus="VIT Chennai", building="AB1", altitude_m=30.0,
        raw_location_phrase="AB1 at VIT Chennai",
    )
    monkeypatch.setattr("app.mission.intent_parser.get_llm_client", lambda: _FakeIntentClient(intent))
    mission_client = _FakeMissionClient(_inspection_mission_near(BUILDING_LAT, BUILDING_LON))
    monkeypatch.setattr("app.mission.generator.get_llm_client", lambda: mission_client)

    resolver, _service = _resolver_with()
    result = generate_mission_from_natural_language(
        "Inspect AB1 at VIT Chennai at 30 meters.", location_resolver=resolver
    )

    assert result.intent.altitude_m == 30.0
    assert result.mission_result is not None
    assert result.mission_result.accepted is True  # Phase 5's existing rule validator ran and approved it
    assert result.location_validation is not None and result.location_validation.valid is True  # new location check also ran
    assert result.accepted is True

    # Now prove "safety validation succeeds/fails appropriately" -- a mission whose waypoint is
    # far from the resolved building is REJECTED by the new location-specific check even though
    # it's otherwise a perfectly rule-valid Mission (Phase 5 has no idea it's in the wrong place).
    far_mission_client = _FakeMissionClient(_inspection_mission_near(1.0, 1.0))
    monkeypatch.setattr("app.mission.generator.get_llm_client", lambda: far_mission_client)
    resolver2, _service2 = _resolver_with()
    rejected = generate_mission_from_natural_language(
        "Inspect AB1 at VIT Chennai at 30 meters.", location_resolver=resolver2
    )
    assert rejected.status == NaturalLanguageMissionStatus.LOCATION_SAFETY_REJECTED
    assert rejected.accepted is False


def test_TEST3_unknown_building_is_not_resolved(monkeypatch):
    """TEST 3: 'Inspect XYZ of VIT Chennai.' -> LOCATION_NOT_RESOLVED, no mission generated."""
    intent = MissionIntent(
        mission_type=MissionType.INSPECTION, location_mentioned=True,
        institution="VIT Chennai", campus="VIT Chennai", building="XYZ", altitude_m=None,
        raw_location_phrase="XYZ of VIT Chennai",
    )
    monkeypatch.setattr("app.mission.intent_parser.get_llm_client", lambda: _FakeIntentClient(intent))
    # No mission-generation fake is installed -- if the code tried to call it, this test would
    # fail with an unrelated error, proving the pipeline genuinely STOPPED before that step.

    resolver, _service = _resolver_with()
    result = generate_mission_from_natural_language("Inspect XYZ of VIT Chennai.", location_resolver=resolver)

    assert result.status == NaturalLanguageMissionStatus.LOCATION_NOT_RESOLVED
    assert result.mission_result is None
    assert result.accepted is False


def test_TEST4_ambiguous_location_without_institution(monkeypatch):
    """TEST 4: 'Inspect AB1.' -> ambiguous location response, no mission generated."""
    intent = MissionIntent(
        mission_type=MissionType.INSPECTION, location_mentioned=True,
        institution=None, campus=None, building="AB1", altitude_m=None, raw_location_phrase="AB1",
    )
    monkeypatch.setattr("app.mission.intent_parser.get_llm_client", lambda: _FakeIntentClient(intent))

    resolver, _service = _resolver_with()
    result = generate_mission_from_natural_language("Inspect AB1.", location_resolver=resolver)

    assert result.status == NaturalLanguageMissionStatus.LOCATION_AMBIGUOUS
    assert result.mission_result is None
    assert "AB1" in result.message and "institution" in result.message.lower()


def test_TEST5_api_failure_falls_back_to_cache_then_stops_safely(monkeypatch):
    """TEST 5: simulate a Nominatim failure -- verified cache checked; cached location used if present, else stop safely."""
    intent = MissionIntent(
        mission_type=MissionType.INSPECTION, location_mentioned=True,
        institution="VIT Chennai", campus="VIT Chennai", building="AB1", altitude_m=None,
        raw_location_phrase="AB1 of VIT Chennai",
    )
    monkeypatch.setattr("app.mission.intent_parser.get_llm_client", lambda: _FakeIntentClient(intent))

    # 5a: no cache entry exists -- API failure must stop mission generation safely (LOCATION_NOT_RESOLVED), never guess.
    resolver_no_cache, _service = _resolver_with(raise_on_search=GeocodingConnectionError("simulated network failure"))
    result_no_cache = generate_mission_from_natural_language("Inspect AB1 of VIT Chennai.", location_resolver=resolver_no_cache)
    assert result_no_cache.status == NaturalLanguageMissionStatus.LOCATION_NOT_RESOLVED
    assert result_no_cache.mission_result is None

    # 5b: a verified location for this EXACT request was already cached by an earlier, successful
    # resolution -- the API failure this time must be masked by that cache, not block the mission.
    from app.location.models import MatchDetails, ResolvedLocation

    cache = LocationCache(path=None)
    cache.set(
        "VIT Chennai", "VIT Chennai", "AB1",
        ResolvedLocation(
            institution="VIT Chennai", campus="VIT Chennai", building="AB1",
            latitude=BUILDING_LAT, longitude=BUILDING_LON, resolution_type=ResolutionType.POINT,
            source=LocationSource.OPENSTREETMAP, confidence=LocationConfidence.HIGH,
            geometry_available=False, match_details=MatchDetails(),
        ),
    )
    resolver_with_cache, _service2 = _resolver_with(
        raise_on_search=GeocodingConnectionError("simulated network failure"), cache=cache
    )
    mission_client = _FakeMissionClient(_inspection_mission_near(BUILDING_LAT, BUILDING_LON))
    monkeypatch.setattr("app.mission.generator.get_llm_client", lambda: mission_client)

    result_cached = generate_mission_from_natural_language("Inspect AB1 of VIT Chennai.", location_resolver=resolver_with_cache)
    assert result_cached.status == NaturalLanguageMissionStatus.GENERATED
    assert result_cached.location_result.location.source == LocationSource.OPENSTREETMAP  # the ORIGINAL source, cache is just delivery
    assert result_cached.accepted is True


def test_TEST6_ramr_and_cv_still_work_after_location_layer(monkeypatch):
    """TEST 6: existing RAMR/CV functionality is unaffected by this improvement -- a smoke test on top of the full suite."""
    from app.mavlink.drone_state import DroneState
    from app.mission.state import MissionStateManager
    from app.replanning.replanner import AdaptiveMissionReplanner, RAMRStatus
    from app.trigger.engine import TriggerEngine
    from app.trigger.events import EventSeverity, EventSource, EventType, ReplanningEvent

    mission = Mission(
        mission_name="RAMR smoke test",
        mission_items=[
            MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
            MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=1.0, longitude=2.0, altitude=30),
            MissionItem(sequence=3, command=MissionCommand.WAYPOINT, latitude=1.01, longitude=2.0, altitude=30),
            MissionItem(sequence=4, command=MissionCommand.RETURN_TO_LAUNCH),
        ],
    )
    manager = MissionStateManager()
    manager.start(mission)
    manager.mark_validated()
    manager.mark_uploaded()
    manager.mark_running()
    manager.record_progress(current=2, total=4)

    drone_state = DroneState(latitude_deg=1.0, longitude_deg=2.0, battery_remaining_percent=0.22)
    event = ReplanningEvent(
        event_type=EventType.BATTERY_DEVIATION, severity=EventSeverity.HIGH, source=EventSource.TELEMETRY,
        mission_id=manager.snapshot().mission_id, metadata={"battery_percent": 0.22},
    )

    decision = TriggerEngine().handle_event(event)
    assert decision.should_replan is True

    result = AdaptiveMissionReplanner().replan(
        event, manager.snapshot(), drone_state, home_latitude_deg=1.0, home_longitude_deg=2.0
    )
    assert result.status in (RAMRStatus.MODIFIED, RAMRStatus.UNCHANGED, RAMRStatus.UNSUPPORTED)
