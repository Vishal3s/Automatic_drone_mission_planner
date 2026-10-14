"""
Microsoft GlobalMLBuildingFootprints integration -- no network: the index
CSV and the gzipped GeoJSON-lines tile are served by a fake session in the
dataset's published format. Coordinates are FAKE TEST FIXTURES.
"""

from __future__ import annotations

import gzip
import json
import sys
import types

import pytest

from app.location.footprint_sources import FootprintSources
from app.location.geocoding_service import GeocodingConnectionError
from app.location.ms_footprints import (
    MicrosoftFootprintService,
    parse_feature,
    quadkey,
    quadkeys_for_bbox,
    tile_to_quadkey,
)
from tests.test_phase15_footprints import AB1_RING, AB2_RING, _FakeGoogle, _FakeOverpass, _fp, _resolve, _resolver

AB1_POINT = (12.8412, 80.1531)


def _feature(ring, height=-1.0, confidence=0.9):
    coords = [[lon, lat] for lat, lon in ring] + [[ring[0][1], ring[0][0]]]
    return json.dumps({"type": "Feature", "properties": {"height": height, "confidence": confidence},
                       "geometry": {"type": "Polygon", "coordinates": [coords]}})


class _Resp:
    def __init__(self, body: bytes, status=200):
        self.content, self.status_code = body, status

    def iter_content(self, chunk_size=1):
        yield self.content


class _FakeBlob:
    """Serves dataset-links.csv and one tile, like the real storage account."""

    def __init__(self, features, *, index_ok=True, qk=None):
        self.qk = qk or quadkey(*AB1_POINT)
        self.tile = gzip.compress(("\n".join(features) + "\n").encode())
        self.index_ok = index_ok
        self.calls = []

    def get(self, url, timeout=None, stream=False, headers=None):
        self.calls.append(url)
        if url.endswith("dataset-links.csv"):
            if not self.index_ok:
                return _Resp(b"", 503)
            return _Resp(f"Location,QuadKey,Url,Size,UploadDate\nIndia,{self.qk},https://blob/tile.csv.gz,1MB,2026-08-13\n".encode())
        if url == "https://blob/tile.csv.gz":
            return _Resp(self.tile)
        return _Resp(b"", 404)


def _service(tmp_path, blob, **kw):
    return MicrosoftFootprintService(index_urls="https://blob/dataset-links.csv", cache_dir=tmp_path, session=blob, **kw)


# --- tiles + parsing -----------------------------------------------------------


def test_quadkey_matches_the_bing_tile_system_example():
    # Bing Maps Tile System docs: tile (3, 5) at level 3 has quadkey "213".
    assert tile_to_quadkey(3, 5, 3) == "213"
    qk = quadkey(*AB1_POINT)
    assert len(qk) == 9 and set(qk) <= set("0123")
    assert quadkeys_for_bbox(12.84, 12.842, 80.15, 80.152) == [qk]


def test_feature_is_lon_lat_and_minus_one_means_unknown():
    rings, height, conf = parse_feature(_feature(AB1_RING, height=-1, confidence=0.87))
    assert rings == [AB1_RING] and height is None and conf == pytest.approx(0.87)
    assert parse_feature('{"type":"Feature","geometry":{"type":"Point","coordinates":[1,2]}}') is None
    assert parse_feature("not json") is None


# --- service ---------------------------------------------------------------------


def test_tile_is_downloaded_once_then_queried_locally(tmp_path):
    blob = _FakeBlob([_feature(AB1_RING, height=31.5, confidence=0.93), _feature(AB2_RING)])
    svc = _service(tmp_path, blob)
    found = svc.footprints_near(*AB1_POINT, radius_m=60)
    assert [f.source for f in found] == ["MICROSOFT"]          # AB2 is ~150 m away -> outside the search box
    ab1 = found[0]
    assert ab1.contains(*AB1_POINT) and ab1.tags["height"] == "31.5" and ab1.tags["confidence"] == "0.93"
    calls = len(blob.calls)
    # A new service on the same cache: no download at all.
    again = _service(tmp_path, blob).footprints_near(*AB1_POINT, radius_m=400)
    assert len(again) == 2 and len(blob.calls) == calls


