"""
Phase 15 tests -- Google Maps location provider + mission map.
Run with: pytest tests/test_phase15_google_maps.py -v

Google's HTTP endpoints are faked at the requests-session boundary (no
network, no key needed), returning the documented response shapes of
Places API (New) Text Search and Geocoding v4 SearchDestinations.
All coordinates below are FAKE TEST FIXTURES; the code under test
contains none.

Map tests run in two layers:
  - Python: the view model (/api/mission-view) and the server endpoints.
  - Browser: tests/browser/test_mission_map_page.py drives the real HTML
    page in headless Chromium with a recording stand-in for the Google
    Maps JS library (see that file).
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from app.location.building_resolver import BuildingResolver
from app.location.campus_resolver import CampusResolver
from app.location.geocoding_service import GeocodingAuthError, GeocodingConnectionError
from app.location.google_places_service import GooglePlacesGeocodingService, _rings_from_geojson
from app.location.location_cache import LocationCache
from app.location.location_resolver import LocationResolver
from app.location.models import GeometryType, LocationResolutionStatus, LocationSource
from app.location.providers import get_geocoding_service

CAMPUS_BBOX = (12.8370, 12.8440, 80.1500, 80.1570)  # (south, north, west, east)
CAMPUS = {"id": "campus1", "displayName": {"text": "Vellore Institute of Technology Chennai"},
          "formattedAddress": "Vandalur-Kelambakkam Rd, Chennai, Tamil Nadu, India",
          "location": {"latitude": 12.8406, "longitude": 80.1534}, "primaryType": "university", "types": ["university"],
          "viewport": {"low": {"latitude": CAMPUS_BBOX[0], "longitude": CAMPUS_BBOX[2]},
                       "high": {"latitude": CAMPUS_BBOX[1], "longitude": CAMPUS_BBOX[3]}}}
AB1 = {"id": "bld1", "displayName": {"text": "Academic Block 1"}, "formattedAddress": "VIT Chennai, Chennai, India",
       "location": {"latitude": 12.8412, "longitude": 80.1531}, "primaryType": "university", "types": ["establishment"]}
AB1_OUTLINE = {"type": "Polygon", "coordinates": [[[80.1530, 12.8411], [80.1532, 12.8411], [80.1532, 12.8413], [80.1530, 12.8413], [80.1530, 12.8411]]]}


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeGoogle:
    """Stands in for requests.Session against Google's two endpoints."""

    def __init__(self, *, building_places=None, outline=AB1_OUTLINE, fail=None, campus_first=True):
        self.building_places = [AB1] if building_places is None else building_places
        self.outline, self.fail, self.campus_first = outline, fail, campus_first
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        if self.fail == "connection":
            import requests

            raise requests.exceptions.ConnectionError("network down")
        if self.fail == "bad_key":
            return _Resp(400, {"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT",
                                         "details": [{"reason": "API_KEY_INVALID"}]}})
        if self.fail == "unavailable":
            return _Resp(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "backend error"}})
        if url.endswith("places:searchText"):
            q = json["textQuery"].lower()
            if "locationRestriction" not in json and "locationBias" not in json:
                return _Resp(200, {"places": [CAMPUS]} if "vit chennai" in q or "vellore" in q else {})
            places = []
            if self.campus_first:  # Google's fuzzy search happily returns the campus itself
                places.append(CAMPUS)
            if any(t in q for t in ("ab1", "ab 1", "ab-1", "academic block 1")):
                places += self.building_places
            return _Resp(200, {"places": places} if places else {})
        if url.endswith("/destinations"):
            if json["place"] == "places/bld1" and self.outline is not None:
                return _Resp(200, {"destinations": [{"primary": {"structureType": "BUILDING", "displayPolygon": self.outline}}]})
            return _Resp(200, {"destinations": [{"primary": {"structureType": "POINT"}}]})
        return _Resp(404, None)


def _resolver(fake, *, key="test-key", cache=None):
    service = GooglePlacesGeocodingService(api_key=key, session=fake)
    return LocationResolver(
        geocoding_service=service,
        campus_resolver=CampusResolver(service),
        building_resolver=BuildingResolver(service, retrieve_fn=lambda *a, **k: []),
        cache=cache if cache is not None else LocationCache(path=None),
    )


# ---------------------------------------------------------------------
# 1-2. valid location (and wording variant via the same intent fields)
# ---------------------------------------------------------------------


