"""
Download of the Mission Planner .waypoints file from the web app, and the
file-request detection. No network: the planner is stubbed with a building
outline built in local metres.
"""

import asyncio

import pytest

from app.api.main import create_app
from app.api.service import MissionControlService
from app.location.building_knowledge import BuildingFacts
from app.location.models import LocationResolutionResult, LocationResolutionStatus
from app.mission.intent_parser import ScanMode, MissionIntent, MissionType, wants_waypoints_file
import app.mission.location_aware_generator as generator
from tests.test_structural_scan import BOX, location


class _Resolver:
    def resolve(self, **kw):
        return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=location(BOX, name="Ripon Building"))


def _planner(request: str, *, location_resolver=None, home_position=None, battery_percent=None):
    intent = MissionIntent(mission_type=MissionType.INSPECTION, location_mentioned=True, institution="VIT",
                           campus="VIT Chennai", building="Ripon Building", altitude_m=40.0,
                           scan_mode=ScanMode.STRUCTURAL)
    original = generator.parse_mission_intent
    generator.parse_mission_intent = lambda req: intent
    try:
        return generator.generate_mission_from_natural_language(
            request, location_resolver=_Resolver(), home_position=home_position, battery_percent=battery_percent,
            building_facts_fn=lambda b, c: BuildingFacts(building=b, height_m=30.0))
    finally:
        generator.parse_mission_intent = original


def _service():
    return MissionControlService(resolver=_Resolver(), planner=_planner)


def _get(app, path):
    scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [], "root_path": ""}
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], headers, body


def test_file_request_is_detected():
    assert wants_waypoints_file("Structural scan of Ripon Building and generate the waypoints file")
    assert wants_waypoints_file("save the mission file")
    assert not wants_waypoints_file("Structural scan of Ripon Building at 40 meters")


def test_accepted_plan_downloads_as_mission_planner_file():
    svc = _service()
    out = svc.plan("Structural scan of Ripon Building, VIT Chennai at 40 meters", home=None, battery=None)
    assert out["accepted"] and out["waypoints_download"] == f"/api/plans/{out['plan_id']}/waypoints"
    app = create_app(svc)
    status, headers, body = _get(app, out["waypoints_download"])
    assert status == 200
    assert headers["content-disposition"].startswith("attachment; filename=")
    assert headers["content-disposition"].endswith('.waypoints"')
    assert body.decode().splitlines()[0] == "QGC WPL 110"


def test_unknown_plan_is_404():
    app = create_app(_service())
    status, _, _ = _get(app, "/api/plans/999/waypoints")
    assert status == 404


def test_plan_not_accepted_cannot_be_downloaded():
    from types import SimpleNamespace

    svc = _service()
    svc._results[7] = SimpleNamespace(accepted=False)  # a stored plan that was refused
    with pytest.raises(PermissionError):
        svc.waypoints_download(7)