def test_no_published_tile_means_no_outlines_not_an_error(tmp_path):
    blob = _FakeBlob([_feature(AB1_RING)], qk="000000000")
    assert _service(tmp_path, blob).footprints_near(*AB1_POINT, radius_m=60) == []


def test_unreachable_storage_raises_the_resolvers_error_type(tmp_path):
    with pytest.raises(GeocodingConnectionError):
        _service(tmp_path, _FakeBlob([], index_ok=False)).footprints_near(*AB1_POINT, radius_m=60)


# --- through the resolver ----------------------------------------------------------


def test_geocoded_point_gets_the_microsoft_outline_containing_it(tmp_path):
    ms = _service(tmp_path, _FakeBlob([_feature(AB1_RING, confidence=0.91), _feature(AB2_RING)]))
    osm = _FakeOverpass([])
    sources = FootprintSources([("microsoft", ms), ("openstreetmap", osm)])
    loc = _resolve(_resolver(_FakeGoogle(outline=None), sources)).location
    assert loc.geometry == AB1_RING and loc.geometry_source == "MICROSOFT"
    assert loc.geometry_meta["confidence"] == pytest.approx(0.91)
    assert "Microsoft GlobalMLBuildingFootprints" in loc.geometry_note
    assert not [c for c in osm.calls if c[0] == "near"]  # Microsoft answered -> OSM not asked for the outline


def test_falls_back_to_openstreetmap_when_microsoft_has_no_outline_there(tmp_path):
    ms = _service(tmp_path, _FakeBlob([_feature(AB2_RING)]))
    sources = FootprintSources([("microsoft", ms), ("openstreetmap", _FakeOverpass([_fp("way/1", AB1_RING)]))])
    loc = _resolve(_resolver(_FakeGoogle(outline=None), sources)).location
    assert loc.geometry == AB1_RING and loc.geometry_source == "OPENSTREETMAP"


