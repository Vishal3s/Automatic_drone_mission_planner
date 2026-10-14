"""
Location resolution improvement -- REMAINING phases (A-D) tests.
Run with: pytest tests/test_phase_location_remaining.py -v

Same conventions as tests/test_phase_location_resolution.py: every
external dependency (Nominatim, LLM, Chroma) is faked at its boundary.
The coordinates below are FAKE TEST FIXTURES only -- the resolution code
itself never contains a coordinate.
"""

from __future__ import annotations

import pytest

from app.location.building_names import building_name_variants, mentions_building
from app.location.building_resolver import BuildingResolver
from app.location.campus_resolver import CampusResolver
from app.location.geocoding_service import GeocodingCandidate
from app.location.location_cache import LocationCache
from app.location.location_resolver import LocationResolver
from app.location.models import LocationConfidence, LocationResolutionStatus
from app.mission.location_aware_generator import (
    NaturalLanguageMissionStatus,
    generate_mission_from_natural_language,
    meets_min_confidence,
)

CAMPUS_LAT, CAMPUS_LON = 12.8406, 80.1534
CAMPUS_BBOX = (12.8370, 12.8440, 80.1500, 80.1570)
BUILDING_LAT, BUILDING_LON = 12.8412, 80.1531
BUILDING_POLYGON = [(12.8411, 80.1530), (12.8411, 80.1532), (12.8413, 80.1532), (12.8413, 80.1530)]


class _FakeGeocoder:
    """Knows the campus, and knows the building ONLY by its full name "Academic Block 1" (like real OSM often would)."""

    def __init__(self):
        self.queries: list[str] = []

    def search(self, query, *, limit=5, viewbox=None, bounded=False, want_polygon=False):
        self.queries.append(query)
        q = query.lower()
        if "vit chennai" in q:
            return [GeocodingCandidate(
                display_name="VIT Chennai", latitude=CAMPUS_LAT, longitude=CAMPUS_LON, bounding_box=CAMPUS_BBOX,
                importance=0.7, osm_type="way", osm_id=1, category="amenity", place_type="university", geometry=None,
            )]
        if q.startswith("academic block 1") and viewbox == CAMPUS_BBOX:
            return [GeocodingCandidate(
                display_name="Academic Block 1, VIT Chennai", latitude=BUILDING_LAT, longitude=BUILDING_LON,
                bounding_box=None, importance=0.4, osm_type="way", osm_id=2, category="building", place_type="yes",
                geometry=BUILDING_POLYGON if want_polygon else None,
            )]
        return []


def _resolver(geocoder, retrieve_fn=lambda *a, **k: []):
    return LocationResolver(
        geocoding_service=geocoder,
        campus_resolver=CampusResolver(geocoder),
        building_resolver=BuildingResolver(geocoder, retrieve_fn=retrieve_fn),
        cache=LocationCache(path=None),
    )


# ---------------------------------------------------------------------
# Phase A -- building-name variants
# ---------------------------------------------------------------------


def test_variants_expand_code_to_full_name():
    v = building_name_variants("AB1")
    assert v[0] == "AB1"
    assert "Academic Block 1" in v and "AB-1" in v and "AB 1" in v


def test_variants_reverse_full_name_to_code():
    v = building_name_variants("Academic Block 1")
    assert v[0] == "Academic Block 1"
    assert "AB1" in v


def test_variants_leave_plain_names_alone():
    assert building_name_variants("Library") == ["Library"]


def test_mentions_building_is_whole_name_only():
    variants = building_name_variants("AB1")
    assert mentions_building("4 Academic Block 1 36", variants)
    assert not mentions_building("Academic Block 12 is new", variants)


def test_osm_resolves_ab1_via_full_name_variant():
    geocoder = _FakeGeocoder()
    result = _resolver(geocoder).resolve(institution="VIT Chennai", campus="VIT Chennai", building="AB1")
    assert result.status == LocationResolutionStatus.RESOLVED
    assert result.location.building == "AB1"
    assert result.location.geometry_available is True
    assert any(q.lower().startswith("academic block 1") for q in geocoder.queries)


def test_rag_lookup_matches_full_name_in_document_text():
    class _Chunk:
        def __init__(self, text, source):
            self.text, self.source = text, source

    class _NoBuildingGeocoder(_FakeGeocoder):
        def search(self, query, **kw):
            return super().search(query, **kw) if "vit chennai" in query.lower() else []

    def fake_retrieve(query, k=3):
        return [_Chunk("Academic Block 1: latitude=12.8412, longitude=80.1531", "campus_map.md")]

    result = _resolver(_NoBuildingGeocoder(), fake_retrieve).resolve(
        institution="VIT Chennai", campus="VIT Chennai", building="AB1"
    )
    assert result.status == LocationResolutionStatus.RESOLVED
    assert result.location.source.value == "RAG_KNOWLEDGE"


# ---------------------------------------------------------------------
# Phase A -- minimum-confidence gate
# ---------------------------------------------------------------------


