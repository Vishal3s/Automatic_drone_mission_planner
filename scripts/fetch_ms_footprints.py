"""
Pre-download the Microsoft GlobalMLBuildingFootprints tile for an area,
so the first mission request there doesn't wait for the download.

    python scripts/fetch_ms_footprints.py 12.8406 80.1534          # VIT Chennai
    python scripts/fetch_ms_footprints.py 12.8406 80.1534 --check  # also list the buildings within 150 m

The tile (Bing quadkey, zoom 9, ~70 km across) is stored once under
settings.ms_footprints_cache_dir as a SQLite file with a spatial index.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable from any cwd

from app.location.geocoding_service import GeocodingError  # noqa: E402
from app.location.ms_footprints import MicrosoftFootprintService, quadkey  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("latitude", type=float)
    parser.add_argument("longitude", type=float)
    parser.add_argument("--check", action="store_true", help="list the footprints within 150 m of the point")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    service = MicrosoftFootprintService()
    qk = quadkey(args.latitude, args.longitude)
    print(f"Tile (quadkey, zoom 9): {qk}")
    try:
        db = service.tile_db(qk)
    except GeocodingError as exc:
        print(f"Download failed: {exc}")
        return 1
    if db is None:
        print("The dataset has no footprints for this tile.")
        return 1
    print(f"Ready: {db}")
    if args.check:
        found = service.footprints_near(args.latitude, args.longitude, 150)
        inside = [f for f in found if f.contains(args.latitude, args.longitude)]
        print(f"{len(found)} footprints within ~150 m; containing the point: {[f.osm_id for f in inside] or 'none'}")
        for f in found[:20]:
            print(f"  {f.osm_id}  area {f.area_m2():.0f} m2  height {f.tags.get('height', '?')}  confidence {f.tags.get('confidence', '?')}"
                  f"  distance {f.distance_m(args.latitude, args.longitude):.0f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
