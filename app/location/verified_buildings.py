"""
Phase 15.4 -- buildings the USER has verified by clicking them once on the
mission map.

When neither OpenStreetMap nor Google names a building (VIT Chennai's AB1:
both return nothing for "AB1"/"Academic Block 1"), the app cannot tell on
its own WHICH campus building it is. The map then asks for one click on
the building; that click is the one coordinate, the real OpenStreetMap
outline around it becomes the boundary, and the result is stored here.
Every later request for that building is resolved from this file
automatically -- no click, no API calls.

This is the spec's resolution priority #3, "existing authoritative
campus/building dataset", built by the user, never by the app guessing.
Stored as plain JSON (`settings.verified_buildings_path`), separate from
LocationCache: it holds the user's own selections (and OpenStreetMap
outlines, ODbL), not Google content, so Google's 30-day caching limit does
not apply. Delete an entry (or the file) to re-pick a building.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.location.building_names import building_name_variants, mentions_building, normalize_name

log = logging.getLogger(__name__)

_UNSET = object()


class VerifiedBuildingStore:
    """`path=None` keeps it in memory only (tests)."""

    def __init__(self, *, path: Path | None = _UNSET) -> None:
        self._path = settings.verified_buildings_full_path if path is _UNSET else path
        self._lock = threading.Lock()
        self._records: list[dict] = []
        if self._path is not None and self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._records = [r for r in data.get("buildings", []) if isinstance(r, dict)]
            except (OSError, ValueError) as exc:
                log.warning("Could not read verified buildings at %s (%s) -- starting empty.", self._path, exc)

    def find(self, campus: str | None, building: str) -> dict | None:
        """The saved record for this building on this campus (any name variant: "AB1" finds a saved "Academic Block 1")."""
        variants = building_name_variants(building)
        campus_key = normalize_name(campus or "")
        with self._lock:
            for record in reversed(self._records):  # newest selection wins
                if normalize_name(record.get("campus") or "") != campus_key:
                    continue
                if mentions_building(record.get("building") or "", variants) or mentions_building(building, building_name_variants(record.get("building") or "")):
                    return dict(record)
        return None

    def save(self, record: dict) -> None:
        record = dict(record, selected_at=datetime.now(timezone.utc).isoformat())
        with self._lock:
            campus_key, building_key = normalize_name(record.get("campus") or ""), normalize_name(record.get("building") or "")
            self._records = [
                r for r in self._records
                if not (normalize_name(r.get("campus") or "") == campus_key and normalize_name(r.get("building") or "") == building_key)
            ]
            self._records.append(record)
            if self._path is not None:
                try:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    self._path.write_text(json.dumps({"buildings": self._records}, indent=2), encoding="utf-8")
                except OSError as exc:
                    log.warning("Could not save verified buildings to %s: %s", self._path, exc)
        log.info("Saved verified building %r at %r (%.6f, %.6f)", record.get("building"), record.get("campus"),
                 record.get("latitude"), record.get("longitude"))
