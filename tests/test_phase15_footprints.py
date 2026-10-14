"""
Phase 15.1 tests -- recognizing a building and its REAL boundary from
OpenStreetMap footprints. Run with: pytest tests/test_phase15_footprints.py -v

Overpass and Google are faked; coordinates are FAKE TEST FIXTURES.
"""

from __future__ import annotations

from app.location.building_footprints import (
    AmbiguousFootprintError,
    BuildingFootprint,
    find_named_footprint,
    footprint_for_point,
    parse_overpass,
)
from app.location.building_names import building_name_variants
from app.location.building_resolver import BuildingResolver
from app.location.campus_resolver import CampusResolver
from app.location.geocoding_service import GeocodingConnectionError
from app.location.google_places_service import GooglePlacesGeocodingService
from app.location.location_cache import LocationCache
from app.location.location_resolver import LocationResolver
from app.location.models import GeometryType, LocationResolutionStatus, LocationSource
from tests.test_phase15_google_maps import AB1, _FakeGoogle

# Two neighbouring buildings inside the fake campus; AB1's outline contains the fake AB1 point (12.8412, 80.1531).
AB1_RING = [(12.8410, 80.1529), (12.8410, 80.1533), (12.8414, 80.1533), (12.8414, 80.1529)]
AB2_RING = [(12.8420, 80.1540), (12.8420, 80.1544), (12.8424, 80.1544), (12.8424, 80.1540)]


def _fp(osm_id, ring, **tags):
    return BuildingFootprint(osm_id=osm_id, rings=[ring], tags={"building": "university", **tags})


class _FakeOverpass:
    def __init__(self, footprints, fail=False):
        self.footprints, self.fail = footprints, fail
        self.calls = []

    def footprints_in_bbox(self, bbox):
        self.calls.append(("bbox", bbox))
        if self.fail:
            raise GeocodingConnectionError("overpass down")
        return self.footprints

    def footprints_near(self, lat, lon, radius_m):
        self.calls.append(("near", (lat, lon)))
        if self.fail:
            raise GeocodingConnectionError("overpass down")
        return self.footprints


def _resolver(google, overpass, retrieve_fn=lambda *a, **k: []):
    service = GooglePlacesGeocodingService(api_key="k", session=google)
    return LocationResolver(
        geocoding_service=service, campus_resolver=CampusResolver(service),
        building_resolver=BuildingResolver(service, retrieve_fn=retrieve_fn),
        cache=LocationCache(path=None), footprint_service=overpass,
    )


def _resolve(r):
    return r.resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")


# --- parsing + pure recognition --------------------------------------------

def test_parse_overpass_way_and_relation():
    payload = {"elements": [
        {"type": "way", "id": 1, "tags": {"building": "yes", "name": "Academic Block-1"},
         "geometry": [{"lat": a, "lon": b} for a, b in AB1_RING]},
        {"type": "relation", "id": 2, "tags": {"building": "yes"},
         "members": [{"type": "way", "role": "outer", "geometry": [{"lat": a, "lon": b} for a, b in AB2_RING]}]},
        {"type": "node", "id": 3},
    ]}
    fps = parse_overpass(payload)
    assert [f.osm_id for f in fps] == ["way/1", "relation/2"]
    assert fps[0].rings[0][0] == (12.8410, 80.1529)


def test_named_footprint_matches_real_world_spellings():
    v = building_name_variants("AB1")
    assert find_named_footprint([_fp("way/1", AB1_RING, name="Academic Block-1"), _fp("way/2", AB2_RING, name="AB2")], v).osm_id == "way/1"
    assert find_named_footprint([_fp("way/1", AB1_RING, ref="AB-1")], v).osm_id == "way/1"
    assert find_named_footprint([_fp("way/2", AB2_RING, name="Academic Block 12")], v) is None


def test_two_named_footprints_are_ambiguous():
    v = building_name_variants("AB1")
    try:
        find_named_footprint([_fp("way/1", AB1_RING, name="AB1"), _fp("way/2", AB2_RING, name="Academic Block 1")], v)
    except AmbiguousFootprintError:
        return
    raise AssertionError("expected ambiguity")


def test_point_inside_outline_selects_it():
    hit = footprint_for_point([_fp("way/1", AB1_RING), _fp("way/2", AB2_RING)], 12.8412, 80.1531)
    assert hit[0].osm_id == "way/1" and hit[1] is None


def test_point_just_outside_snaps_with_warning_far_point_does_not():
    fps = [_fp("way/1", AB1_RING), _fp("way/2", AB2_RING)]
    hit = footprint_for_point(fps, 12.84092, 80.1531, snap_m=15)  # ~9 m south of AB1's edge (an entrance pin)
    assert hit[0].osm_id == "way/1" and "snapped" in hit[1]
    assert footprint_for_point(fps, 12.8395, 80.1531, snap_m=15) is None  # ~170 m away -> no outline