def test_low_confidence_microsoft_outline_is_not_used(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ms_footprints_min_confidence", 0.8)
    ms = _service(tmp_path, _FakeBlob([_feature(AB1_RING, confidence=0.4)]))
    result = _resolve(_resolver(_FakeGoogle(outline=None), FootprintSources([("microsoft", ms)])))
    assert result.location.geometry is None
    assert any("low-confidence" in w for w in result.warnings)


# --- mission: order, camera, output --------------------------------------------------


def _long_location():
    from tests.test_boundary_fix import location
    return location()


def test_ring_starts_at_the_point_nearest_home():
    from app.location.inspection_geometry import compute_inspection_ring
    from tests.test_boundary_fix import ll

    home = ll(120, 0)  # east of the long building
    ring = compute_inspection_ring(_long_location(), standoff_m=15, start_near=home)
    first = ring.points[0]
    from app.location.geofence import haversine_km
    d_first = haversine_km(*first, *home)
    assert all(d_first <= haversine_km(*p, *home) + 1e-9 for p in ring.points)


def test_camera_faces_the_nearest_wall_and_aims_at_mid_height():
    from app.location.building_knowledge import BuildingFacts
    from app.mission.inspection_camera import orient_camera
    from tests.test_boundary_fix import ll, mission

    north_of_wall = ll(60, 25)  # 15 m north of the long building's north wall (y = 10)
    m, aims = orient_camera(mission([north_of_wall], 30), _long_location(),
                            BuildingFacts(building="Long Block", height_m=36.0, height_source="t.pdf", height_page=1))
    wp = m.mission_items[1]
    assert wp.yaw_deg == pytest.approx(180.0, abs=0.5)          # facing south, at the wall -- not at the centre
    assert wp.gimbal_pitch_deg == pytest.approx(-38.7, abs=0.5)  # aim at 18 m from 30 m, 15 m away
    assert wp.take_photo is True and aims[0].distance_m == pytest.approx(15.0, abs=0.5)
    assert m.mission_items[0].yaw_deg is None                    # TAKEOFF untouched


def test_unknown_height_keeps_the_camera_level():
    from app.mission.inspection_camera import orient_camera
    from tests.test_boundary_fix import ll, mission

    m, _ = orient_camera(mission([ll(60, 25)], 30), _long_location(), None)
    assert m.mission_items[1].gimbal_pitch_deg == 0.0


def test_camera_fields_are_never_asked_of_the_llm():
    from app.llm.llm_client import _strict_json_schema
    from app.mission.schema import Mission

    text = json.dumps(_strict_json_schema(Mission))
    assert "yaw_deg" not in text and "gimbal_pitch_deg" not in text and "take_photo" not in text


def test_converter_sends_heading_gimbal_and_photo_to_px4(monkeypatch):
    from tests import test_phase6_converter as conv

    conv._FakeCameraAction.TAKE_PHOTO = "CameraAction.TAKE_PHOTO"
    module = types.ModuleType("mavsdk.mission")
    module.MissionItem, module.MissionPlan = conv._FakeMissionItem, conv._FakeMissionPlan
    pkg = types.ModuleType("mavsdk")
    pkg.mission = module
    monkeypatch.setitem(sys.modules, "mavsdk", pkg)
    monkeypatch.setitem(sys.modules, "mavsdk.mission", module)
    monkeypatch.delitem(sys.modules, "app.mission.converter", raising=False)
    from app.mission.converter import to_mavsdk_mission_plan
    from app.mission.schema import Mission, MissionCommand, MissionItem

    m = Mission(mission_name="t", mission_items=[
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=30),
        MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=12.84, longitude=80.15, altitude=30,
                    yaw_deg=180.0, gimbal_pitch_deg=-20.0, take_photo=True),
    ])
    wp = to_mavsdk_mission_plan(m).plan.mission_items[1]
    assert wp.yaw_deg == 180.0 and wp.gimbal_pitch_deg == -20.0
    assert wp.camera_action == "CameraAction.TAKE_PHOTO" and wp.is_fly_through is False


def test_inspection_summary_returns_polygon_and_waypoints():
    from app.location.inspection_geometry import compute_inspection_ring
    from app.location.models import LocationResolutionResult, LocationResolutionStatus
    from app.mission.generator import MissionGenerationResult
    from app.mission.intent_parser import MissionIntent, MissionType
    from app.mission.location_aware_generator import NaturalLanguageMissionResult, NaturalLanguageMissionStatus
    from app.mission.validator import ValidationResult
    from tests.test_boundary_fix import mission

    loc = _long_location()
    loc.geometry_source, loc.geometry_meta = "MICROSOFT", {"id": "msft/1/2", "confidence": 0.9, "height_m": None}
    ring = compute_inspection_ring(loc, standoff_m=15)
    result = NaturalLanguageMissionResult(
        status=NaturalLanguageMissionStatus.GENERATED,
        intent=MissionIntent(mission_type=MissionType.INSPECTION, location_mentioned=True, building="Long Block"),
        location_result=LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=loc),
        mission_result=MissionGenerationResult(mission=mission(ring.points, 30), validation=ValidationResult(valid=True), attempts=1),
        inspection_ring=ring,
    )
    s = result.inspection_summary()
    assert s["building"]["outline_source"] == "MICROSOFT" and s["building"]["outline_detected"]
    assert len(s["building"]["polygon"][0]) == 4
    assert len([w for w in s["waypoints"] if w["command"] == "WAYPOINT"]) == len(ring.points)
    assert json.dumps(result.as_dict(), default=str)
