"""
Phase 15 -- browser tests for app/ui/static/mission_map.html.
Run with: pytest tests/test_phase15_map_page.py -v      (needs `playwright` + Chromium; skipped otherwise)

The real page is served by the real map server and opened in headless
Chromium. The only stand-in is the Google Maps JavaScript library itself:
requests to maps.googleapis.com are answered with a small RECORDING
implementation of the classes the page uses (Map, Polygon, Polyline,
Circle, AdvancedMarkerElement, PinElement, LatLngBounds), so the tests
can assert what the page asked Google Maps to draw and where it moved
the camera. It cannot prove Google's own rendering -- only that this page
issues the right calls (see the README limitations).
"""

from __future__ import annotations

import time

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from app.ui import map_server  # noqa: E402
from app.ui.mission_view import MissionViewStore  # noqa: E402
from tests.test_phase15_google_maps import HOME, _planned_result  # noqa: E402

FAKE_MAPS_JS = r"""
(() => {
  const calls = window.__calls = [];
  class LatLngBounds { constructor(){ this.pts=[]; } extend(p){ this.pts.push(p); } }
  class MVC { constructor(o){ Object.assign(this, o||{}); } setMap(m){ this.map=m; } addListener(e, fn){ this._click = fn; } }
  class Map { constructor(el, o){ this.o=o; calls.push({k:"map", mapId:o.mapId}); }
    addListener(e, fn){ if (e === "click") window.__mapClick = (lat, lng) => fn({ latLng: { lat: () => lat, lng: () => lng } }); }
    fitBounds(b, pad){ calls.push({k:"fitBounds", n:b.pts.length}); }
    setCenter(c){ calls.push({k:"setCenter", c}); } setZoom(z){ calls.push({k:"setZoom", z}); } }
  class Polygon extends MVC { constructor(o){ super(o); calls.push({k:"polygon", color:o.strokeColor, n:o.paths.length}); } }
  class Polyline extends MVC { constructor(o){ super(o); calls.push({k:"polyline", color:o.strokeColor, n:o.path.length}); } }
  class Circle extends MVC { constructor(o){ super(o); calls.push({k:"circle", color:o.strokeColor, r:o.radius}); }
    getBounds(){ const c=this.center; return {getNorthEast:()=>c, getSouthWest:()=>c}; } }
  class PinElement { constructor(o){ this.o=o; this.element=document.createElement("div"); this.element.dataset.bg=o.background; this.element.dataset.glyph=o.glyph; } }
  class AdvancedMarkerElement { constructor(o){ Object.assign(this,o); calls.push({k:"marker", bg:o.content?.dataset?.bg||"custom", glyph:o.content?.dataset?.glyph||null, title:o.title}); } }
  window.google.maps.SymbolPath = { FORWARD_OPEN_ARROW: 1 };
  const libs = { maps:{Map,Polygon,Polyline,Circle}, marker:{AdvancedMarkerElement,PinElement}, core:{LatLngBounds} };
  window.google.maps.importLibrary = async (name) => libs[name];
  window.google.maps.__ib__();
})();
"""


@pytest.fixture()
def browser_page(monkeypatch):
    monkeypatch.setattr(map_server.settings, "google_maps_browser_api_key", "BROWSER-KEY")
    store = MissionViewStore()
    url = map_server.start_map_server(store, host="127.0.0.1", port=0)
    with playwright_sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {exc}")
        page = browser.new_page()
        page.route("https://maps.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/javascript", body=FAKE_MAPS_JS))
        yield store, url, page
        browser.close()
    map_server.stop_map_server(host="127.0.0.1", port=0)


def _calls(page, kind=None):
    calls = page.evaluate("window.__calls || []")
    return [c for c in calls if kind is None or c["k"] == kind]


