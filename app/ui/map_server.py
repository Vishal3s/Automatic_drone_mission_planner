"""
Phase 15 -- a tiny LOCAL web server for the mission map (standard library
only: http.server). No web framework was added: this project had no
existing frontend or HTTP backend (app/ui/ was an empty placeholder), so
the smallest working option is one static page + two JSON endpoints.

    GET /                  -> app/ui/static/mission_map.html (Google Maps JavaScript API page)
    GET /api/mission-view  -> MissionViewStore.snapshot()   (polled by the page every second)
    GET /api/maps-config   -> the BROWSER key + Map ID       (never the server key)

Binds to 127.0.0.1 by default (settings.map_server_host) so it is not
reachable from the network. The browser key is necessarily visible to the
browser -- that is true of every Maps JavaScript API key -- which is why it
is a separate key restricted by HTTP referrer (see config.py / README).

Usage:
    store = MissionViewStore()
    url = open_mission_map(store)   # starts the server once (background thread) and opens the browser
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import settings
from app.ui.mission_view import MissionViewStore

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAP_PAGE = STATIC_DIR / "mission_map.html"

_server_lock = threading.Lock()
_running: dict[tuple[str, int], ThreadingHTTPServer] = {}


def maps_config() -> dict:
    """What the browser is allowed to see. The server-side GOOGLE_MAPS_API_KEY is deliberately absent."""
    return {
        "browser_api_key": settings.google_maps_browser_api_key,
        "map_id": settings.google_maps_map_id or "DEMO_MAP_ID",
        "configured": bool(settings.google_maps_browser_api_key),
    }


def _make_handler(store: MissionViewStore):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, status: int = 200) -> None:
            self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json")

        def do_GET(self) -> None:  # noqa: N802 -- http.server naming
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                try:
                    self._send(200, MAP_PAGE.read_bytes(), "text/html; charset=utf-8")
                except OSError as exc:
                    self._send(500, f"Map page missing: {exc}".encode(), "text/plain")
            elif path.startswith("/static/") and path.endswith(".js") and "/" not in path[len("/static/"):]:
                f = STATIC_DIR / path[len("/static/"):]  # flat whitelist: /static/<name>.js only
                if f.is_file():
                    self._send(200, f.read_bytes(), "text/javascript; charset=utf-8")
                else:
                    self._send(404, b"Not found", "text/plain")
            elif path == "/api/mission-view":
                try:
                    self._json(store.snapshot())
                except Exception as exc:  # noqa: BLE001 -- the page shows the error instead of hanging
                    log.exception("mission-view snapshot failed")
                    self._json({"has_plan": False, "error": str(exc)}, status=500)
            elif path == "/api/maps-config":
                self._json(maps_config())
            else:
                self._send(404, b"Not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            """Phase 15.4: POST /api/pick {"latitude": .., "longitude": ..} -- the user's one click on the building."""
            if self.path.split("?", 1)[0] != "/api/pick":
                self._send(404, b"Not found", "text/plain")
                return
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 10_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                lat, lon = float(body["latitude"]), float(body["longitude"])
            except (ValueError, KeyError, TypeError):
                self._json({"ok": False, "message": "Expected JSON {latitude, longitude}."}, status=400)
                return
            self._json(store.handle_pick(lat, lon))

        def log_message(self, fmt: str, *args) -> None:  # keep the 1 s polling out of the console
            log.debug("map server: " + fmt, *args)

    return Handler


def start_map_server(store: MissionViewStore, *, host: str | None = None, port: int | None = None) -> str:
    """Starts the server in a daemon thread (once per host/port) and returns its URL."""
    host = host or settings.map_server_host
    port = settings.map_server_port if port is None else port
    with _server_lock:
        server = _running.get((host, port))
        if server is None:
            server = ThreadingHTTPServer((host, port), _make_handler(store))  # OSError if the port is taken
            threading.Thread(target=server.serve_forever, name="mission-map-server", daemon=True).start()
            _running[(host, port)] = server
            log.info("Mission map server running at http://%s:%d/", host, server.server_address[1])
    return f"http://{host}:{server.server_address[1]}/"


def stop_map_server(*, host: str | None = None, port: int | None = None) -> None:
    host = host or settings.map_server_host
    port = settings.map_server_port if port is None else port
    with _server_lock:
        server = _running.pop((host, port), None)
    if server is not None:
        server.shutdown()
        server.server_close()


def open_mission_map(store: MissionViewStore, *, open_browser: bool = True, host: str | None = None, port: int | None = None) -> str:
    """Spec step 9: called when a mission is submitted -- the map opens on its own; the user never searches for the location."""
    url = start_map_server(store, host=host, port=port)
    if open_browser:
        try:
            webbrowser.open(url, new=0)
        except Exception as exc:  # noqa: BLE001 -- headless machine: print the URL instead
            log.warning("Could not open a browser (%s) -- open %s manually.", exc, url)
    return url
