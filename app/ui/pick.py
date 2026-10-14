"""
Phase 15.4 -- switch the map to "click the building once" mode when the
campus was found but no map provider names the building. Shared by
scripts/plan_mission.py and the web UI (app/api/service.py).
"""

from __future__ import annotations

import json
import logging

from app.config import settings
from app.location.footprint_sources import build_footprint_sources
from app.location.geocoding_service import GeocodingError

log = logging.getLogger(__name__)


def _summary(result) -> dict:
    d = result.as_dict()
    return {"status": d["status"], "accepted": d["accepted"], "message": d["message"]}


def ask_for_map_pick(store, resolver, result, request, plan, home, on_planned=None) -> bool:
    """
    Phase 15.4: the campus was found but the building wasn't (no map
    provider names it). Instead of stopping, the map asks the user to click
    the building once; that click is the one coordinate, the real outline
    around it is marked, the pick is saved, and the mission is planned.
    Returns True when the map was switched to pick mode.
    """
    intent = result.intent
    details = (result.location_result.details or {}) if result.location_result else {}
    campus_info = details.get("campus_resolved")
    if result.status.value != "LOCATION_NOT_RESOLVED" or not intent.building or not campus_info:
        return False
    bbox = tuple(campus_info["bounding_box"]) if campus_info.get("bounding_box") else None
    institution = intent.institution or intent.campus
    campus = intent.campus or intent.institution

    footprints, note = [], None
    sources = build_footprint_sources() if bbox else None
    if sources is not None:
        try:
            outlines = sources.footprints_in_bbox(bbox)
            footprints = [[list(p) for p in fp.rings[0]] for fp in outlines]
            credit = "Microsoft GlobalMLBuildingFootprints" if outlines and outlines[0].source == "MICROSOFT" else "© OpenStreetMap contributors"
            note = f"{len(footprints)} mapped building outlines shown ({credit})."
        except GeocodingError as exc:
            note = f"Building outlines could not be loaded ({exc}); you can still click the roof on the satellite image."

    def on_pick(latitude: float, longitude: float):
        picked = resolver.resolve_from_map_pick(
            institution=institution, campus=campus, building=intent.building,
            latitude=latitude, longitude=longitude, campus_bbox=bbox,
        )
        if not picked.resolved:
            return False, picked.message
        print(f"\n{intent.building} picked on the map at ({latitude:.6f}, {longitude:.6f}) -- "
              f"{picked.location.geometry_note}\nPlanning: {request}")
        new_result = plan(request)  # the resolver now finds the verified building -> full existing pipeline
        store.publish_plan(new_result, home=home)
        if on_planned is not None:
            on_planned(new_result)
        print(json.dumps(_summary(new_result), indent=2, default=str))
        return True, f"{intent.building} saved. {new_result.message}"

    store.set_pick_handler(on_pick)
    store.publish_pick_request(
        building=intent.building, campus=campus, campus_bbox=bbox,
        campus_center=(campus_info["latitude"], campus_info["longitude"]),
        message=f"{intent.building!r} isn't named on OpenStreetMap or Google, so the app can't tell which building it is on its own.",
        footprints=footprints, footprints_note=note,
    )
    print(f"\n>>> {intent.building} isn't named on any map. Click it once on the map in your browser -- "
          f"the mission is planned automatically after the click (only needed once per building).")
    return True


