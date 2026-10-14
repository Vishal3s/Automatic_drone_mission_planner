"""
Road lookup for the road scan: named OpenStreetMap ways near a point, as polylines.

Kept separate from building_footprints.py on purpose: that module returns
polygons for buildings, and roads need the centrelines. Uses the same
Overpass endpoints and the same error types as the geocoding services.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.location.geocoding_service import (
    GeocodingConnectionError,
    GeocodingError,
    GeocodingInvalidResponseError,
    GeocodingRateLimitedError,
    GeocodingTimeoutError,
)
from app.mission.road_scan import RoadWay

log = logging.getLogger(__name__)


def _post(url: str, query: str, timeout_s: float, session=None) -> dict:
    import requests

    session = session or requests.Session()
    try:
        response = session.post(url, data={"data": query}, timeout=timeout_s, headers={"User-Agent": settings.nominatim_user_agent})
    except requests.exceptions.Timeout as exc:
        raise GeocodingTimeoutError(f"Overpass API timed out after {timeout_s}s") from exc
    except requests.exceptions.RequestException as exc:
        raise GeocodingConnectionError(f"Could not reach the Overpass API: {exc}") from exc
    if response.status_code == 429:
        raise GeocodingRateLimitedError("Overpass API rate limit (HTTP 429) -- try again shortly.")
    if response.status_code in (502, 503, 504):
        raise GeocodingConnectionError(f"Overpass API busy/unavailable (HTTP {response.status_code}).")
    if response.status_code != 200:
        raise GeocodingInvalidResponseError(f"Overpass API returned HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError as exc:
        raise GeocodingInvalidResponseError("Overpass API response was not JSON.") from exc


def fetch_named_roads(lat: float, lon: float, radius_m: float, *, url: str | None = None, timeout_s: float | None = None, session=None) -> list[RoadWay]:
    """Named highway ways within `radius_m` of (lat, lon). Tries each endpoint in turn; raises GeocodingError if none answers."""
    timeout = float(settings.overpass_timeout_s if timeout_s is None else timeout_s)
    query = (
        f'[out:json][timeout:{int(timeout)}];'
        f'way["highway"]["name"](around:{int(radius_m)},{lat},{lon});out geom tags;'
    )
    endpoints = [u.strip() for u in (url or settings.overpass_url).split(",") if u.strip()]
    last: GeocodingError | None = None
    payload = None
    for endpoint in endpoints:
        try:
            payload = _post(endpoint, query, timeout, session)
            break
        except (GeocodingConnectionError, GeocodingTimeoutError, GeocodingRateLimitedError) as exc:
            log.warning("Overpass endpoint %s failed (%s)", endpoint, exc)
            last = exc
    if payload is None:
        raise last or GeocodingConnectionError("No Overpass endpoint configured.")

    ways = []
    for el in payload.get("elements", []) or []:
        if el.get("type") != "way":
            continue
        pts = [(float(p["lat"]), float(p["lon"])) for p in (el.get("geometry") or []) if "lat" in p and "lon" in p]
        if len(pts) >= 2:
            ways.append(RoadWay(name=(el.get("tags") or {}).get("name", ""), points=pts))
    return ways