def test_meets_min_confidence_ordering():
    assert meets_min_confidence(LocationConfidence.HIGH, "MEDIUM")
    assert meets_min_confidence(LocationConfidence.MEDIUM, "MEDIUM")
    assert not meets_min_confidence(LocationConfidence.LOW, "MEDIUM")
    assert meets_min_confidence(LocationConfidence.LOW, "LOW")


def test_low_confidence_location_stops_mission_generation(monkeypatch):
    import app.mission.location_aware_generator as lag
    from app.location.models import LocationResolutionResult, LocationSource, MatchDetails, ResolutionType, ResolvedLocation
    from app.mission.intent_parser import MissionIntent, MissionType

    monkeypatch.setattr(lag, "parse_mission_intent", lambda req: MissionIntent(
        mission_type=MissionType.INSPECTION, location_mentioned=True,
        institution="VIT Chennai", campus="VIT Chennai", building="AB1", altitude_m=None,
    ))

    def _must_not_generate(*a, **k):
        raise AssertionError("mission generation must not be reached for a LOW-confidence location")

    monkeypatch.setattr(lag, "generate_validated_mission", _must_not_generate)

    low = ResolvedLocation(
        institution="VIT Chennai", campus="VIT Chennai", building="AB1",
        latitude=BUILDING_LAT, longitude=BUILDING_LON, resolution_type=ResolutionType.POINT,
        source=LocationSource.OPENSTREETMAP, confidence=LocationConfidence.LOW,
        geometry_available=False, match_details=MatchDetails(),
    )

    class _Resolver:
        def resolve(self, **kw):
            return LocationResolutionResult(status=LocationResolutionStatus.RESOLVED, location=low)

    result = generate_mission_from_natural_language("Inspect AB1 of VIT Chennai.", location_resolver=_Resolver())
    assert result.status == NaturalLanguageMissionStatus.LOCATION_LOW_CONFIDENCE
    assert result.mission_result is None
    assert result.accepted is False


# ---------------------------------------------------------------------
# Phase B -- building facts from the existing RAG
# ---------------------------------------------------------------------

from app.location.building_knowledge import BuildingFacts, lookup_building_facts  # noqa: E402
from app.location.inspection_geometry import compute_inspection_ring  # noqa: E402
from app.location.location_safety import (  # noqa: E402
    safety_constraints_for_location,
    validate_building_clearance,
    validate_initial_battery,
)
from app.location.geofence import haversine_km  # noqa: E402
from app.location.models import LocationSource, MatchDetails, ResolutionType, ResolvedLocation  # noqa: E402
from app.mission.schema import Mission, MissionCommand, MissionItem  # noqa: E402


class _Chunk:
    def __init__(self, text, source, page=None):
        self.text, self.source, self.page = text, source, page


# Verbatim text of the user's own Vit_building_details.pdf, page 6 (Table 1), as the PDF loader chunks it.
_PDF_TABLE_CHUNK = (
    "Table 1: Buildings situated inside the university They are building no. Cluster Name H (m) max "
    "1 Boy's Hostel, A block 69 2 Campus Bank 13.5 3 North Square 6.30 A 4 Academic Block 1 36 "
    "5 Gym Khanna 22.50 6 Guest House 15 7 Campus main gate 8.36 Admin block & 8 41 Library B "
    "9 Auditorium 30 10 Sit-out 6.10 11 Gazebos 4.79 12 Health Centre block 22.50 13 Academic block 2 40 C"
)


def _pdf_retrieve(query, k=5):
    return [_Chunk(_PDF_TABLE_CHUNK, "Vit_building_details.pdf", 6)]


def test_height_read_from_user_pdf_table_with_citation():
    facts = lookup_building_facts("AB1", "VIT Chennai", retrieve_fn=_pdf_retrieve)
    assert facts.height_m == 36.0
    assert facts.citation() == "Vit_building_details.pdf, page 6"
    assert lookup_building_facts("AB2", retrieve_fn=_pdf_retrieve).height_m == 40.0


def test_unknown_building_height_is_none_not_guessed():
    assert lookup_building_facts("AB3", retrieve_fn=_pdf_retrieve).height_m is None
    # "Admin block & ... Library" is a broken row -- must not borrow a neighbouring number
    assert lookup_building_facts("Library", retrieve_fn=_pdf_retrieve).height_m is None


def test_height_with_unit_beats_other_numbers_in_row():
    retrieve = lambda q, k=5: [_Chunk("Building | Floors | Height AB1 | 7 | 45 m AB2 | 5 | 32 m", "t.pdf", 1)]
    assert lookup_building_facts("AB1", retrieve_fn=retrieve).height_m == 45.0


def test_conflicting_heights_keep_largest_and_warn():
    retrieve = lambda q, k=5: [_Chunk("AB1 is 36 m tall", "a.md"), _Chunk("Academic Block 1 height 40 m", "b.md")]
    facts = lookup_building_facts("AB1", retrieve_fn=retrieve)
    assert facts.height_m == 40.0 and facts.warnings


