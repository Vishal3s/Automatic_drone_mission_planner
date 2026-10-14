"""
Phase 15 -- picks the geocoding provider LocationResolver uses.

settings.location_provider:
  "auto"          -> Google when GOOGLE_MAPS_API_KEY is set, else OpenStreetMap (default)
  "google"        -> Google (a missing key fails safely: LOCATION_NOT_RESOLVED, never a guess)
  "openstreetmap" -> OpenStreetMap / Nominatim
Adding another provider later = one more class with the same `search()` and one more branch here.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.location.models import LocationSource

log = logging.getLogger(__name__)


def get_geocoding_service(provider: str | None = None):
    choice = (provider or settings.location_provider or "auto").strip().lower()
    if choice == "auto":
        choice = "google" if settings.google_maps_api_key else "openstreetmap"
    if choice == "google":
        from app.location.google_places_service import GooglePlacesGeocodingService

        log.info("Location provider: Google Places")
        return GooglePlacesGeocodingService()
    if choice in ("openstreetmap", "osm", "nominatim"):
        from app.location.geocoding_service import NominatimGeocodingService

        log.info("Location provider: OpenStreetMap/Nominatim")
        return NominatimGeocodingService()
    raise ValueError(f"Unknown LOCATION_PROVIDER {provider or settings.location_provider!r} (use auto, google or openstreetmap).")


def source_for(service) -> LocationSource:
    """The LocationSource label for results from `service` (fakes without a label count as OpenStreetMap, the original behaviour)."""
    name = getattr(service, "source_name", "OPENSTREETMAP")
    try:
        return LocationSource(name)
    except ValueError:
        return LocationSource.OPENSTREETMAP


def provider_name(service) -> str:
    return getattr(service, "provider_name", "OpenStreetMap/Nominatim")
