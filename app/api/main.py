"""
Web UI -- mission control backend.

    python -m app.api.main          # http://127.0.0.1:8000  (opens the browser)

Built on Starlette (the framework FastAPI itself is built on) + uvicorn:
    pip install starlette uvicorn
Routes are thin: every one calls `MissionControlService` (app/api/service.py),
which holds all the logic and the safety rules. Blocking work (LLM, map
lookups) runs in a worker thread so the drone link's event loop stays live.
Binds to localhost by default (settings.ui_host) -- this page can send
missions to a real drone, so it is not exposed to the network.

    GET  /                       mission control page (app/ui/static/mission_control.html)
    GET  /static/<file>.js|.css  shared page scripts
    GET  /api/config             browser map key + Map ID + default drone address (never the server key)
    GET  /api/state              everything the page polls: map view, drone, plans, planning status
    POST /api/plan               {request, home_latitude?, home_longitude?, battery_percent?}
    POST /api/pick               {latitude, longitude}   -- the one click on an unnamed building
    GET  /api/plans, /api/plans/{id}
    GET  /api/plans/{id}/qgc     the accepted plan as a QGroundControl .plan file (download, nothing sent to a drone)
    POST /api/drone/connect      {address?}
    POST /api/drone/disconnect
    POST /api/drone/send         {plan_id, confirm: true}
    POST /api/drone/rtl, /api/drone/hold
"""

from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from app.api.service import MissionControlService
from app.config import settings
from app.mission.qgc_plan import QGCExportError
from app.ui.map_server import STATIC_DIR

log = logging.getLogger(__name__)

PAGE = STATIC_DIR / "mission_control.html"
_STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


class BadRequest(Exception):
    pass


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise BadRequest("Body must be JSON.") from exc
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object.")
    return body


