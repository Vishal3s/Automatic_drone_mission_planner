"""
Real-world coordinate & building resolution improvement -- Step 11:
location caching.

    Add caching for successfully resolved locations... Avoid unnecessary
    external API calls. The cache should be designed so that locations
    can be refreshed or invalidated when necessary.

Nominatim's own usage policy explicitly asks callers to cache results
rather than repeat identical queries, so this isn't just a nice-to-have
-- it's part of being a well-behaved API consumer (see
`geocoding_service.py`'s docstring for the rest of that policy).

Persisted as a plain JSON file (`settings.location_cache_path`) -- same
persistence idea as Chroma's `chroma_persist_path` (Phase 2), just
without a vector database's complexity, since this is a handful of small
records, not embeddings.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.location.models import (
    LocationConfidence,
    LocationSource,
    MatchDetails,
    ResolutionType,
    ResolvedLocation,
)

log = logging.getLogger(__name__)

# Sentinel distinguishing "no path argument given -> use settings.location_cache_full_path"
# from an EXPLICIT `path=None` -> disable persistence entirely (in-memory only, used by
# tests so they never touch disk). A bare `path: Path | None = None` default would conflate
# the two -- this sentinel is what keeps them apart.
_UNSET = object()


def _normalize_key(institution: str | None, campus: str | None, building: str | None) -> str:
    """Case/whitespace-insensitive cache key -- "VIT Chennai"/"vit chennai " must hit the same entry."""
    parts = [(institution or "").strip().lower(), (campus or "").strip().lower(), (building or "").strip().lower()]
    return "|".join(parts)


@dataclass
class _CacheEntry:
    location: ResolvedLocation
    cached_at: datetime

    def is_expired(self, ttl_hours: float) -> bool:
        return datetime.now(timezone.utc) - self.cached_at > timedelta(hours=ttl_hours)


class LocationCache:
    """
    Usage:
        cache = LocationCache()
        cache.set("VIT Chennai", "VIT Chennai", "AB1", resolved_location)
        hit = cache.get("VIT Chennai", "VIT Chennai", "AB1")   # None if missing/expired

    In-memory dict backed by an optional JSON file (`path=None` disables
    persistence entirely -- used by tests so they never touch disk).
    Thread-safety is a plain `Lock` (this project has no concurrent
    resolution callers today, same level of caution `MissionUpdateManager`
    already uses for its own lock).
    """

    def __init__(self, *, path: Path | None = _UNSET, ttl_hours: float | None = None) -> None:
        self._path = settings.location_cache_full_path if path is _UNSET else path
        self._ttl_hours = ttl_hours if ttl_hours is not None else settings.location_cache_ttl_hours
        self._lock = threading.Lock()
        self._entries: dict[str, _CacheEntry] = {}
        if self._path is not None:
            self._load()

    def get(self, institution: str | None, campus: str | None, building: str | None) -> ResolvedLocation | None:
        key = _normalize_key(institution, campus, building)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.is_expired(self._ttl_hours):
                log.info("Location cache entry %r expired (TTL %.1fh) -- treating as a miss.", key, self._ttl_hours)
                del self._entries[key]
                self._save_locked()
                return None
            return entry.location

    def set(self, institution: str | None, campus: str | None, building: str | None, location: ResolvedLocation) -> None:
        key = _normalize_key(institution, campus, building)
        with self._lock:
            self._entries[key] = _CacheEntry(location=location, cached_at=datetime.now(timezone.utc))
            self._save_locked()

    def invalidate(self, institution: str | None, campus: str | None, building: str | None) -> bool:
        """Returns True if an entry was actually removed."""
        key = _normalize_key(institution, campus, building)
        with self._lock:
            existed = key in self._entries
            self._entries.pop(key, None)
            if existed:
                self._save_locked()
            return existed

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._save_locked()

    def __len__(self) -> int:
        return len(self._entries)

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Could not read location cache at %s (%s) -- starting empty.", self._path, exc)
            return

        for key, record in raw.items():
            try:
                loc = record["location"]
                match_details = MatchDetails(**loc.get("match_details", {}))
                location = ResolvedLocation(
                    institution=loc.get("institution"),
                    campus=loc.get("campus"),
                    building=loc.get("building"),
                    latitude=loc["latitude"],
                    longitude=loc["longitude"],
                    resolution_type=ResolutionType(loc["resolution_type"]),
                    source=LocationSource(loc["source"]),
                    confidence=LocationConfidence(loc["confidence"]),
                    geometry_available=loc.get("geometry_available", False),
                    geometry=[tuple(p) for p in loc["geometry"]] if loc.get("geometry") else None,
                    bounding_box=tuple(loc["bounding_box"]) if loc.get("bounding_box") else None,
                    raw_source_name=loc.get("raw_source_name"),
                    match_details=match_details,
                    resolved_at=datetime.fromisoformat(loc["resolved_at"]),
                    # Phase 15 fields -- absent in caches written before Phase 15, hence .get()
                    place_id=loc.get("place_id"),
                    formatted_address=loc.get("formatted_address"),
                    geometry_parts=[[tuple(p) for p in ring] for ring in loc["geometry_parts"]] if loc.get("geometry_parts") else None,
                    geometry_source=loc.get("geometry_source"),
                    geometry_note=loc.get("geometry_note"),
                    geometry_meta=loc.get("geometry_meta"),
                )
                self._entries[key] = _CacheEntry(
                    location=location, cached_at=datetime.fromisoformat(record["cached_at"])
                )
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("Skipping malformed location cache entry %r: %s", key, exc)

    def _save_locked(self) -> None:
        """Caller must already hold `self._lock`."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            raw = {
                key: {"location": entry.location.as_dict(), "cached_at": entry.cached_at.isoformat()}
                for key, entry in self._entries.items()
            }
            self._path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not persist location cache to %s: %s", self._path, exc)
