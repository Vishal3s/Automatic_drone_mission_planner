"""
Real-world coordinate & building resolution improvement -- Step 1 of
hierarchical resolution ("Resolve the institution/campus... VIT Chennai
-> VIT Chennai campus").

`CampusResolver` wraps `GeocodingService` (dependency-injected, same
pattern as everything else in this improvement) with the campus-specific
bit of judgment: given several raw geocoding candidates for an
institution/campus name, pick the one that actually looks like a
campus/institution (preferring OSM's own `university`/`college`/`school`
place types when present) rather than blindly taking the first result.
"""

from __future__ import annotations

import logging

from app.location.geocoding_service import GeocodingCandidate

log = logging.getLogger(__name__)

# OSM `type` values that indicate "this result is an institution/campus,
# not some unrelated place that merely matched the search text" -- used
# only to PREFER a candidate when several are returned, never to reject
# every other candidate outright (a real campus is sometimes tagged
# `amenity=university` and sometimes just `landuse=education`, and this
# project has no authority to know every possible OSM tagging in
# advance).
_PREFERRED_PLACE_TYPES = {"university", "college", "school", "education"}


def _score(candidate: GeocodingCandidate) -> tuple[int, float]:
    """Sort key: institution-flavored place types first, then Nominatim's own importance score."""
    preferred = 1 if (candidate.place_type in _PREFERRED_PLACE_TYPES or candidate.category == "amenity") else 0
    return (preferred, candidate.importance or 0.0)


class CampusResolver:
    """
    Usage:
        resolver = CampusResolver(geocoding_service)
        candidates = resolver.resolve("VIT Chennai")
        if candidates:
            best = candidates[0]  # highest-scored match
    """

    def __init__(self, geocoding_service) -> None:
        self._geocoding_service = geocoding_service

    def resolve(self, campus_or_institution_name: str) -> list[GeocodingCandidate]:
        """
        Returns every candidate Nominatim found for this name, BEST
        MATCH FIRST (see `_score()`). Empty list -- not an exception --
        means "no results," matching this improvement's spec ("handle...
        no search results" as a normal case, not an error). Raises
        whatever `GeocodingError` subclass the underlying service raised
        for a real failure (timeout/connection/rate-limit/invalid
        response) -- the caller (`LocationResolver`) decides what to do
        with that (e.g. fall back to the cache).
        """
        name = campus_or_institution_name.strip()
        if not name:
            return []
        candidates = self._geocoding_service.search(name, limit=5, want_polygon=True)
        candidates.sort(key=_score, reverse=True)
        return candidates
