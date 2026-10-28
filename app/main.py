"""PrintWatch application. Management is deliberately restricted to loopback."""

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import secrets
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from app.cameras import CameraError, CameraManager
from app.frontend import FrontendVersion
from app.models import Camera, Layout, Playlist, Settings
from app.store import Store, new_id, public_camera

ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 50 * 1024 * 1024
VERSION = "0.1.0"

# Scoped display tokens appear in paths; never send them to an access log.
logging.getLogger("uvicorn.access").disabled = True
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def is_admin(connection):
    client = connection.client.host if connection.client else ""
    host = (connection.url.hostname or "").lower()
    try:
        local = ipaddress.ip_address(client).is_loopback
    except ValueError:
        local = False
    return local and host in ("localhost", "127.0.0.1", "::1")


def require_admin(request):
    if not is_admin(request):
        raise HTTPException(403, "Configuration is available only at localhost on the server machine")


def find(state, collection, item_id):
    result = next((item for item in state[collection] if item["id"] == item_id), None)
    if result is None:
        raise HTTPException(404, "Item not found")
    return result


def display_state(store, token):
    state = store.read()
    layout = next(
        (item for item in state["layouts"]
         if secrets.compare_digest(item["display_token"], token)), None
    )
    if layout is None:
        raise HTTPException(403, "Display link is invalid or has been revoked")
    if not layout.get("published"):
        raise HTTPException(409, "This layout has not been published yet")
    published = layout["published"]
    camera_ids = {t["source_id"] for t in published["tiles"] if t["type"] == "camera"}
    playlist_ids = {t["source_id"] for t in published["tiles"] if t["type"] == "advertisement"}
    playlists = [p for p in state["playlists"] if p["id"] in playlist_ids]
    asset_ids = {i["asset_id"] for p in playlists for i in p["items"]}
    return {
        "revision": state["revision"], "layout": published,
        "cameras": [public_camera(c, display=True) for c in state["cameras"]
                    if c["id"] in camera_ids],
        "playlists": playlists,
        "assets": [a for a in state["assets"] if a["id"] in asset_ids],
    }


def authorize_media(connection, store, collection, item_id):
    if is_admin(connection):
        return
    token = connection.query_params.get("token", "")
    state = display_state(store, token)
    if not any(item["id"] == item_id for item in state[collection]):
        raise HTTPException(403, "This display does not have access to this resource")


def validate_playlist(state, data):
    for item in data["items"]:
        asset = find(state, "assets", item["asset_id"])
        if asset["status"] != "ready" or item["page"] >= len(asset["pages"]):
            raise HTTPException(422, "Playlist items must reference a ready asset and valid page")
        if data["transition"] != "cut" and data["transition_ms"] >= item["duration"] * 1000:
            raise HTTPException(422, "Transition must be shorter than every slide duration")


def validate_layout(state, data):
    ids = set()
    for tile in data["tiles"]:
        if tile["id"] in ids:
            raise HTTPException(422, "Tile IDs must be unique")
        ids.add(tile["id"])
        find(state, "cameras" if tile["type"] == "camera" else "playlists", tile["source_id"])
        if tile["x"] + tile["width"] > data["width"] + 1:
            raise HTTPException(422, "A tile extends beyond the layout width")
        if tile["y"] + tile["height"] > data["height"] + 1:
            raise HTTPException(422, "A tile extends beyond the layout height")