def test_valid_location_resolves_via_google_with_place_id_and_polygon():
    fake = _FakeGoogle()
    result = _resolver(fake).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.RESOLVED
    loc = result.location
    assert loc.source == LocationSource.GOOGLE_PLACES
    assert loc.place_id == "bld1"
    assert loc.geometry_type == GeometryType.POLYGON
    assert (loc.latitude, loc.longitude) == (12.8412, 80.1531)
    assert loc.geometry[0] == (12.8411, 80.1530)  # GeoJSON [lng, lat] flipped to (lat, lon)
    assert loc.confidence.value == "HIGH"
    text_search = [c for c in fake.calls if c[0].endswith("places:searchText")]
    assert all(c[2]["X-Goog-Api-Key"] == "test-key" and "places.id" in c[2]["X-Goog-FieldMask"] for c in text_search)
    assert any("locationRestriction" in c[1] for c in text_search)


def test_second_request_with_different_wording_is_served_from_cache():
    cache = LocationCache(path=None)
    fake = _FakeGoogle()
    _resolver(fake, cache=cache).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    calls_after_first = len(fake.calls)
    again = _resolver(fake, cache=cache).resolve(institution="vit chennai ", campus="VIT Chennai", building="AB1")
    assert again.status == LocationResolutionStatus.RESOLVED
    assert len(fake.calls) == calls_after_first  # no new Google request


# ---------------------------------------------------------------------
# 3-4. missing institution / unknown building
# ---------------------------------------------------------------------


def test_missing_institution_is_ambiguous():
    result = _resolver(_FakeGoogle()).resolve(building="AB1")
    assert result.status == LocationResolutionStatus.AMBIGUOUS


def test_unknown_building_not_resolved_even_though_google_returned_the_campus():
    result = _resolver(_FakeGoogle()).resolve(institution="VIT Chennai", campus="VIT Chennai", building="XYZ")
    assert result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED
    assert result.location is None  # never the campus centre


def test_campus_result_is_never_accepted_as_the_building():
    fake = _FakeGoogle(building_places=[])  # Google only ever returns the campus
    result = _resolver(fake).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED


# ---------------------------------------------------------------------
# 5-6. API unavailable / invalid or missing key
# ---------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["connection", "unavailable"])
def test_google_unavailable_stops_safely_without_cache(failure):
    result = _resolver(_FakeGoogle(fail=failure)).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED
    assert "Google Places was unavailable" in result.message


def test_google_unavailable_uses_verified_cache():
    cache = LocationCache(path=None)
    _resolver(_FakeGoogle(), cache=cache).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    result = _resolver(_FakeGoogle(fail="connection"), cache=cache).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.RESOLVED and result.location.place_id == "bld1"


def test_invalid_api_key_is_an_auth_error_and_stops_safely():
    service = GooglePlacesGeocodingService(api_key="bad", session=_FakeGoogle(fail="bad_key"))
    with pytest.raises(GeocodingAuthError):
        service.search("VIT Chennai")
    result = _resolver(_FakeGoogle(fail="bad_key"), key="bad").resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED


def test_missing_api_key_never_calls_google():
    fake = _FakeGoogle()
    with pytest.raises(GeocodingAuthError):
        GooglePlacesGeocodingService(api_key="", session=fake).search("VIT Chennai")
    assert fake.calls == []


def test_provider_selection(monkeypatch):
    import app.location.providers as providers

    monkeypatch.setattr(providers.settings, "location_provider", "auto")
    monkeypatch.setattr(providers.settings, "google_maps_api_key", "")
    assert type(get_geocoding_service()).__name__ == "NominatimGeocodingService"
    monkeypatch.setattr(providers.settings, "google_maps_api_key", "k")
    assert type(get_geocoding_service()).__name__ == "GooglePlacesGeocodingService"
    assert type(get_geocoding_service("openstreetmap")).__name__ == "NominatimGeocodingService"


# ---------------------------------------------------------------------
# 7-8. invalid coordinates / multiple candidates
# ---------------------------------------------------------------------


def test_invalid_coordinates_are_dropped():
    bad = dict(AB1, location={"latitude": 123.0, "longitude": 80.15})
    fake = _FakeGoogle(building_places=[bad])
    result = _resolver(fake).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED


def test_two_different_places_named_ab1_is_ambiguous():
    other = dict(AB1, id="bld9", location={"latitude": 12.8380, "longitude": 80.1560})  # ~500 m away, same name
    result = _resolver(_FakeGoogle(building_places=[AB1, other])).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.AMBIGUOUS
    assert "more than one place" in result.message


def test_same_place_listed_twice_is_not_ambiguous():
    dup = dict(AB1, id="bld1")
    result = _resolver(_FakeGoogle(building_places=[AB1, dup])).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.RESOLVED


def test_building_outside_campus_is_rejected():
    outside = dict(AB1, location={"latitude": 12.90, "longitude": 80.20})
    result = _resolver(_FakeGoogle(building_places=[outside])).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED


# ---------------------------------------------------------------------
# 9-10. point-only vs real polygon / multipolygon
# ---------------------------------------------------------------------


def test_point_only_result_stays_a_point_no_invented_polygon():
    result = _resolver(_FakeGoogle(outline=None)).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    loc = result.location
    assert loc.geometry_type == GeometryType.POINT
    assert loc.geometry is None and loc.geometry_available is False
    assert loc.footprint_rings == []


def test_multipolygon_outline_keeps_every_part():
    multi = {"type": "MultiPolygon", "coordinates": [
        [[[80.1530, 12.8411], [80.1532, 12.8411], [80.1532, 12.8413], [80.1530, 12.8411]]],
        [[[80.1535, 12.8415], [80.1536, 12.8415], [80.1536, 12.8416], [80.1535, 12.8415]]],
    ]}
    result = _resolver(_FakeGoogle(outline=multi)).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    loc = result.location
    assert loc.geometry_type == GeometryType.MULTIPOLYGON
    assert len(loc.footprint_rings) == 2


def test_geojson_parser_rejects_non_polygons():
    assert _rings_from_geojson({"type": "Point", "coordinates": [80.1, 12.8]}) == []
    assert _rings_from_geojson(None) == []


# ---------------------------------------------------------------------
# 11-15. map view model + server
# ---------------------------------------------------------------------

from app.location.inspection_geometry import compute_inspection_ring  # noqa: E402
from app.mission.location_aware_generator import NaturalLanguageMissionResult, NaturalLanguageMissionStatus  # noqa: E402
from app.mission.generator import MissionGenerationResult  # noqa: E402
from app.mission.intent_parser import MissionIntent, MissionType  # noqa: E402
from app.mission.schema import Mission, MissionCommand, MissionItem  # noqa: E402
from app.mission.state import MissionStateManager  # noqa: E402
from app.mission.validator import validate_mission  # noqa: E402
from app.ui.mission_view import MissionViewStore  # noqa: E402

HOME = (12.8400, 80.1525)


def _planned_result():
    location = _resolver(_FakeGoogle()).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    ring = compute_inspection_ring(location.location, standoff_m=15, n_points=4)
    items = [MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30)]
    for i, (lat, lon) in enumerate(ring.points, start=2):
        items.append(MissionItem(sequence=i, command=MissionCommand.WAYPOINT, latitude=lat, longitude=lon, altitude=30))
    items.append(MissionItem(sequence=len(items) + 1, command=MissionCommand.RETURN_TO_LAUNCH))
    mission = Mission(mission_name="Inspect AB1", mission_items=items)
    intent = MissionIntent(mission_type=MissionType.INSPECTION, location_mentioned=True, institution="VIT Chennai",
                           campus="VIT Chennai", building="AB1", altitude_m=30)
    return NaturalLanguageMissionResult(
        status=NaturalLanguageMissionStatus.GENERATED, intent=intent, location_result=location,
        mission_result=MissionGenerationResult(mission=mission, validation=validate_mission(mission), attempts=1),
        inspection_ring=ring, message="ok",
    )


def test_view_model_target_region_waypoints_and_route_match_the_mission():
    result = _planned_result()
    store = MissionViewStore()
    store.publish_plan(result, home=HOME)
    v = store.snapshot()
    assert v["has_plan"] and v["accepted"]
    assert v["target"]["latitude"] == 12.8412 and v["target"]["geometry_type"] == "POLYGON"
    assert len(v["target"]["footprint"]) == 1
    assert v["inspection_region"]["radius_m"] == pytest.approx(result.inspection_ring.radius_m)
    mission_coords = [(i.latitude, i.longitude) for i in result.mission_result.mission.mission_items if i.latitude is not None]
    assert [(w["latitude"], w["longitude"]) for w in v["waypoints"]] == mission_coords  # exactly the mission, nothing added
    assert v["route"][0] == list(HOME) and v["route"][-1] == list(HOME)  # takeoff from home, RTL back
    assert [tuple(p) for p in v["route"][1:-1]] == mission_coords