def test_point_between_two_equally_close_outlines_is_not_snapped():
    left = [(12.8410, 80.15290), (12.8410, 80.15300), (12.8412, 80.15300), (12.8412, 80.15290)]
    right = [(12.8410, 80.15320), (12.8410, 80.15330), (12.8412, 80.15330), (12.8412, 80.15320)]
    assert footprint_for_point([_fp("way/1", left), _fp("way/2", right)], 12.8411, 80.15310, snap_m=15) is None


# --- through the resolver ------------------------------------------------------

def test_google_point_gets_the_real_outline_containing_it():
    overpass = _FakeOverpass([_fp("way/1", AB1_RING), _fp("way/2", AB2_RING)])
    loc = _resolve(_resolver(_FakeGoogle(outline=None), overpass)).location
    assert loc.source == LocationSource.GOOGLE_PLACES and loc.place_id == "bld1"
    assert loc.geometry_type == GeometryType.POLYGON and loc.geometry == AB1_RING
    assert loc.geometry_source == "OPENSTREETMAP"


def test_google_does_not_know_ab1_but_osm_name_tag_does():
    overpass = _FakeOverpass([_fp("way/1", AB1_RING, name="Academic Block 1"), _fp("way/2", AB2_RING, name="AB2")])
    result = _resolve(_resolver(_FakeGoogle(building_places=[]), overpass))
    assert result.status == LocationResolutionStatus.RESOLVED
    loc = result.location
    assert loc.source == LocationSource.OPENSTREETMAP and loc.geometry == AB1_RING
    assert loc.confidence.value == "HIGH"
    assert "name tags" in loc.geometry_note


def test_one_coordinate_from_project_documents_becomes_a_boundary():
    class _Chunk:
        text, source = "Academic Block 1 (AB1): latitude=12.8412, longitude=80.1531", "campus_buildings.md"

    overpass = _FakeOverpass([_fp("way/1", AB1_RING), _fp("way/2", AB2_RING)])
    result = _resolve(_resolver(_FakeGoogle(building_places=[]), overpass, lambda q, k=3: [_Chunk()]))
    loc = result.location
    assert result.status == LocationResolutionStatus.RESOLVED
    assert loc.source == LocationSource.RAG_KNOWLEDGE and loc.geometry == AB1_RING
    assert loc.resolution_type.value == "BUILDING" and loc.confidence.value == "MEDIUM"


def test_campus_centre_is_never_used_to_pick_a_building():
    overpass = _FakeOverpass([_fp("way/1", AB1_RING), _fp("way/2", AB2_RING)])  # unnamed buildings
    result = _resolve(_resolver(_FakeGoogle(building_places=[]), overpass))
    assert result.status == LocationResolutionStatus.LOCATION_NOT_RESOLVED
    assert not any(kind == "near" for kind, _ in overpass.calls)  # no "nearest building to the campus" lookup


def test_overpass_down_keeps_the_point_and_warns():
    result = _resolve(_resolver(_FakeGoogle(outline=None), _FakeOverpass([], fail=True)))
    assert result.status == LocationResolutionStatus.RESOLVED
    assert result.location.geometry_type == GeometryType.POINT
    assert any("outline lookup" in w for w in result.warnings)


def test_google_name_with_punctuation_is_accepted_and_candidates_are_logged():
    renamed = dict(AB1, displayName={"text": "VIT Chennai Academic Block-1"})
    result = _resolve(_resolver(_FakeGoogle(building_places=[renamed]), None))
    assert result.status == LocationResolutionStatus.RESOLVED
    seen = result.details["provider_candidates"]
    assert any(c["accepted"] for c in seen)
    assert any(not c["accepted"] and c["name"].startswith("Vellore Institute") for c in seen)  # campus rejected, with a reason


# --- Phase 15.2: automatic campus-wide discovery ---------------------------------

class _CampusListingGoogle(_FakeGoogle):
    """Google that does NOT answer "AB1"/"Academic Block 1" directly, but lists the campus's academic blocks."""

    BLOCKS = [
        dict(AB1, id="b3", displayName={"text": "VIT Chennai Academic Block - 3"}, location={"latitude": 12.8425, "longitude": 80.1545}),
        dict(AB1, id="b1", displayName={"text": "VIT Chennai Academic Block - 1"}),
        dict(AB1, id="b2", displayName={"text": "Academic Block 2"}, location={"latitude": 12.8420, "longitude": 80.1542}),
    ]

    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith("places:searchText") and ("locationRestriction" in json):
            self.calls.append((url, json, headers))
            q = json["textQuery"].lower()
            generic = q.startswith("academic block") and not any(ch.isdigit() for ch in q)
            from tests.test_phase15_google_maps import _Resp
            return _Resp(200, {"places": [*self.BLOCKS]} if generic else {})
        return super().post(url, json=json, headers=headers, timeout=timeout)