def _float(body: dict, key: str, lo: float, hi: float, *, required: bool = False) -> float | None:
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise BadRequest(f"'{key}' is required.")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"'{key}' must be a number.") from exc
    if not math.isfinite(number) or not lo <= number <= hi:
        raise BadRequest(f"'{key}' must be between {lo} and {hi}.")
    return number


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def create_app(service: MissionControlService | None = None) -> Starlette:
    svc = service or MissionControlService()

    async def index(_: Request) -> Response:
        return FileResponse(PAGE, headers={"Cache-Control": "no-store"})

    async def static(request: Request) -> Response:
        name = request.path_params["name"]
        suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        path = STATIC_DIR / name
        if "/" in name or "\\" in name or suffix not in _STATIC_TYPES or not path.is_file():
            return _error(404, "Not found.")
        return FileResponse(path, media_type=_STATIC_TYPES[suffix], headers={"Cache-Control": "no-store"})

    async def config(_: Request) -> JSONResponse:
        return JSONResponse({
            "browser_api_key": settings.google_maps_browser_api_key,   # Maps JS key only -- never the server key
            "map_id": settings.google_maps_map_id or "DEMO_MAP_ID",
            "configured": bool(settings.google_maps_browser_api_key),
            "px4_address": settings.px4_connection_url,
        })

    async def state(_: Request) -> JSONResponse:
        return JSONResponse(svc.state())

    async def plan(request: Request) -> JSONResponse:
        try:
            body = await _json(request)
            text = str(body.get("request") or "").strip()
            if not text or len(text) > 500:
                raise BadRequest("Type a mission request (up to 500 characters).")
            lat = _float(body, "home_latitude", -90, 90)
            lon = _float(body, "home_longitude", -180, 180)
            if (lat is None) != (lon is None):
                raise BadRequest("Give both home latitude and longitude, or neither.")
            battery = _float(body, "battery_percent", 0, 1)
        except BadRequest as exc:
            return _error(400, str(exc))
        home = (lat, lon) if lat is not None else None
        # Connected drone: plan from its REAL home and battery unless the user gave their own.
        live_home, live_battery = await svc.live_context()
        home = home or live_home
        battery = battery if battery is not None else live_battery
        try:
            return JSONResponse(await run_in_threadpool(svc.plan, text, home=home, battery=battery))
        except ValueError as exc:
            return _error(400, str(exc))
        except RuntimeError as exc:
            return _error(409, str(exc))
        except Exception as exc:  # noqa: BLE001 -- e.g. LLM provider unreachable
            log.exception("Planning failed")
            return _error(502, f"Planning failed: {exc}")

    async def pick(request: Request) -> JSONResponse:
        try:
            body = await _json(request)
            lat = _float(body, "latitude", -90, 90, required=True)
            lon = _float(body, "longitude", -180, 180, required=True)
        except BadRequest as exc:
            return _error(400, str(exc))
        return JSONResponse(await run_in_threadpool(svc.pick, lat, lon))

    async def plans(_: Request) -> JSONResponse:
        return JSONResponse(svc.plans())

    async def plan_detail(request: Request) -> JSONResponse:
        try:
            return JSONResponse(svc.plan_summary(int(request.path_params["plan_id"])))
        except (KeyError, ValueError):
            return _error(404, "No such plan.")

    async def plan_qgc(request: Request) -> Response:
        plan_id = request.path_params["plan_id"]
        try:
            text = svc.qgc_plan_json(int(plan_id))
        except (KeyError, ValueError):
            return _error(404, "No such plan.")
        except PermissionError as exc:
            return _error(400, str(exc))
        except QGCExportError as exc:
            return _error(400, f"Cannot export to QGroundControl: {exc}")
        return Response(text, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="mission_{plan_id}.plan"', "Cache-Control": "no-store"})

    async def plan_waypoints(request: Request) -> Response:
        try:
            name, text = svc.waypoints_download(int(request.path_params["plan_id"]))
        except (KeyError, ValueError):
            return _error(404, "No such plan.")
        except PermissionError as exc:
            return _error(400, str(exc))
        except Exception as exc:  # e.g. a mission with no coordinates cannot be written as a .waypoints file
            log.warning("Waypoints download failed for plan %s: %s", request.path_params["plan_id"], exc)
            return _error(400, f"This plan cannot be written as a .waypoints file: {exc}")
        return Response(text, media_type="text/plain", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            # Plan ids restart at 1 when the server restarts, so the same URL can mean a
            # different plan: never let the browser reuse an old download.
            "Cache-Control": "no-store, max-age=0",
        })

    async def drone_connect(request: Request) -> JSONResponse:
        body = await _json(request) if (await request.body()) else {}
        try:
            return JSONResponse(await svc.connect_drone(str(body.get("address") or "")))
        except RuntimeError as exc:
            return _error(409, str(exc))

    async def drone_disconnect(_: Request) -> JSONResponse:
        try:
            return JSONResponse(await svc.disconnect_drone())
        except RuntimeError as exc:
            return _error(409, str(exc))

    async def drone_send(request: Request) -> JSONResponse:
        try:
            body = await _json(request)
            plan_id = int(body.get("plan_id"))
        except (BadRequest, TypeError, ValueError):
            return _error(400, "Expected JSON {plan_id, confirm}.")
        try:
            return JSONResponse(await svc.send_to_drone(plan_id, confirm=body.get("confirm") is True))
        except PermissionError as exc:
            return _error(400, str(exc))
        except KeyError:
            return _error(404, "No such plan.")
        except ConnectionError as exc:
            return _error(503, str(exc))
        except RuntimeError as exc:
            return _error(409, str(exc))

    async def drone_rtl(_: Request) -> JSONResponse:
        try:
            return JSONResponse(await svc.return_to_launch())
        except ConnectionError as exc:
            return _error(503, str(exc))
        except Exception as exc:  # noqa: BLE001 -- e.g. PX4 refused the command
            return _error(502, f"Return to Launch failed: {exc}")

    async def drone_hold(_: Request) -> JSONResponse:
        try:
            return JSONResponse(await svc.hold())
        except ConnectionError as exc:
            return _error(503, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(502, f"Hold failed: {exc}")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await svc.shutdown()  # close the drone link cleanly

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/static/{name}", static),
            Route("/api/config", config),
            Route("/api/state", state),
            Route("/api/plan", plan, methods=["POST"]),
            Route("/api/pick", pick, methods=["POST"]),
            Route("/api/plans", plans),
            Route("/api/plans/{plan_id}", plan_detail),
            Route("/api/plans/{plan_id}/qgc", plan_qgc),
            Route("/api/plans/{plan_id}/waypoints", plan_waypoints),
            Route("/api/drone/connect", drone_connect, methods=["POST"]),
            Route("/api/drone/disconnect", drone_disconnect, methods=["POST"]),
            Route("/api/drone/send", drone_send, methods=["POST"]),
            Route("/api/drone/rtl", drone_rtl, methods=["POST"]),
            Route("/api/drone/hold", drone_hold, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.service = svc
    return app


def main() -> None:
    import threading
    import webbrowser

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    url = f"http://{settings.ui_host}:{settings.ui_port}/"
    print(f"Mission control: {url}  (Ctrl+C to stop)")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=settings.ui_host, port=settings.ui_port, log_level="warning")


if __name__ == "__main__":
    main()