def test_stopped_request_draws_no_target():
    store = MissionViewStore()
    intent = MissionIntent(mission_type=MissionType.INSPECTION, location_mentioned=True, building="XYZ")
    from app.location.models import LocationResolutionResult

    store.publish_plan(NaturalLanguageMissionResult(
        status=NaturalLanguageMissionStatus.LOCATION_NOT_RESOLVED, intent=intent,
        location_result=LocationResolutionResult(status=LocationResolutionStatus.LOCATION_NOT_RESOLVED, message="nope"),
        mission_result=None, message="Could not reliably identify XYZ"))
    v = store.snapshot()
    assert v["target"] is None and v["waypoints"] == [] and v["accepted"] is False


def test_ramr_update_changes_displayed_route_and_refits():
    result = _planned_result()
    mission = result.mission_result.mission
    manager = MissionStateManager()
    manager.start(mission)
    manager.mark_validated(); manager.mark_uploaded(); manager.mark_running()
    manager.record_progress(current=3, total=len(mission.mission_items))  # WP item #2 done, #3 current

    class _Drone:
        latitude_deg, longitude_deg, relative_altitude_m = 12.8413, 80.1533, 30.0
        heading_deg, battery_remaining_percent, flight_mode = 90.0, 0.8, "MISSION"

    store = MissionViewStore()
    store.publish_plan(result, home=HOME)
    store.attach(manager, lambda: _Drone())
    before = store.snapshot()
    assert before["fit_key"].endswith(":1") and before["drone"]["latitude"] == 12.8413
    assert any(w["state"] == "completed" for w in before["waypoints"])

    # RAMR (Phase 14) inserts a corrective LOITER revisit at the blurred waypoint and applies it.
    blurred = mission.mission_items[1]
    remaining = [i for i in mission.mission_items if i.sequence >= 3]
    new_items = [MissionItem(sequence=1, command=MissionCommand.LOITER, latitude=blurred.latitude,
                             longitude=blurred.longitude, altitude=30, hold_time_s=5)]
    for n, item in enumerate(remaining, start=2):
        new_items.append(item.model_copy(update={"sequence": n}))
    manager.start_replanning()
    assert store.snapshot()["replanning_active"] is True
    manager.begin_update()
    manager.apply_replan(Mission(mission_name="Inspect AB1 (revisit)", mission_items=new_items))

    after = store.snapshot()
    assert after["fit_key"].endswith(":2") and after["mission_version"] == 2 and after["adaptation_count"] == 1
    corrective = [w for w in after["waypoints"] if w["corrective"]]
    assert len(corrective) == 1 and corrective[0]["command"] == "LOITER"
    assert after["route"] != before["route"]
    assert after["completed_history"], "completed waypoints stay visible after the replan"


def test_map_server_serves_page_view_and_browser_config_only(monkeypatch):
    from app.ui import map_server

    monkeypatch.setattr(map_server.settings, "google_maps_api_key", "SERVER-SECRET")
    monkeypatch.setattr(map_server.settings, "google_maps_browser_api_key", "BROWSER-KEY")
    store = MissionViewStore()
    store.publish_plan(_planned_result(), home=HOME)
    url = map_server.start_map_server(store, host="127.0.0.1", port=0)
    try:
        page = urllib.request.urlopen(url).read().decode()
        assert "/static/mission_map.js" in page                      # drawing lives in the shared script
        js = urllib.request.urlopen(url + "static/mission_map.js").read().decode()
        assert "importLibrary" in js and "AdvancedMarkerElement" in js
        view = json.loads(urllib.request.urlopen(url + "api/mission-view").read())
        assert view["has_plan"] and len(view["waypoints"]) == 4
        cfg_raw = urllib.request.urlopen(url + "api/maps-config").read().decode()
        assert "BROWSER-KEY" in cfg_raw and "SERVER-SECRET" not in cfg_raw
    finally:
        map_server.stop_map_server(host="127.0.0.1", port=0)
