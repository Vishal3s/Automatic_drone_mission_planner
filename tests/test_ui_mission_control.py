"""
Web UI tests -- app/api/service.py (logic + safety rules) and app/api/main.py (routes).
Run with: pytest tests/test_ui_mission_control.py -v      (needs `starlette` + `httpx`; skipped otherwise)

The planner and the drone are fakes; the mission objects are real, built by
the same helper the Phase 15 tests use. Coordinates are FAKE TEST FIXTURES.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from app.api.main import create_app  # noqa: E402
from app.api.service import MissionControlService  # noqa: E402
from app.mavlink.drone_state import DroneStateManager  # noqa: E402
from app.mavlink.px4_client import MissionExecutionResult  # noqa: E402
from app.mission.location_aware_generator import NaturalLanguageMissionStatus  # noqa: E402
from tests.test_phase15_google_maps import _planned_result  # noqa: E402

PLANNED = _planned_result()
FIRST_WP = next((i.latitude, i.longitude) for i in PLANNED.mission_result.mission.mission_items if i.latitude is not None)


class FakeLink:
    """Stands in for DroneLink: same methods, no MAVSDK."""

    def __init__(self, address="udpin://0.0.0.0:14540", *, home=FIRST_WP, fail_stage=None, battery=85.0, fly_delay=0.0):
        self.address, self.home_pos, self.fail_stage, self.fly_delay = address, home, fail_stage, fly_delay
        self.connected, self.last_error = False, None
        self.state = DroneStateManager()
        self.state.update_position(latitude_deg=home[0], longitude_deg=home[1], absolute_altitude_m=10.0, relative_altitude_m=0.0)
        self.state.update_battery(voltage_v=16.0, remaining_percent=battery)
        self.commands = []

    async def connect(self, timeout_s=None):
        self.connected = True
        return True

    async def home(self):
        return self.home_pos

    async def fly(self, mission, *, on_progress=None, on_stage=None, preupload_check=None, mission_timeout_s=900.0):
        on_stage("preflight")
        problem = preupload_check(*self.home_pos) if preupload_check else None
        if problem:
            return MissionExecutionResult(accepted=False, stage="preflight", error=problem)
        for stage in ("upload", "arm", "start"):
            on_stage(stage)
            if stage == self.fail_stage:
                return MissionExecutionResult(accepted=False, stage=stage, error=f"{stage} refused")
        on_stage("monitor")
        total = len(mission.mission_items)
        for current in range(1, total + 1):
            await asyncio.sleep(self.fly_delay)
            on_progress(current, total)
        return MissionExecutionResult(accepted=True, stage="complete", error=None)

    async def return_to_launch(self):
        self.commands.append("rtl")

    async def hold(self):
        self.commands.append("hold")

    async def close(self):
        self.connected = False


def _service(result=PLANNED, link=None):
    links = []

    def factory(address=None):
        made = link or FakeLink(address or "udpin://0.0.0.0:14540")
        links.append(made)
        return made

    svc = MissionControlService(planner=lambda text, **kw: result, resolver=object(), link_factory=factory)
    return svc, links


def _rejected():
    from app.mission.location_aware_generator import NaturalLanguageMissionResult
    from app.location.models import LocationResolutionResult, LocationResolutionStatus

    return NaturalLanguageMissionResult(
        status=NaturalLanguageMissionStatus.LOCATION_NOT_RESOLVED, intent=PLANNED.intent, mission_result=None,
        location_result=LocationResolutionResult(status=LocationResolutionStatus.LOCATION_NOT_RESOLVED, message="nope"),
        message="nope")


async def _wait_flight(svc, states=("completed", "failed")):
    for _ in range(200):
        if svc.flight["state"] in states:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"flight stuck in {svc.flight}")


# ---------------------------------------------------------------------
# Service: planning
# ---------------------------------------------------------------------

def test_plan_records_history_and_current_plan():
    svc, _ = _service()
    out = svc.plan("Inspect AB1 of VIT Chennai at 30 meters")
    assert out["accepted"] and out["current"]
    state = svc.state()
    assert state["current_plan"]["plan_id"] == out["plan_id"]
    assert state["plans"][0]["request"].startswith("Inspect AB1")
    assert state["view"]["has_plan"] and len(state["view"]["waypoints"]) == 4


def test_empty_request_is_rejected():
    svc, _ = _service()
    with pytest.raises(ValueError):
        svc.plan("   ")


# ---------------------------------------------------------------------
# Service: sending -- every safety rule
# ---------------------------------------------------------------------

def test_send_requires_confirmation_and_accepted_current_plan():
    async def run():
        svc, _ = _service()
        first = svc.plan("Inspect AB1")["plan_id"]
        with pytest.raises(PermissionError):
            await svc.send_to_drone(first, confirm=False)
        svc._planner = lambda text, **kw: _rejected()
        second = svc.plan("Inspect XYZ")["plan_id"]
        with pytest.raises(RuntimeError, match="not accepted"):
            await svc.send_to_drone(second, confirm=True)
        with pytest.raises(RuntimeError, match="most recent"):
            await svc.send_to_drone(first, confirm=True)
        with pytest.raises(KeyError):
            await svc.send_to_drone(999, confirm=True)

    asyncio.run(run())


def test_send_flies_the_exact_mission_and_tracks_progress():
    async def run():
        svc, links = _service()
        plan_id = svc.plan("Inspect AB1")["plan_id"]
        status = await svc.send_to_drone(plan_id, confirm=True)   # auto-connects
        assert status["connected"] and svc.flight["state"] == "starting"
        await _wait_flight(svc)
        assert svc.flight["state"] == "completed" and svc.flight["progress"][0] == svc.flight["progress"][1]
        view = svc.view()
        assert view["mission_status"] == "COMPLETED"
        assert view["drone"]["latitude"] == FIRST_WP[0]                     # live drone on the map

    asyncio.run(run())


def test_second_send_while_flying_is_refused():
    async def run():
        svc, _ = _service(link=FakeLink(fly_delay=0.05))
        plan_id = svc.plan("Inspect AB1")["plan_id"]
        await svc.send_to_drone(plan_id, confirm=True)
        with pytest.raises(RuntimeError, match="already in flight"):
            await svc.send_to_drone(plan_id, confirm=True)
        with pytest.raises(RuntimeError):
            svc.plan("Inspect AB2")                                          # no re-planning mid-flight
        with pytest.raises(RuntimeError):
            await svc.disconnect_drone()
        await _wait_flight(svc)

    asyncio.run(run())


def test_mission_far_from_drone_home_is_refused_before_upload():
    async def run():
        svc, _ = _service(link=FakeLink(home=(FIRST_WP[0] + 0.5, FIRST_WP[1])))   # ~55 km away
        plan_id = svc.plan("Inspect AB1")["plan_id"]
        await svc.send_to_drone(plan_id, confirm=True)
        await _wait_flight(svc)
        assert svc.flight["state"] == "failed" and svc.flight["stage"] == "preflight"
        assert "PX4_HOME_LAT" in svc.flight["message"]

    asyncio.run(run())


def test_px4_refusal_is_reported_with_its_stage():
    async def run():
        svc, _ = _service(link=FakeLink(fail_stage="arm"))
        await svc.send_to_drone(svc.plan("Inspect AB1")["plan_id"], confirm=True)
        await _wait_flight(svc)
        assert svc.flight["state"] == "failed" and "arm" in svc.flight["message"]

    asyncio.run(run())


def test_live_context_converts_mavsdk_battery_percent_to_fraction():
    async def run():
        svc, _ = _service()
        assert await svc.live_context() == (None, None)
        await svc.connect_drone()
        home, battery = await svc.live_context()
        assert home == FIRST_WP and battery == pytest.approx(0.85)

    asyncio.run(run())


def test_connect_with_a_different_address_replaces_the_link():
    async def run():
        svc, links = _service()
        await svc.connect_drone()
        await svc.connect_drone("serial://COM5:57600")
        assert links[-1].address == "serial://COM5:57600" and not links[0].connected

    asyncio.run(run())


def test_rtl_and_hold_reach_the_drone():
    async def run():
        svc, links = _service()
        with pytest.raises(ConnectionError):
            await svc.return_to_launch()
        await svc.connect_drone()
        await svc.return_to_launch()
        await svc.hold()
        assert links[0].commands == ["rtl", "hold"]

    asyncio.run(run())


# ---------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------

def _client(monkeypatch, svc=None):
    import app.api.main as main

    monkeypatch.setattr(main.settings, "google_maps_api_key", "SERVER-SECRET")
    monkeypatch.setattr(main.settings, "google_maps_browser_api_key", "BROWSER-KEY")
    svc = svc or _service()[0]
    return TestClient(create_app(svc)), svc


def test_page_static_and_config_never_leak_server_key(monkeypatch):
    client, _ = _client(monkeypatch)
    page = client.get("/")
    assert page.status_code == 200 and "Drone Mission Control" in page.text and "/static/mission_map.js" in page.text
    assert "MissionMap" in client.get("/static/mission_map.js").text
    assert client.get("/static/../config.py").status_code == 404
    assert client.get("/static/secrets.env").status_code == 404
    cfg = client.get("/api/config")
    assert cfg.json()["browser_api_key"] == "BROWSER-KEY" and "SERVER-SECRET" not in cfg.text


def test_plan_route_validates_input(monkeypatch):
    client, _ = _client(monkeypatch)
    assert client.post("/api/plan", json={"request": ""}).status_code == 400
    assert client.post("/api/plan", json={"request": "x", "home_latitude": 12.8}).status_code == 400
    assert client.post("/api/plan", json={"request": "x", "home_latitude": 999, "home_longitude": 1}).status_code == 400
    ok = client.post("/api/plan", json={"request": "Inspect AB1 of VIT Chennai"})
    assert ok.status_code == 200 and ok.json()["accepted"]
    state = client.get("/api/state").json()
    assert state["current_plan"]["accepted"] and state["planning"]["active"] is False


def test_send_route_requires_confirm_true(monkeypatch):
    client, _ = _client(monkeypatch)
    plan_id = client.post("/api/plan", json={"request": "Inspect AB1"}).json()["plan_id"]
    assert client.post("/api/drone/send", json={"plan_id": plan_id}).status_code == 400
    assert client.post("/api/drone/send", json={"plan_id": plan_id, "confirm": "yes"}).status_code == 400
    assert client.post("/api/drone/send", json={"plan_id": 42, "confirm": True}).status_code == 404
    assert client.post("/api/drone/send", json={"plan_id": plan_id, "confirm": True}).status_code == 200


def test_planning_uses_the_connected_drones_home(monkeypatch):
    seen = {}

    def planner(text, **kw):
        seen.update(kw)
        return PLANNED

    svc = MissionControlService(planner=planner, resolver=object(), link_factory=lambda address=None: FakeLink())
    client, _ = _client(monkeypatch, svc)
    client.post("/api/drone/connect", json={})
    client.post("/api/plan", json={"request": "Inspect AB1"})
    assert seen["home_position"] == FIRST_WP and seen["battery_percent"] == pytest.approx(0.85)


def test_unexpected_crash_in_flight_task_shows_stopped_not_stuck():
    async def run():
        class Boom(FakeLink):
            async def fly(self, *a, **k):
                raise RuntimeError("mavsdk exploded")

        svc, _ = _service(link=Boom())
        await svc.send_to_drone(svc.plan("Inspect AB1")["plan_id"], confirm=True)
        await _wait_flight(svc)
        assert svc.flight["state"] == "failed" and "mavsdk exploded" in svc.flight["message"]

    asyncio.run(run())