def test_rag_failure_returns_empty_facts():
    def broken(q, k=5):
        raise RuntimeError("no index")

    facts = lookup_building_facts("AB1", retrieve_fn=broken)
    assert facts.height_m is None and facts.warnings


# ---------------------------------------------------------------------
# Phase C -- perimeter inspection ring
# ---------------------------------------------------------------------


def _location(polygon=None):
    return ResolvedLocation(
        institution="VIT Chennai", campus="VIT Chennai", building="AB1",
        latitude=BUILDING_LAT, longitude=BUILDING_LON,
        resolution_type=ResolutionType.BUILDING if polygon else ResolutionType.POINT,
        source=LocationSource.OPENSTREETMAP, confidence=LocationConfidence.HIGH,
        geometry_available=polygon is not None, geometry=polygon, match_details=MatchDetails(),
    )


def test_ring_from_polygon_lies_outside_footprint_at_standoff():
    # Boundary fix: the ring follows the outline -- every point is the standoff away from the building itself.
    from app.location.building_footprints import BuildingFootprint
    from app.location.geofence import point_in_polygon

    ring = compute_inspection_ring(_location(BUILDING_POLYGON), standoff_m=15, n_points=8)
    assert ring.footprint_measured and ring.shape == "outline" and len(ring.points) == 8
    outline = BuildingFootprint(osm_id="way/1", rings=[list(BUILDING_POLYGON)])
    for lat, lon in ring.points:
        assert not point_in_polygon(lat, lon, BUILDING_POLYGON)
        assert outline.distance_m(lat, lon) == pytest.approx(15.0, abs=0.5)


def test_ring_from_point_uses_flagged_assumption():
    ring = compute_inspection_ring(_location(None), standoff_m=15, n_points=6)
    assert ring.footprint_measured is False
    assert ring.radius_m == pytest.approx(60.0 + 15.0)


# ---------------------------------------------------------------------
# Phase D -- roof clearance, battery/RTH, RAMR geofence
# ---------------------------------------------------------------------


def _mission(points, altitude):
    items = [MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=altitude)]
    for i, (lat, lon) in enumerate(points, start=2):
        items.append(MissionItem(sequence=i, command=MissionCommand.WAYPOINT, latitude=lat, longitude=lon, altitude=altitude))
    items.append(MissionItem(sequence=len(items) + 1, command=MissionCommand.RETURN_TO_LAUNCH))
    return Mission(mission_name="t", mission_items=items)


_AB1_FACTS = BuildingFacts(building="AB1", height_m=36.0, height_source="Vit_building_details.pdf", height_page=6)


def test_waypoint_over_36m_building_at_30m_is_rejected():
    result = validate_building_clearance(_mission([(BUILDING_LAT, BUILDING_LON)], 30), _location(BUILDING_POLYGON), _AB1_FACTS)
    assert not result.valid
    assert "36 m tall" in result.errors[0] and "46 m" in result.errors[0]


def test_waypoint_over_building_with_clearance_passes():
    result = validate_building_clearance(_mission([(BUILDING_LAT, BUILDING_LON)], 50), _location(BUILDING_POLYGON), _AB1_FACTS)
    assert result.valid


def test_facade_ring_below_roof_is_warning_not_error():
    ring = compute_inspection_ring(_location(BUILDING_POLYGON))
    result = validate_building_clearance(_mission(ring.points, 30), _location(BUILDING_POLYGON), _AB1_FACTS)
    assert result.valid and any("below" in w for w in result.warnings)


def test_battery_not_checked_without_live_readings():
    result = validate_initial_battery(_mission([(BUILDING_LAT, BUILDING_LON)], 30), home=None, battery_percent=None)
    assert result.valid and "NOT checked" in result.warnings[0]


def test_battery_infeasible_is_rejected():
    far = [(BUILDING_LAT + 0.5, BUILDING_LON)]  # ~55 km away
    result = validate_initial_battery(_mission(far, 30), home=(BUILDING_LAT, BUILDING_LON), battery_percent=0.5)
    assert not result.valid


def test_ramr_constraints_carry_resolved_geofence():
    c = safety_constraints_for_location(_location(BUILDING_POLYGON), radius_km=0.5)
    assert c.geofence is not None and c.geofence.radius_km == 0.5
    assert abs(c.geofence.center_latitude_deg - BUILDING_LAT) < 0.001


def test_rag_coordinates_belong_to_the_named_building_not_the_first_in_text():
    from app.location.building_resolver import _rag_lookup

    text = ("Academic Block 2 (AB2): latitude=12.8380, longitude=80.1560. "
            "Academic Block 1 (AB1): latitude=12.8412, longitude=80.1531.")
    hit = _rag_lookup("AB1", "VIT Chennai", lambda q, k=3: [_Chunk(text, "campus_buildings.md")])
    assert hit[:2] == (12.8412, 80.1531)
    assert _rag_lookup("AB3", None, lambda q, k=3: [_Chunk(text, "campus_buildings.md")]) is None