def _wait_for(page, predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        page.wait_for_timeout(150)
    return False


def test_map_auto_fits_and_draws_target_region_waypoints_and_route(browser_page):
    store, url, page = browser_page
    result = _planned_result()
    store.publish_plan(result, home=HOME)
    page.goto(url)
    assert _wait_for(page, lambda: _calls(page, "fitBounds")), "map never fitted to the mission"

    assert _calls(page, "map")[0]["mapId"] == "DEMO_MAP_ID"
    polygons = _calls(page, "polygon")
    assert polygons and polygons[0]["color"] == "#D93025"                       # RED footprint
    # RED inspection region -- boundary fix: drawn as a polygon that follows the outline, not a circle
    assert len(polygons) >= 2 and polygons[1]["color"] == "#D93025"
    assert not _calls(page, "circle")
    routes = [c for c in _calls(page, "polyline") if c["color"] == "#1A73E8"]   # BLUE planned path
    assert routes and routes[0]["n"] == 4 + 2                                   # home + 4 waypoints + home
    blue_pins = [m for m in _calls(page, "marker") if m["bg"] == "#1A73E8"]
    assert sorted(int(m["glyph"]) for m in blue_pins) == [2, 3, 4, 5]           # exactly the mission's waypoints
    assert "Mission accepted" in page.inner_text("#status")
    assert "AB1" in page.inner_text("#target")


def test_ramr_update_redraws_without_reload_and_refits(browser_page):
    from app.mission.schema import Mission, MissionCommand, MissionItem
    from app.mission.state import MissionStateManager

    store, url, page = browser_page
    result = _planned_result()
    mission = result.mission_result.mission
    manager = MissionStateManager()
    manager.start(mission); manager.mark_validated(); manager.mark_uploaded(); manager.mark_running()
    store.publish_plan(result, home=HOME)
    store.attach(manager)
    page.goto(url)
    assert _wait_for(page, lambda: len(_calls(page, "fitBounds")) == 1)

    blurred = mission.mission_items[1]
    items = [MissionItem(sequence=1, command=MissionCommand.LOITER, latitude=blurred.latitude, longitude=blurred.longitude, altitude=30, hold_time_s=5)]
    items += [i.model_copy(update={"sequence": n}) for n, i in enumerate((i for i in mission.mission_items if i.sequence >= 3), start=2)]
    manager.start_replanning(); manager.begin_update()
    manager.apply_replan(Mission(mission_name="revisit", mission_items=items))

    assert _wait_for(page, lambda: len(_calls(page, "fitBounds")) >= 2), "map did not refit after the RAMR update"
    assert any(m["bg"] == "#F9AB00" for m in _calls(page, "marker")), "corrective waypoint not shown in yellow"
    assert "1 RAMR update" in page.inner_text("#live")


def test_point_only_target_is_centered_and_zoomed(browser_page):
    from app.mission.intent_parser import MissionIntent, MissionType
    from app.mission.location_aware_generator import NaturalLanguageMissionResult, NaturalLanguageMissionStatus

    store, url, page = browser_page
    location = _planned_result().location_result
    location.location.geometry, location.location.geometry_available = None, False  # a point-only resolution
    store.publish_plan(NaturalLanguageMissionResult(
        status=NaturalLanguageMissionStatus.GENERATED,
        intent=MissionIntent(mission_type=MissionType.INSPECTION, location_mentioned=True, campus="VIT Chennai"),
        location_result=location, mission_result=None, message="campus only"))
    page.goto(url)
    assert _wait_for(page, lambda: _calls(page, "setZoom"))
    assert _calls(page, "setZoom")[0]["z"] == 18
    assert not _calls(page, "polygon"), "a point must never be drawn as a polygon"
    assert any(m["glyph"] == "T" and m["bg"] == "#D93025" for m in _calls(page, "marker"))


def test_missing_browser_key_shows_banner_but_keeps_panel(browser_page, monkeypatch):
    store, url, page = browser_page
    monkeypatch.setattr(map_server.settings, "google_maps_browser_api_key", "")
    store.publish_plan(_planned_result(), home=HOME)
    page.goto(url)
    assert _wait_for(page, lambda: "GOOGLE_MAPS_BROWSER_API_KEY" in page.inner_text("#banner"))
    assert _wait_for(page, lambda: "Mission accepted" in page.inner_text("#status"))


def test_pick_mode_click_marks_building_and_plans(browser_page):
    """Building unknown to every map: page shows the campus + outlines; one click -> handler -> plan drawn."""
    from tests.test_phase15_google_maps import CAMPUS_BBOX

    store, url, page = browser_page
    planned = _planned_result()
    store.publish_pick_request(building="AB1", campus="VIT Chennai", campus_bbox=CAMPUS_BBOX,
                               campus_center=(12.8406, 80.1534), message="AB1 isn't named on any map",
                               footprints=[[[12.8411, 80.1530], [12.8411, 80.1532], [12.8413, 80.1532]]])
    clicks = []

    def handler(lat, lon):
        clicks.append((lat, lon))
        store.publish_plan(planned, home=HOME)
        return True, "AB1 saved."

    store.set_pick_handler(handler)
    page.goto(url)
    assert _wait_for(page, lambda: "Click AB1 on the map" in page.inner_text("#status"))
    assert _wait_for(page, lambda: _calls(page, "fitBounds")), "map did not fit the campus"
    assert len(_calls(page, "polygon")) >= 2  # campus box + OSM outline

    page.evaluate("window.__mapClick(12.8412, 80.1531)")
    assert _wait_for(page, lambda: "Mission accepted" in page.inner_text("#status")), "plan not shown after the click"
    assert clicks == [(12.8412, 80.1531)]
    assert any(c["color"] == "#D93025" for c in _calls(page, "polygon"))  # AB1 now marked in red
