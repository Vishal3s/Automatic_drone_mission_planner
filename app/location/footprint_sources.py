"""
Which building-outline sources are used, and in what order
(`settings.building_footprint_provider`, comma-separated):

    microsoft       Microsoft GlobalMLBuildingFootprints (app/location/ms_footprints.py)
    openstreetmap   OpenStreetMap via Overpass (app/location/building_footprints.py)
    off             no outlines (points stay points)

Default "microsoft,openstreetmap": the outline that contains the
building's geocoded point is taken from the first source that has one.
Recognition BY NAME is only possible with OpenStreetMap (Microsoft's
footprints are unnamed), so the name search always uses the OSM source
when it is configured.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.location.building_footprints import BuildingFootprint, OverpassFootprintService
from app.location.geocoding_service import GeocodingError

log = logging.getLogger(__name__)

SOURCE_LABELS = {"MICROSOFT": "Microsoft GlobalMLBuildingFootprints", "OPENSTREETMAP": "OpenStreetMap"}


class FootprintSources:
    """An ordered list of (name, service). Also usable as ONE service: the first source that answers wins."""

    def __init__(self, sources: list[tuple[str, object]]) -> None:
        self.sources = sources

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.sources]

    def named_search_service(self):
        """The source that can recognize a building by its name tags (OpenStreetMap), or None."""
        for name, service in self.sources:
            if name == "openstreetmap":
                return service
        return None

    def _first(self, call) -> list[BuildingFootprint]:
        last: GeocodingError | None = None
        for name, service in self.sources:
            try:
                found = call(service)
            except GeocodingError as exc:
                log.warning("Footprint source %s failed: %s", name, exc)
                last = exc
                continue
            if found:
                return found
        if last is not None and len(self.sources) == 1:
            raise last
        return []

    def footprints_near(self, lat: float, lon: float, radius_m: float) -> list[BuildingFootprint]:
        return self._first(lambda s: s.footprints_near(lat, lon, radius_m=radius_m))

    def footprints_in_bbox(self, bbox) -> list[BuildingFootprint]:
        return self._first(lambda s: s.footprints_in_bbox(bbox))


def build_footprint_sources(spec: str | None = None) -> FootprintSources | None:
    names = [n.strip().lower() for n in (spec or settings.building_footprint_provider).split(",") if n.strip()]
    sources = []
    for name in names:
        if name in ("off", "none"):
            return None
        if name in ("microsoft", "msft", "ms"):
            from app.location.ms_footprints import MicrosoftFootprintService

            sources.append(("microsoft", MicrosoftFootprintService()))
        elif name in ("openstreetmap", "osm"):
            sources.append(("openstreetmap", OverpassFootprintService()))
        else:
            log.warning("Unknown building footprint source %r ignored.", name)
    return FootprintSources(sources) if sources else None


def as_sources(service) -> list[tuple[str, object]]:
    """[(name, service)] for a FootprintSources or a single injected service (tests)."""
    if service is None:
        return []
    if isinstance(service, FootprintSources):
        return list(service.sources)
    return [(getattr(service, "source_name", "openstreetmap"), service)]