def create_app(data_dir=None, frontend_dir=None):
    directory = Path(data_dir or os.getenv("PRINTWATCH_DATA_DIR", ROOT / "data"))
    static_dir = Path(frontend_dir or ROOT / "static")
    static_dir.mkdir(exist_ok=True)
    frontend_version = FrontendVersion(static_dir)

    @asynccontextmanager
    async def lifespan(application):
        application.state.store = Store(directory)
        application.state.cameras = CameraManager(application.state.store)
        application.state.render_limit = asyncio.Semaphore(1)
        application.state.render_tasks = set()
        # Interrupted jobs are explicit failures, never permanently "processing".
        def recover(state):
            for asset in state["assets"]:
                if asset["status"] == "processing":
                    asset.update(status="error", error="Processing interrupted. Delete and re-upload.")
        application.state.store.change(recover)
        yield
        tasks = application.state.render_tasks
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await application.state.cameras.close()
        application.state.store.close()

    application = FastAPI(title="PrintWatch", version=VERSION, lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)

    @application.middleware("http")
    async def guard(request: Request, call_next):
        # Exact origin checks also protect localhost APIs from drive-by websites and DNS rebinding.
        origin = request.headers.get("origin")
        if origin:
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin != expected:
                return JSONResponse({"detail": "Cross-origin requests are not allowed"}, 403)
        if request.headers.get("sec-fetch-site") == "cross-site" and request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Cross-site requests are not allowed"}, 403)
        path = request.url.path
        public_get = request.method == "GET" and (
            path in ("/api/health", "/api/frontend-version") or path.startswith("/api/display/")
            or path == "/api/gateway/video-rtc.js"
            or (path.startswith("/api/cameras/") and path.endswith("/snapshot"))
            or (path.startswith("/api/assets/") and "/pages/" in path)
        )
        if path.startswith("/api/") and not public_get and not is_admin(request):
            return JSONResponse({"detail": "Configuration is available only at localhost "
                                 "on the server machine"}, 403)
        length = request.headers.get("content-length")
        try:
            if length and int(length) > MAX_UPLOAD + 1024 * 1024:
                return JSONResponse({"detail": "Upload exceeds the 50 MB limit"}, 413)
        except ValueError:
            return JSONResponse({"detail": "Invalid request length"}, 400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
        if (request.url.path.startswith(("/api/", "/display/", "/static/"))
                or request.url.path == "/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors contain raw input; do not echo access codes back to the client.
        return JSONResponse({
            "detail": "; ".join(
                f"{'.'.join(str(x) for x in error['loc'][1:])}: {error['msg']}"
                for error in exc.errors()
            )
        }, status_code=422)

    @application.exception_handler(CameraError)
    async def camera_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @application.get("/api/health")
    async def health(request: Request):
        return {"status": "ok", "version": VERSION,
                "gateway": await request.app.state.cameras.gateway_available()}

    @application.get("/api/frontend-version")
    async def browser_version():
        return {"version": frontend_version.current()}

    @application.get("/api/state")
    async def state(request: Request):
        require_admin(request)
        value = request.app.state.store.read()
        value["cameras"] = [public_camera(camera) | request.app.state.cameras.status.get(
            camera["id"], {"status": "unconfigured" if not camera["enabled"] else "idle", "error": ""}
        ) for camera in value["cameras"]]
        return value

    @application.get("/api/diagnostics")
    async def diagnostics(request: Request):
        require_admin(request)
        return {
            "version": VERSION, "gateway": await request.app.state.cameras.gateway_available(),
            "ffmpeg": bool(shutil.which("ffmpeg")), "storage": str(directory),
            "admin_mode": "localhost only", "upload_limit_mb": 50, "pdf_page_limit": 200,
            "transport": request.url.scheme,
            "notes": [
                "Enable LAN live view on Bambu H2/P2 printers.",
                "U1 stock camera support depends on firmware and a valid local token.",
                "TLS certificate verification can be relaxed only per camera.",
                "Use HTTPS for display pairing on untrusted networks.",
            ],
        }

    @application.put("/api/settings")
    async def update_settings(body: Settings, request: Request):
        require_admin(request)
        return request.app.state.store.change(
            lambda state: state.update(settings=body.model_dump()) or state["settings"]
        )

    async def save_camera(body, request, camera_id=None):
        require_admin(request)
        store = request.app.state.store
        data = body.model_dump(exclude={"password"})
        if body.kind in ("bambu", "u1") and not body.host:
            raise HTTPException(422, "A printer hostname or IP address is required")
        if body.kind not in ("bambu", "u1") and not body.url:
            raise HTTPException(422, "A stream URL is required")
        camera_id = camera_id or new_id()
        def save(value):
            previous = find(value, "cameras", camera_id) if any(
                c["id"] == camera_id for c in value["cameras"]
            ) else None
            secret = (previous or {}).get("password_encrypted", "")
            if body.password is not None:
                secret = store.encrypt(body.password)
            item = data | {"id": camera_id, "password_encrypted": secret}
            if previous is not None:
                value["cameras"][value["cameras"].index(previous)] = item
            else:
                if len(value["cameras"]) >= 64:
                    raise HTTPException(422, "Maximum 64 configured cameras")
                value["cameras"].append(item)
            return item
        saved = store.change(save)
        await request.app.state.cameras.forget(camera_id)
        return public_camera(saved)

    @application.post("/api/cameras")
    async def add_camera(body: Camera, request: Request):
        return await save_camera(body, request)

    @application.put("/api/cameras/{camera_id}")
    async def edit_camera(camera_id: str, body: Camera, request: Request):
        require_admin(request)
        find(request.app.state.store.read(), "cameras", camera_id)
        return await save_camera(body, request, camera_id)

    @application.delete("/api/cameras/{camera_id}")
    async def delete_camera(camera_id: str, request: Request):
        require_admin(request)
        def remove(state):
            camera = find(state, "cameras", camera_id)
            if any(t["source_id"] == camera_id for layout in state["layouts"]
                   for t in layout["tiles"] + (layout.get("published") or {}).get("tiles", [])):
                raise HTTPException(409, "Remove this camera from draft and published layouts first")
            state["cameras"].remove(camera)
            return {"ok": True}
        result = request.app.state.store.change(remove)
        await request.app.state.cameras.forget(camera_id)
        return result

    @application.post("/api/cameras/{camera_id}/test")
    async def test_camera(camera_id: str, request: Request):
        require_admin(request)
        camera = find(request.app.state.store.read(), "cameras", camera_id)
        try:
            return {"ok": True, "message": await request.app.state.cameras.test(camera)}
        except CameraError as exc:
            request.app.state.cameras.status[camera_id] = {"status": "offline", "error": str(exc)}
            return {"ok": False, "message": str(exc)}
        except Exception:
            return {"ok": False, "message": "Camera test failed. Verify its settings and firmware."}

    @application.get("/api/cameras/{camera_id}/snapshot")
    async def snapshot(camera_id: str, request: Request):
        store = request.app.state.store
        authorize_media(request, store, "cameras", camera_id)
        camera = find(store.read(), "cameras", camera_id)
        frame = await request.app.state.cameras.snapshot(camera)
        return Response(frame, media_type="image/png" if frame.startswith(b"\x89PNG") else "image/jpeg")

    @application.get("/api/gateway/video-rtc.js")
    async def gateway_js(request: Request):
        manager = request.app.state.cameras
        try:
            response = await manager.client.get(manager.gateway + "/video-rtc.js", timeout=3)
            if response.status_code != 200:
                raise HTTPException(503, "Video gateway is not installed or running")
            return Response(response.content, media_type="text/javascript")
        except httpx.HTTPError:
            raise HTTPException(503, "Video gateway is not installed or running") from None

    @application.websocket("/api/cameras/{camera_id}/stream")
    async def stream(camera_id: str, websocket: WebSocket):
        origin = websocket.headers.get("origin")
        scheme = "https" if websocket.url.scheme == "wss" else "http"
        if origin and origin != f"{scheme}://{websocket.headers.get('host', '')}":
            await websocket.close(code=1008)
            return
        store = websocket.app.state.store
        manager = websocket.app.state.cameras
        try:
            authorize_media(websocket, store, "cameras", camera_id)
            camera = find(store.read(), "cameras", camera_id)
            await manager.register(camera)
        except (HTTPException, CameraError):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        gateway_ws = manager.gateway.replace("http://", "ws://", 1)
        try:
            async with websockets.connect(
                f"{gateway_ws}/api/ws?src={camera_id}", max_size=2**20, proxy=None
            ) as upstream:
                async def browser_to_gateway():
                    while True:
                        raw = await websocket.receive_text()
                        if len(raw) > 65536:
                            return
                        message = json.loads(raw)
                        if message.get("type") in ("webrtc/offer", "webrtc/candidate", "mse"):
                            await upstream.send(json.dumps(message))
                async def gateway_to_browser():
                    while True:
                        raw = await upstream.recv()
                        if isinstance(raw, bytes):
                            await websocket.send_bytes(raw)
                        else:
                            message = json.loads(raw)
                            if message.get("type") == "error":
                                message["value"] = "Camera playback failed. Check local live view and credentials."
                            await websocket.send_text(json.dumps(message))
                async def check_access():
                    while True:
                        await asyncio.sleep(3)
                        authorize_media(websocket, store, "cameras", camera_id)
                        current = manager.get(camera_id)
                        if current is None or not current["enabled"] or current != camera:
                            return
                jobs = [asyncio.create_task(fn()) for fn in
                        (browser_to_gateway, gateway_to_browser, check_access)]
                try:
                    done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                    for job in done:
                        job.result()
                finally:
                    for job in jobs:
                        job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)
        except (WebSocketDisconnect, websockets.WebSocketException, OSError, ValueError, HTTPException):
            pass
        finally:
            with contextlib.suppress(RuntimeError):
                await websocket.close()

    async def process_asset(asset_id):
        async with application.state.render_limit:
            worker = None
            try:
                worker = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "app.render_asset",
                    str(directory / "uploads" / asset_id),
                    str(directory / "pages" / asset_id),
                    cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(worker.communicate(), 120)
                result = json.loads(stdout)
                if worker.returncode or "error" in result:
                    raise ValueError(result.get("error", "File rendering failed"))
                def ready(state):
                    asset = find(state, "assets", asset_id)
                    asset.update(status="ready", error="", pages=[
                        p | {"url": f"/api/assets/{asset_id}/pages/{p['index']}"}
                        for p in result["pages"]
                    ])
                application.state.store.change(ready)
            except asyncio.CancelledError:
                if worker and worker.returncode is None:
                    worker.kill()
                    await worker.wait()
                raise
            except Exception:
                if worker and worker.returncode is None:
                    worker.kill()
                    await worker.wait()
                def failed(state):
                    find(state, "assets", asset_id).update(
                        status="error", error="Unable to render. Check file type, PDF password, "
                        "page count (200 max), and image dimensions (40 MP max)."
                    )
                with contextlib.suppress(HTTPException):
                    application.state.store.change(failed)

    @application.post("/api/assets")
    async def upload_asset(request: Request, file: UploadFile):
        require_admin(request)
        # Keep both the queue and storage bounded even with many simultaneous uploads.
        if len(application.state.render_tasks) >= 8:
            raise HTTPException(429, "Upload queue is full. Wait for processing to finish.")
        asset_id = new_id()
        uploads = directory / "uploads"
        uploads.mkdir(mode=0o700, exist_ok=True)
        path = uploads / asset_id
        total = 0
        try:
            with path.open("xb") as stream:
                while chunk := await file.read(65536):
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, "File exceeds the 50 MB upload limit")
                    stream.write(chunk)
            if total == 0:
                raise HTTPException(422, "File is empty")
            with path.open("rb") as stream:
                signature = stream.read(12)
            if not (signature.startswith((b"%PDF-", b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"))
                    or (signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")):
                raise HTTPException(422, "Only PDF, JPEG, PNG, and WebP content is supported")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        asset = {
            "id": asset_id, "name": Path(file.filename or "Advertisement").name[:200],
            "status": "processing", "error": "", "pages": [], "size": total,
        }
        application.state.store.change(lambda state: state["assets"].append(asset))
        task = asyncio.create_task(process_asset(asset_id))
        application.state.render_tasks.add(task)
        task.add_done_callback(application.state.render_tasks.discard)
        return asset

    @application.get("/api/assets/{asset_id}/pages/{page}")
    async def asset_page(asset_id: str, page: int, request: Request):
        store = request.app.state.store
        authorize_media(request, store, "assets", asset_id)
        asset = find(store.read(), "assets", asset_id)
        if asset["status"] != "ready" or page < 0 or page >= len(asset["pages"]):
            raise HTTPException(404, "Page not found")
        return FileResponse(directory / "pages" / asset["id"] / f"{page}.jpg",
                            media_type="image/jpeg")

    @application.delete("/api/assets/{asset_id}")
    async def delete_asset(asset_id: str, request: Request):
        require_admin(request)
        def remove(state):
            asset = find(state, "assets", asset_id)
            if asset["status"] == "processing":
                raise HTTPException(409, "Wait for this upload to finish processing")
            if any(item["asset_id"] == asset_id for p in state["playlists"] for item in p["items"]):
                raise HTTPException(409, "Remove this asset from playlists before deleting it")
            state["assets"].remove(asset)
            return {"ok": True}
        result = request.app.state.store.change(remove)
        (directory / "uploads" / asset_id).unlink(missing_ok=True)
        shutil.rmtree(directory / "pages" / asset_id, ignore_errors=True)
        return result

    @application.post("/api/playlists")
    async def add_playlist(body: Playlist, request: Request):
        require_admin(request)
        data = body.model_dump() | {"id": new_id()}
        def save(state):
            validate_playlist(state, data)
            state["playlists"].append(data)
            return data
        return request.app.state.store.change(save)

    @application.put("/api/playlists/{playlist_id}")
    async def edit_playlist(playlist_id: str, body: Playlist, request: Request):
        require_admin(request)
        def save(state):
            old = find(state, "playlists", playlist_id)
            data = body.model_dump() | {"id": playlist_id}
            validate_playlist(state, data)
            state["playlists"][state["playlists"].index(old)] = data
            return data
        return request.app.state.store.change(save)

    @application.delete("/api/playlists/{playlist_id}")
    async def delete_playlist(playlist_id: str, request: Request):
        require_admin(request)
        def remove(state):
            item = find(state, "playlists", playlist_id)
            if any(t["source_id"] == playlist_id for layout in state["layouts"]
                   for t in layout["tiles"] + (layout.get("published") or {}).get("tiles", [])):
                raise HTTPException(409, "Remove this playlist from draft and published layouts first")
            state["playlists"].remove(item)
            return {"ok": True}
        return request.app.state.store.change(remove)

    @application.post("/api/layouts")
    async def add_layout(body: Layout, request: Request):
        require_admin(request)
        data = body.model_dump() | {"id": new_id(), "published": None,
                                    "display_token": secrets.token_urlsafe(32)}
        def save(state):
            validate_layout(state, data)
            state["layouts"].append(data)
            return data
        return request.app.state.store.change(save)

    @application.put("/api/layouts/{layout_id}")
    async def edit_layout(layout_id: str, body: Layout, request: Request):
        require_admin(request)
        def save(state):
            layout = find(state, "layouts", layout_id)
            validate_layout(state, body.model_dump())
            layout.update(body.model_dump())
            return layout
        return request.app.state.store.change(save)

    @application.post("/api/layouts/{layout_id}/publish")
    async def publish(layout_id: str, request: Request):
        require_admin(request)
        def save(state):
            layout = find(state, "layouts", layout_id)
            validate_layout(state, layout)
            for tile in layout["tiles"]:
                if tile["type"] == "advertisement":
                    validate_playlist(state, find(state, "playlists", tile["source_id"]))
            layout["published"] = {key: layout[key] for key in
                                   ("id", "name", "width", "height", "background", "tiles")}
            return layout
        return request.app.state.store.change(save)

    @application.post("/api/layouts/{layout_id}/rotate-token")
    async def rotate(layout_id: str, request: Request):
        require_admin(request)
        def save(state):
            layout = find(state, "layouts", layout_id)
            layout["display_token"] = secrets.token_urlsafe(32)
            return layout
        return request.app.state.store.change(save)

    @application.delete("/api/layouts/{layout_id}")
    async def delete_layout(layout_id: str, request: Request):
        require_admin(request)
        def remove(state):
            state["layouts"].remove(find(state, "layouts", layout_id))
            return {"ok": True}
        return request.app.state.store.change(remove)

    @application.get("/api/display/{token}")
    async def display(token: str, request: Request):
        result = display_state(request.app.state.store, token)
        result["cameras"] = [camera | request.app.state.cameras.status.get(
            camera["id"], {"status": "idle", "error": ""}
        ) for camera in result["cameras"]]
        return result

    # Static path resolves only within this directory; no uploaded source files are served.
    application.mount("/static", StaticFiles(directory=static_dir), name="static")

    @application.get("/")
    @application.get("/display/{token}")
    async def index():
        path = static_dir / "index.html"
        if not path.exists():
            return Response("PrintWatch frontend is being prepared.", status_code=503)
        version = frontend_version.current()
        html = path.read_text().replace("__PRINTWATCH_BUILD__", version)
        html = html.replace('href="/static/styles.css"', f'href="/static/styles.css?v={version}"')
        html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={version}"')
        return Response(html, media_type="text/html")

    return application


app = create_app()
