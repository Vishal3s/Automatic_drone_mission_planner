"""
Phase 15.4 tests -- "click the building once" when no map provider names it.
Run with: pytest tests/test_phase15_map_pick.py -v

Google / Overpass are faked; coordinates are FAKE TEST FIXTURES.
"""

from __future__ import annotations

import json
import urllib.request

from app.location.models import LocationResolutionStatus, LocationSource
from app.location.verified_buildings import VerifiedBuildingStore
from app.ui.mission_view import MissionViewStore
from tests.test_phase15_footprints import AB1_RING, AB2_RING, _FakeOverpass, _fp, _resolver
from tests.test_phase15_google_maps import CAMPUS_BBOX, _FakeGoogle


def _pick_resolver(overpass, store=None, google=None):
    google = google or _FakeGoogle(building_places=[])  # Google doesn't know AB1 -- like the real VIT Chennai run
    r = _resolver(google, overpass)
    r._verified = store if store is not None else VerifiedBuildingStore(path=None)
    return r, google


def test_click_inside_ab1_marks_its_real_outline_and_saves_it():
    r, _ = _pick_resolver(_FakeOverpass([_fp("way/1", AB1_RING), _fp("way/2", AB2_RING)]))
    picked = r.resolve_from_map_pick(institution="VIT Chennai", campus="VIT Chennai", building="AB1",
                                     latitude=12.8412, longitude=80.1531, campus_bbox=CAMPUS_BBOX)
    loc = picked.location
    assert picked.status == LocationResolutionStatus.RESOLVED
    assert loc.source == LocationSource.USER_VERIFIED and loc.confidence.value == "HIGH"
    assert loc.geometry == AB1_RING and loc.resolution_type.value == "BUILDING"
    assert "Selected on the map" in loc.geometry_note


def test_next_request_is_automatic_no_click_no_google():
    r, google = _pick_resolver(_FakeOverpass([_fp("way/1", AB1_RING)]))
    r.resolve_from_map_pick(institution="VIT Chennai", campus="VIT Chennai", building="AB1",
                            latitude=12.8412, longitude=80.1531, campus_bbox=CAMPUS_BBOX)
    calls_before = len(google.calls)
    again = r.resolve(institution="VIT Chennai", campus="VIT Chennai", building="Academic Block 1")  # other spelling
    assert again.status == LocationResolutionStatus.RESOLVED
    assert again.location.source == LocationSource.USER_VERIFIED and again.location.geometry == AB1_RING
    assert len(google.calls) == calls_before


def test_click_outside_campus_is_refused():
    r, _ = _pick_resolver(_FakeOverpass([_fp("way/1", AB1_RING)]))
    picked = r.resolve_from_map_pick(institution="VIT Chennai", campus="VIT Chennai", building="AB1",
                                     latitude=12.90, longitude=80.20, campus_bbox=CAMPUS_BBOX)
    assert picked.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED and "outside" in picked.message


def test_click_on_unmapped_spot_is_kept_as_a_point_not_an_invented_outline():
    r, _ = _pick_resolver(_FakeOverpass([_fp("way/2", AB2_RING)]))
    picked = r.resolve_from_map_pick(institution="VIT Chennai", campus="VIT Chennai", building="AB1",
                                     latitude=12.8380, longitude=80.1510, campus_bbox=CAMPUS_BBOX)
    loc = picked.location
    assert loc.geometry is None and loc.resolution_type.value == "POINT" and loc.confidence.value == "MEDIUM"


def test_verified_store_persists_to_disk(tmp_path):
    path = tmp_path / "verified.json"
    VerifiedBuildingStore(path=path).save({"campus": "VIT Chennai", "building": "AB1", "latitude": 1.0, "longitude": 2.0})
    found = VerifiedBuildingStore(path=path).find("vit chennai", "AB-1")
    assert found and found["latitude"] == 1.0
    assert VerifiedBuildingStore(path=path).find("Other Campus", "AB1") is None


def test_unresolved_request_publishes_pick_mode_and_click_endpoint_calls_handler():
    from app.ui import map_server

    store = MissionViewStore()
    store.publish_pick_request(building="AB1", campus="VIT Chennai", campus_bbox=CAMPUS_BBOX,
                               campus_center=(12.8406, 80.1534), message="not named", footprints=[[list(p) for p in AB1_RING]])
    clicks = []
    store.set_pick_handler(lambda lat, lon: (clicks.append((lat, lon)) or True, "saved"))
    url = map_server.start_map_server(store, host="127.0.0.1", port=0)
    try:
        view = json.loads(urllib.request.urlopen(url + "api/mission-view").read())
        assert view["has_plan"] is False and view["pick"]["building"] == "AB1" and len(view["pick"]["footprints"]) == 1
        req = urllib.request.Request(url + "api/pick", data=json.dumps({"latitude": 12.8412, "longitude": 80.1531}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        out = json.loads(urllib.request.urlopen(req).read())
        assert out == {"ok": True, "message": "saved"} and clicks == [(12.8412, 80.1531)]
        bad = urllib.request.Request(url + "api/pick", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(bad)
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        map_server.stop_map_server(host="127.0.0.1", port=0)


def test_plan_mission_script_switches_to_pick_mode_and_plans_after_click(monkeypatch):
    """scripts/plan_mission.py glue: NOT_RESOLVED with a known campus -> pick mode -> click -> plan published."""
    import runpy
    from pathlib import Path

    from app.location.models import LocationResolutionResult
    from app.mission.intent_parser import MissionIntent, MissionType
    from app.mission.location_aware_generator import NaturalLanguageMissionResult, NaturalLanguageMissionStatus
    from tests.test_phase15_google_maps import _planned_result

    script = runpy.run_path(str(Path(__file__).resolve().parent.parent / "scripts" / "plan_mission.py"), run_name="not_main")
    ask = script["_ask_for_map_pick"]
    import app.ui.pick as pick_module
    from app.location.footprint_sources import FootprintSources
    fake_sources = FootprintSources([("openstreetmap", _FakeOverpass([_fp("way/1", AB1_RING)]))])
    monkeypatch.setattr(pick_module, "build_footprint_sources", lambda: fake_sources)
    monkeypatch.setitem(ask.__globals__, "build_footprint_sources", lambda: fake_sources)

    intent = MissionIntent(mission_type=MissionType.INSPECTION, location_mentioned=True,
                           institution="VIT Chennai", campus="VIT Chennai", building="AB1", altitude_m=30)
    campus = {"latitude": 12.8406, "longitude": 80.1534, "bounding_box": CAMPUS_BBOX}
    unresolved = NaturalLanguageMissionResult(
        status=NaturalLanguageMissionStatus.LOCATION_NOT_RESOLVED, intent=intent, mission_result=None,
        location_result=LocationResolutionResult(status=LocationResolutionStatus.LOCATION_NOT_RESOLVED,
                                                 message="not named", details={"campus_resolved": campus}),
        message="not named")

    r, _ = _pick_resolver(_FakeOverpass([_fp("way/1", AB1_RING)]))
    store = MissionViewStore()
    assert ask(store, r, unresolved, "Inspect AB1 of VIT Chennai at 30 meters", lambda text: _planned_result(), None) is True
    view = store.snapshot()
    assert view["pick"]["building"] == "AB1" and len(view["pick"]["footprints"]) == 1

    out = store.handle_pick(12.8412, 80.1531)
    assert out["ok"] is True
    assert store.snapshot()["has_plan"] is True and store.snapshot()["accepted"] is True
    assert r.resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1").location.source == LocationSource.USER_VERIFIED