def test_ab1_found_automatically_by_listing_campus_blocks():
    overpass = _FakeOverpass([_fp("way/1", AB1_RING), _fp("way/2", AB2_RING)])
    google = _CampusListingGoogle(outline=None)
    result = _resolve(_resolver(google, overpass))
    assert result.status == LocationResolutionStatus.RESOLVED
    loc = result.location
    assert loc.place_id == "b1"                    # Academic Block - 1, not 2 or 3
    assert loc.geometry == AB1_RING                # boundary = real outline containing Google's point
    discovery = [c[1] for c in google.calls if c[1] and c[1].get("textQuery", "").lower() == "academic block"]
    assert discovery and discovery[0]["pageSize"] == 20


# --- Phase 15.3: OpenStreetMap first for the building's one coordinate ------------

from app.location.geocoding_service import GeocodingCandidate  # noqa: E402
from app.location.models import LocationSource as _LS  # noqa: E402


class _FakeNominatim:
    strict_viewbox = True
    provider_name, source_name = "OpenStreetMap/Nominatim", "OPENSTREETMAP"

    def __init__(self, knows_ab1=True):
        self.knows_ab1, self.queries = knows_ab1, []

    def search(self, query, *, limit=5, viewbox=None, bounded=False, want_polygon=False):
        self.queries.append(query)
        if self.knows_ab1 and query.lower().startswith(("ab1", "academic block 1")):
            return [GeocodingCandidate(display_name="Academic Block 1, VIT Chennai, Chennai", latitude=12.8412, longitude=80.1531,
                                       bounding_box=None, importance=0.3, osm_type="node", osm_id=7, category="building",
                                       place_type="university", geometry=None)]
        return []


def _two_provider_resolver(nominatim, google, overpass):
    r = _resolver(google, overpass)
    google_service = r._geocoding_service
    r._building_lookups = [
        (BuildingResolver(nominatim, retrieve_fn=lambda *a, **k: []), _LS.OPENSTREETMAP, "OpenStreetMap/Nominatim"),
        (BuildingResolver(google_service, retrieve_fn=lambda *a, **k: []), _LS.GOOGLE_PLACES, "Google Places"),
    ]
    return r


def test_osm_coordinate_first_then_outline_containing_it():
    google = _FakeGoogle(building_places=[])
    overpass = _FakeOverpass([_fp("way/1", AB1_RING), _fp("way/2", AB2_RING)])
    result = _resolve(_two_provider_resolver(_FakeNominatim(), google, overpass))
    assert result.status == LocationResolutionStatus.RESOLVED
    loc = result.location
    assert loc.source == LocationSource.OPENSTREETMAP and (loc.latitude, loc.longitude) == (12.8412, 80.1531)
    assert loc.geometry == AB1_RING and loc.geometry_source == "OPENSTREETMAP"   # building marked from one point
    building_searches = [c for c in google.calls if c[1] and ("locationRestriction" in c[1] or "locationBias" in c[1])]
    assert building_searches == []  # Google never asked about the building -- OSM answered first


def test_google_used_only_when_osm_has_no_coordinate():
    overpass = _FakeOverpass([_fp("way/1", AB1_RING)])
    result = _resolve(_two_provider_resolver(_FakeNominatim(knows_ab1=False), _FakeGoogle(outline=None), overpass))
    assert result.status == LocationResolutionStatus.RESOLVED
    assert result.location.source == LocationSource.GOOGLE_PLACES
    providers = {c["provider"] for c in result.details["provider_candidates"]}
    assert "Google Places" in providers


def test_default_order_is_openstreetmap_then_google(monkeypatch):
    import app.location.location_resolver as lr

    monkeypatch.setattr(lr.settings, "google_maps_api_key", "k")
    monkeypatch.setattr(lr.settings, "building_point_providers", "openstreetmap,google")
    monkeypatch.setattr("app.location.providers.settings.google_maps_api_key", "k")
    monkeypatch.setattr("app.location.providers.settings.location_provider", "auto")
    r = LocationResolver(cache=LocationCache(path=None), footprint_service=None)
    assert [name for _, _, name in r._building_lookups] == ["OpenStreetMap/Nominatim", "Google Places"]


def test_overpass_falls_back_to_second_endpoint_when_first_is_busy():
    from app.location.building_footprints import OverpassFootprintService
    from tests.test_phase15_google_maps import _Resp

    class _Session:
        def __init__(self):
            self.urls = []

        def post(self, url, data=None, timeout=None, headers=None):
            self.urls.append(url)
            if "first" in url:
                return _Resp(504, None)
            return _Resp(200, {"elements": [{"type": "way", "id": 1, "tags": {"building": "yes"},
                                             "geometry": [{"lat": a, "lon": b} for a, b in AB1_RING]}]})

    session = _Session()
    svc = OverpassFootprintService(url="https://first/api,https://second/api", session=session)
    assert len(svc.footprints_near(12.8412, 80.1531, 100)) == 1
    assert session.urls == ["https://first/api", "https://second/api"]
