"""
Microsoft GlobalMLBuildingFootprints as a building-outline source.

    https://github.com/microsoft/GlobalMLBuildingFootprints
    Building footprints (c) Microsoft, licensed CDLA Permissive 2.0.

How the dataset is published (from the repository README and its
examples/example_building_footprints.ipynb):
- An index CSV, `dataset-links.csv`, with one row per (region, tile):
  columns `Location`, `QuadKey`, `Url` (+ size/date). Tiles are Bing Maps
  quadkeys at zoom level 9.
- Each `Url` is a gzipped file of line-delimited GeoJSON features
  (EPSG:4326). Properties: `height` (m, average over the polygon; -1 =
  no estimate) and `confidence` (0-1 footprint score; -1 = not scored).
- The footprints carry NO names. So a building is identified the same way
  as with an OSM outline found from one coordinate: the polygon that
  CONTAINS the building's geocoded point (or is within the snap distance
  of it, with no second outline about as close) -- see
  `building_footprints.footprint_for_point`. Never "the nearest building
  to the campus centre".

A level-9 tile covers roughly 70 x 70 km and can hold a few hundred
thousand buildings, so each tile is downloaded ONCE and stored as a small
SQLite database with an R-tree index (Python's built-in sqlite3 -- no new
dependency) under `settings.ms_footprints_cache_dir`. Later look-ups are
local and instant. Pre-download a tile with:

    python -m scripts.fetch_ms_footprints 12.8406 80.1534
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import sqlite3
import tempfile
import time
from math import cos, floor, log, pi, radians, sin
from pathlib import Path

from app.config import settings
from app.location.building_footprints import BuildingFootprint
from app.location.geocoding_service import (
    GeocodingConnectionError,
    GeocodingInvalidResponseError,
    GeocodingTimeoutError,
)

log_ = logging.getLogger(__name__)

ZOOM = 9
_M_PER_DEG = 111_320.0
ATTRIBUTION = "Building footprints © Microsoft (GlobalMLBuildingFootprints, CDLA Permissive 2.0)"


# ---------------------------------------------------------------------
# Bing Maps tile math (https://learn.microsoft.com/bingmaps/articles/bing-maps-tile-system)
# ---------------------------------------------------------------------


def tile_xy(lat: float, lon: float, zoom: int = ZOOM) -> tuple[int, int]:
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 1 << zoom
    s = sin(radians(lat))
    x = (lon + 180.0) / 360.0
    y = 0.5 - log((1 + s) / (1 - s)) / (4 * pi)
    return min(n - 1, max(0, floor(x * n))), min(n - 1, max(0, floor(y * n)))


def tile_to_quadkey(tx: int, ty: int, zoom: int = ZOOM) -> str:
    digits = []
    for i in range(zoom, 0, -1):
        mask = 1 << (i - 1)
        digits.append(str((1 if tx & mask else 0) + (2 if ty & mask else 0)))
    return "".join(digits)


def quadkey(lat: float, lon: float, zoom: int = ZOOM) -> str:
    return tile_to_quadkey(*tile_xy(lat, lon, zoom), zoom)


def quadkeys_for_bbox(south: float, north: float, west: float, east: float, zoom: int = ZOOM) -> list[str]:
    x0, y0 = tile_xy(north, west, zoom)
    x1, y1 = tile_xy(south, east, zoom)
    return [tile_to_quadkey(x, y, zoom) for x in range(min(x0, x1), max(x0, x1) + 1) for y in range(min(y0, y1), max(y0, y1) + 1)]


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    dlat = radius_m / _M_PER_DEG
    dlon = radius_m / (_M_PER_DEG * max(0.01, cos(radians(lat))))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


# ---------------------------------------------------------------------
# Feature parsing (one GeoJSON line -> rings in (lat, lon))
# ---------------------------------------------------------------------


def parse_feature(line: str) -> tuple[list[list[tuple[float, float]]], float | None, float | None] | None:
    """(outer rings as (lat, lon), height or None, confidence or None); None for a line that isn't a usable polygon."""
    try:
        feature = json.loads(line)
        geom = feature["geometry"]
        props = feature.get("properties") or {}
    except (ValueError, KeyError, TypeError):
        return None
    if geom.get("type") == "Polygon":
        polygons = [geom.get("coordinates") or []]
    elif geom.get("type") == "MultiPolygon":
        polygons = geom.get("coordinates") or []
    else:
        return None
    rings = []
    for poly in polygons:
        if not poly:
            continue
        try:
            ring = [(float(pt[1]), float(pt[0])) for pt in poly[0]]  # GeoJSON is [lon, lat]
        except (TypeError, ValueError, IndexError):
            continue
        if len(ring) >= 4 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) >= 3:
            rings.append(ring)
    if not rings:
        return None

    def _num(key):
        try:
            v = float(props.get(key))
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None  # -1 = no estimate / not scored

    return rings, _num("height"), _num("confidence")


