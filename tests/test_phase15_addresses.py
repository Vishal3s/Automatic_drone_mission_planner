"""
Phase 15.5 tests -- street addresses / landmarks as ONE location lookup.
Run with: pytest tests/test_phase15_addresses.py -v

Google and Overpass are faked; coordinates are FAKE TEST FIXTURES.
"""

from __future__ import annotations

from app.location.location_cache import LocationCache
from app.location.location_resolver import LocationResolver
from app.location.google_places_service import GooglePlacesGeocodingService
from app.location.models import LocationResolutionStatus, LocationSource
from app.mission.intent_parser import MissionIntent, MissionType
from tests.test_phase15_footprints import _FakeOverpass, _fp
from tests.test_phase15_google_maps import _Resp

OFFICE = {"id": "addr1", "displayName": {"text": "12 Anna Salai"}, "formattedAddress": "12, Anna Salai, Chennai, India",
          "location": {"latitude": 13.0600, "longitude": 80.2600}, "types": ["street_address"]}
OFFICE_RING = [(13.0599, 80.2599), (13.0599, 80.2601), (13.0601, 80.2601), (13.0601, 80.2599)]
CITY = {"id": "city", "displayName": {"text": "Chennai"}, "formattedAddress": "Chennai, Tamil Nadu, India",
        "location": {"latitude": 13.08, "longitude": 80.27}, "primaryType": "locality", "types": ["locality", "political"]}
OTHER_CITY = dict(OFFICE, id="addr2", formattedAddress="12, Anna Salai, Madurai, India",
                  location={"latitude": 9.92, "longitude": 78.12})
SHOP_INSIDE = dict(OFFICE, id="addr3", displayName={"text": "Shop at 12 Anna Salai"},
                   location={"latitude": 13.0601, "longitude": 80.2601})


class _Google:
    def __init__(self, places, fail=False):
        self.places, self.fail, self.calls = places, fail, 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        if self.fail:
            import requests
            raise requests.exceptions.ConnectionError("down")
        return _Resp(200, {"places": self.places} if self.places else {})


def _resolver(google, overpass=None):
    service = GooglePlacesGeocodingService(api_key="k", session=google)
    return LocationResolver(geocoding_service=service, cache=LocationCache(path=None), footprint_service=overpass)


def test_street_address_resolves_with_real_outline():
    r = _resolver(_Google([OFFICE]), _FakeOverpass([_fp("way/9", OFFICE_RING)]))
    result = r.resolve_address("12 Anna Salai, Chennai")
    loc = result.location
    assert result.status == LocationResolutionStatus.RESOLVED
    assert loc.source == LocationSource.GOOGLE_PLACES and loc.place_id == "addr1"
    assert loc.geometry == OFFICE_RING and loc.confidence.value == "HIGH"


def test_address_without_mapped_outline_stays_a_point():
    loc = _resolver(_Google([OFFICE]), _FakeOverpass([])).resolve_address("12 Anna Salai, Chennai").location
    assert loc.geometry is None and loc.resolution_type.value == "POINT" and loc.confidence.value == "MEDIUM"


def test_whole_city_is_too_vague_never_its_centre():
    result = _resolver(_Google([CITY])).resolve_address("Chennai")
    assert result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED
    assert "general" in result.message and result.location is None


def test_same_address_in_two_cities_is_ambiguous():
    result = _resolver(_Google([OFFICE, OTHER_CITY])).resolve_address("12 Anna Salai")
    assert result.status == LocationResolutionStatus.AMBIGUOUS and "Madurai" in result.message


def test_nearby_results_are_the_same_place_not_ambiguous():
    result = _resolver(_Google([OFFICE, SHOP_INSIDE])).resolve_address("12 Anna Salai, Chennai")
    assert result.status == LocationResolutionStatus.RESOLVED and result.location.place_id == "addr1"


def test_provider_down_stops_safely():
    result = _resolver(_Google([OFFICE], fail=True)).resolve_address("12 Anna Salai, Chennai")
    assert result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED and "unavailable" in result.message


def test_second_lookup_comes_from_cache():
    google = _Google([OFFICE])
    r = _resolver(google)
    r.resolve_address("12 Anna Salai, Chennai")
    calls = google.calls
    assert r.resolve_address("12 Anna Salai, Chennai").status == LocationResolutionStatus.RESOLVED
    assert google.calls == calls


def test_parser_keeps_campus_requests_out_of_address_mode(monkeypatch):
    import app.mission.intent_parser as ip

    class _Client:
        def __init__(self, intent):
            self.intent = intent

        def generate_structured(self, **kw):
            return self.intent

    both = MissionIntent(mission_type=MissionType.INSPECTION, location_mentioned=True, institution="VIT Chennai",
                         campus="VIT Chennai", building="AB1", address="AB1 of VIT Chennai")
    monkeypatch.setattr(ip, "get_llm_client", lambda: _Client(both))
    out = ip.parse_mission_intent("Inspect AB1 of VIT Chennai")
    assert out.address is None and out.building == "AB1"

    addr = MissionIntent(mission_type=MissionType.INSPECTION, location_mentioned=True, building="12",
                         address="12 Anna Salai, Chennai")
    monkeypatch.setattr(ip, "get_llm_client", lambda: _Client(addr))
    out = ip.parse_mission_intent("Inspect 12 Anna Salai, Chennai")
    assert out.address == "12 Anna Salai, Chennai" and out.building is None


def test_address_request_plans_a_mission_end_to_end(monkeypatch):
    import app.mission.location_aware_generator as lag
    from app.mission.generator import MissionGenerationResult
    from app.mission.schema import Mission, MissionCommand, MissionItem
    from app.mission.validator import validate_mission

    monkeypatch.setattr(lag, "parse_mission_intent", lambda req: MissionIntent(
        mission_type=MissionType.INSPECTION, location_mentioned=True, address="12 Anna Salai, Chennai", altitude_m=30))
    captured = {}

    def fake_generate(request, **kw):
        captured["context"] = kw.get("location_context") or ""
        items = [MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
                 MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=13.0603, longitude=80.2600, altitude=30),
                 MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH)]
        m = Mission(mission_name="Inspect 12 Anna Salai", mission_items=items)
        return MissionGenerationResult(mission=m, validation=validate_mission(m), attempts=1)

    monkeypatch.setattr(lag, "generate_validated_mission", fake_generate)
    r = _resolver(_Google([OFFICE]), _FakeOverpass([_fp("way/9", OFFICE_RING)]))
    result = lag.generate_mission_from_natural_language(
        "Inspect 12 Anna Salai, Chennai at 30 meters", location_resolver=r, building_facts_fn=lambda b, c: lag.BuildingFacts(building=b))
    assert result.status.value == "GENERATED" and result.accepted
    assert result.inspection_ring is not None and result.inspection_ring.footprint_measured
    assert "Perimeter inspection reference points" in captured["context"]
