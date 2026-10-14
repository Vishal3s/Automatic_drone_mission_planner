"""
Web UI browser test -- the real mission_control.html, served by the real
Starlette app (uvicorn, in a thread), driven in headless Chromium.
Run with: pytest tests/test_ui_mission_control_page.py -v
(needs starlette + uvicorn + httpx + playwright/Chromium; skipped otherwise)

Fakes: the planner, the drone (FakeLink) and Google's Maps JS library
(the same recording stand-in as test_phase15_map_page.py).
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")
playwright_sync = pytest.importorskip("playwright.sync_api")

from app.api.main import create_app  # noqa: E402
from app.api.service import MissionControlService  # noqa: E402
from tests.test_phase15_map_page import FAKE_MAPS_JS  # noqa: E402
from tests.test_ui_mission_control import PLANNED, FakeLink  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def ui(monkeypatch):
    import app.api.main as main

    monkeypatch.setattr(main.settings, "google_maps_browser_api_key", "BROWSER-KEY")
    link = FakeLink(fly_delay=0.15)
    svc = MissionControlService(planner=lambda text, **kw: (time.sleep(0.3), PLANNED)[1], resolver=object(),
                                link_factory=lambda address=None: link)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    with playwright_sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {exc}")
        page = browser.new_page()
        page.route("https://maps.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/javascript", body=FAKE_MAPS_JS))
        yield page, f"http://127.0.0.1:{port}/", svc, link
        browser.close()
    server.should_exit = True
    thread.join(timeout=5)


def _calls(page, kind):
    return [c for c in page.evaluate("window.__calls || []") if c["k"] == kind]


def test_request_plan_connect_confirm_fly(ui):
    page, url, svc, link = ui
    page.goto(url)
    assert "Drone not connected" in page.inner_text("#droneLabel")
    assert page.is_disabled("#sendBtn")                                   # nothing to send yet

    # 1. type a request and plan it
    page.click("#examples .chip >> nth=0")
    assert "AB1" in page.input_value("#request")
    page.click("#planBtn")
    page.wait_for_selector("#planBadge .badge.ok", timeout=8000)
    assert "Accepted" in page.inner_text("#planBadge")
    assert page.locator("#planBody table tbody tr").count() == len(PLANNED.mission_result.mission.mission_items)
    page.wait_for_function("(window.__calls || []).some(c => c.k === 'fitBounds')", timeout=5000)   # map jumped to the target
    assert any(c["color"] == "#1A73E8" for c in _calls(page, "polyline"))                           # route drawn
    assert "Inspect AB1" in page.inner_text("#history")

    # 2. connect to the drone
    page.click("#connectBtn")
    page.wait_for_function("document.querySelector('#droneLabel').textContent.startsWith('Connected')", timeout=5000)
    assert "85%" in page.inner_text("#telemetry")
    assert page.is_enabled("#sendBtn") and page.is_enabled("#rtlBtn")

    # 3. send -> confirmation dialog -> cancel does nothing
    page.click("#sendBtn")
    assert page.is_visible("#confirm") and "Waypoints" in page.inner_text("#confirmBody")
    page.click("#cancelSend")
    time.sleep(0.3)
    assert svc.flight["state"] == "idle"

    # 4. send for real
    page.click("#sendBtn")
    page.click("#doSend")
    page.wait_for_function("document.querySelector('#flightBadge').textContent.includes('Completed')", timeout=10000)
    assert "Done" in page.inner_text("#steps")
    assert page.is_enabled("#sendBtn")                                    # can fly the plan again once landed

    # 5. operator commands reach the drone
    page.click("#rtlBtn")
    page.wait_for_function("document.querySelector('#toast').textContent.includes('Return to Launch')", timeout=5000)
    assert link.commands == ["rtl"]