# ---------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------


class MicrosoftFootprintService:
    """
    Same interface as `OverpassFootprintService` (footprints_near /
    footprints_in_bbox), so `LocationResolver` can use either. Network and
    file errors are raised as the geocoding error types the resolver
    already handles.
    """

    def __init__(self, *, index_urls: str | None = None, cache_dir: str | os.PathLike | None = None,
                 timeout_s: float | None = None, session=None, index_max_age_days: float | None = None) -> None:
        self.index_urls = [u.strip() for u in (index_urls or settings.ms_footprints_index_url).split(",") if u.strip()]
        self.cache_dir = Path(cache_dir or settings.ms_footprints_cache_dir)
        self.timeout_s = settings.ms_footprints_timeout_s if timeout_s is None else timeout_s
        self.index_max_age_s = 86400.0 * (settings.ms_footprints_index_max_age_days if index_max_age_days is None else index_max_age_days)
        self._session = session
        self._index: dict[str, list[dict]] | None = None

    # ---- public interface ---------------------------------------------

    def footprints_near(self, lat: float, lon: float, radius_m: float) -> list[BuildingFootprint]:
        return self.footprints_in_bbox(bbox_around(lat, lon, radius_m))

    def footprints_in_bbox(self, bbox: tuple[float, float, float, float]) -> list[BuildingFootprint]:
        """`bbox` is (south, north, west, east)."""
        south, north, west, east = bbox
        out: list[BuildingFootprint] = []
        for qk in quadkeys_for_bbox(south, north, west, east):
            db = self.tile_db(qk)
            if db is None:
                continue
            with sqlite3.connect(db) as con:
                rows = con.execute(
                    "SELECT f.id, f.rings, f.height, f.confidence FROM idx JOIN features f ON f.id = idx.id "
                    "WHERE idx.max_lat >= ? AND idx.min_lat <= ? AND idx.max_lon >= ? AND idx.min_lon <= ?",
                    (south, north, west, east),
                ).fetchall()
            for fid, rings_json, height, confidence in rows:
                tags = {"source": "Microsoft GlobalMLBuildingFootprints"}
                if height is not None:
                    tags["height"] = f"{height:g}"
                    tags["height_source"] = "Microsoft ML estimate (average over the roof)"
                if confidence is not None:
                    tags["confidence"] = f"{confidence:.2f}"
                rings = [[tuple(p) for p in ring] for ring in json.loads(rings_json)]
                out.append(BuildingFootprint(osm_id=f"msft/{qk}/{fid}", rings=rings, tags=tags, source="MICROSOFT"))
        return out

    def tile_db(self, qk: str) -> Path | None:
        """Local SQLite for a quadkey (downloaded + indexed on first use); None if the dataset has no file for it."""
        path = self.cache_dir / f"{qk}.sqlite"
        if path.is_file():
            return path
        rows = self.index().get(qk, [])
        if not rows:
            log_.info("Microsoft footprints: no data published for tile %s.", qk)
            return None
        self._build_tile(qk, rows, path)
        return path

    # ---- index ---------------------------------------------------------

    def index(self) -> dict[str, list[dict]]:
        if self._index is None:
            self._index = self._parse_index(self._index_text())
        return self._index

    @staticmethod
    def _parse_index(text: str) -> dict[str, list[dict]]:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "QuadKey" not in reader.fieldnames or "Url" not in reader.fieldnames:
            raise GeocodingInvalidResponseError("Microsoft dataset-links.csv has no QuadKey/Url columns.")
        index: dict[str, list[dict]] = {}
        for row in reader:
            qk = (row.get("QuadKey") or "").strip()
            url = (row.get("Url") or "").strip()
            if qk and url:
                # Some tiles appear once per region (partitioned by country AND quadkey) -- keep every row.
                index.setdefault(qk.zfill(ZOOM), []).append({"url": url, "location": row.get("Location")})
        return index

    def _index_text(self) -> str:
        cached = self.cache_dir / "dataset-links.csv"
        if cached.is_file() and time.time() - cached.stat().st_mtime < self.index_max_age_s:
            return cached.read_text(encoding="utf-8")
        last = None
        for url in self.index_urls:
            try:
                text = self._get(url).decode("utf-8")
                self._parse_index(text)  # validate before caching
            except (GeocodingConnectionError, GeocodingTimeoutError, GeocodingInvalidResponseError, UnicodeDecodeError) as exc:
                log_.warning("Microsoft index %s failed: %s", url, exc)
                last = exc
                continue
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_text(text, encoding="utf-8")
            return text
        if cached.is_file():  # stale copy beats nothing
            log_.warning("Using a stale Microsoft dataset index (refresh failed).")
            return cached.read_text(encoding="utf-8")
        if isinstance(last, (GeocodingConnectionError, GeocodingTimeoutError, GeocodingInvalidResponseError)):
            raise last
        raise GeocodingInvalidResponseError(f"Microsoft dataset index unreadable: {last}")

    # ---- download + build ---------------------------------------------

    def _get(self, url: str, *, to_file: Path | None = None) -> bytes:
        import requests

        session = self._session or requests.Session()
        try:
            response = session.get(url, timeout=self.timeout_s, stream=to_file is not None,
                                   headers={"User-Agent": settings.nominatim_user_agent})
        except requests.exceptions.Timeout as exc:
            raise GeocodingTimeoutError(f"Microsoft footprints download timed out ({url})") from exc
        except requests.exceptions.RequestException as exc:
            raise GeocodingConnectionError(f"Could not reach Microsoft footprints storage: {exc}") from exc
        if response.status_code != 200:
            raise GeocodingConnectionError(f"Microsoft footprints storage returned HTTP {response.status_code} for {url}")
        if to_file is None:
            return response.content
        with open(to_file, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
        return b""

    def _build_tile(self, qk: str, rows: list[dict], path: Path) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".partial")
        if partial.exists():
            partial.unlink()
        started = time.time()
        count = 0
        con = sqlite3.connect(partial)
        try:
            con.execute("CREATE TABLE features (id INTEGER PRIMARY KEY, rings TEXT, height REAL, confidence REAL, region TEXT)")
            con.execute("CREATE VIRTUAL TABLE idx USING rtree(id, min_lat, max_lat, min_lon, max_lon)")
            for row in rows:
                log_.info("Downloading Microsoft footprints tile %s (%s) -- first use only, may take a minute...", qk, row["location"])
                with tempfile.TemporaryDirectory() as tmp:
                    gz = Path(tmp) / "tile.gz"
                    self._get(row["url"], to_file=gz)
                    try:
                        with gzip.open(gz, "rt", encoding="utf-8") as fh:
                            batch_f, batch_i = [], []
                            for line in fh:
                                parsed = parse_feature(line)
                                if parsed is None:
                                    continue
                                rings, height, conf = parsed
                                count += 1
                                lats = [p[0] for r in rings for p in r]
                                lons = [p[1] for r in rings for p in r]
                                batch_f.append((count, json.dumps([[[round(a, 7), round(b, 7)] for a, b in r] for r in rings]), height, conf, row["location"]))
                                batch_i.append((count, min(lats), max(lats), min(lons), max(lons)))
                                if len(batch_f) >= 5000:
                                    con.executemany("INSERT INTO features VALUES (?,?,?,?,?)", batch_f)
                                    con.executemany("INSERT INTO idx VALUES (?,?,?,?,?)", batch_i)
                                    batch_f, batch_i = [], []
                            con.executemany("INSERT INTO features VALUES (?,?,?,?,?)", batch_f)
                            con.executemany("INSERT INTO idx VALUES (?,?,?,?,?)", batch_i)
                    except (OSError, EOFError) as exc:
                        raise GeocodingInvalidResponseError(f"Microsoft footprints tile {qk} is not valid gzip: {exc}") from exc
            con.commit()
        finally:
            con.close()
        os.replace(partial, path)
        log_.info("Microsoft footprints tile %s ready: %d buildings in %.0fs (%s).", qk, count, time.time() - started, path)
