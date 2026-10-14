"""
Quick manual demo: run the REAL InspectionQualityAnalyzer (Phase 12,
app/vision/camera_stream.py) against two real image files on disk --
one clear, one unclear -- and print blur/exposure/quality_status for
each. Not a pytest test; just a terminal-runnable sanity check.

Usage:
    python scripts/quality_check_demo.py <clear_image_path> <unclear_image_path>

Example (paths with spaces need quotes):
    python scripts/quality_check_demo.py "images/ChatGPT Image Sep 16, 2026, 07_45_47 AM.png" "images/ChatGPT Image Sep 16, 2026, 07_46_12 AM.png"

Run from the drone_mission_planner/ folder so `app` is importable.
Needs opencv-python-headless + numpy installed (already in requirements.txt).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Running this as `python scripts\quality_check_demo.py` puts this file's own
# folder (scripts\) on sys.path, not the project root -- so `app` isn't
# importable regardless of the current working directory. Add the project
# root (this script's parent folder) explicitly so `python scripts\quality_check_demo.py`
# works no matter where it's invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.replanning.context_builder import DronePosition  # noqa: E402
from app.vision.camera_stream import InspectionFrame, InspectionQualityAnalyzer  # noqa: E402


def check_image(analyzer: InspectionQualityAnalyzer, label: str, path: Path) -> None:
    raw_bytes = path.read_bytes()
    frame = InspectionFrame(
        frame=raw_bytes,
        mission_id="demo-mission-1",
        waypoint_id=2,
        drone_position=DronePosition(latitude_deg=1.000, longitude_deg=2.000, altitude_m=30.0),
    )
    result = analyzer.analyze(frame)
    print(f"--- {label} ({path.name}) ---")
    print(json.dumps(result.as_dict(), indent=2))
    print()


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(1)

    clear_path = Path(sys.argv[1])
    unclear_path = Path(sys.argv[2])

    analyzer = InspectionQualityAnalyzer()
    check_image(analyzer, "clear image", clear_path)
    check_image(analyzer, "unclear image", unclear_path)


if __name__ == "__main__":
    main()
