"""Local-only camera access. Credentials stay in this process and the private gateway."""

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import socket
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
import websockets


class CameraError(Exception):
    pass


async def validate_destination(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https", "rtsp", "rtsps"):
        raise CameraError("Unsupported camera protocol")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or 80, type=socket.SOCK_STREAM
        )
    except (OSError, ValueError):
        raise CameraError("Camera hostname could not be resolved") from None
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0].split("%")[0])
        if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise CameraError("Camera must resolve to a private LAN address (not loopback/link-local)")


class CameraManager:
    def __init__(self, store):
        self.store = store
        self.gateway = os.getenv("PRINTWATCH_GATEWAY_URL", "http://127.0.0.1:1984").rstrip("/")
        parts = urlsplit(self.gateway)
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError("PRINTWATCH_GATEWAY_URL must point to a loopback HTTP gateway")
        self.client = httpx.AsyncClient(timeout=12, trust_env=False)
        self.tasks = {}
        self.frames = {}
        self.status = {}
        self.last_used = {}
        self.registered = {}
        self.locks = {}

    def get(self, camera_id):
        return next((c for c in self.store.read()["cameras"] if c["id"] == camera_id), None)

    def source(self, camera, credentials=True):
        host = camera.get("host", "")
        if camera["kind"] == "bambu":
            url = f"rtsps://{host}:322/streaming/live/1"
        elif camera["kind"] == "u1":
            url = camera.get("url") or f"http://{host}/server/files/camera/monitor.jpg"
        else:
            url = camera.get("url", "")
        if not url:
            raise CameraError("Set the camera host or stream URL")
        parts = urlsplit(url)
        if camera["kind"] in ("bambu", "rtsp") and parts.scheme not in ("rtsp", "rtsps"):
            raise CameraError("This camera profile requires an RTSP or RTSPS URL")
        if camera["kind"] in ("u1", "snapshot", "mjpeg") and parts.scheme not in ("http", "https"):
            raise CameraError("This camera profile requires an HTTP or HTTPS URL")
        if credentials:
            password = self.store.decrypt(camera.get("password_encrypted"))
            username = camera.get("username") or ("bblp" if camera["kind"] == "bambu" else "")
            if username and camera["kind"] in ("bambu", "rtsp"):
                auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
                url = urlunsplit(parts._replace(netloc=auth + parts.netloc))
            if parts.scheme == "rtsps" and not camera.get("verify_tls", True):
                url = "rtspx:" + url[len("rtsps:"):]
        return url

    async def gateway_available(self):
        try:
            response = await self.client.get(self.gateway + "/api/streams", timeout=2)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def register(self, camera):
        if not camera.get("enabled"):
            raise CameraError("Camera is disabled")
        await validate_destination(self.source(camera, credentials=False))
        source = self.source(camera)
        fingerprint = hashlib.sha256(source.encode()).hexdigest()
        async with self.locks.setdefault(camera["id"], asyncio.Lock()):
            if self.registered.get(camera["id"]) == fingerprint:
                try:
                    streams = await self.client.get(self.gateway + "/api/streams", timeout=2)
                    if streams.status_code == 200 and camera["id"] in streams.json():
                        return
                except (httpx.HTTPError, ValueError):
                    pass
            try:
                # PUT persists source URLs to go2rtc.yaml; PATCH creates/updates only in memory.
                response = await self.client.patch(
                    self.gateway + "/api/streams",
                    params={"name": camera["id"], "src": source},
                )
                if response.status_code != 200:
                    raise CameraError("Gateway rejected this camera configuration")
                self.registered[camera["id"]] = fingerprint
            except httpx.HTTPError:
                raise CameraError("Video gateway is unavailable. Launch with --install-gateway.") from None

    async def forget(self, camera_id):
        task = self.tasks.pop(camera_id, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.frames.pop(camera_id, None)
        self.status.pop(camera_id, None)
        self.last_used.pop(camera_id, None)
        self.registered.pop(camera_id, None)
        try:
            await self.client.delete(
                self.gateway + "/api/streams", params={"src": camera_id}, timeout=2
            )
        except httpx.HTTPError:
            pass

    async def wake_u1(self, camera):
        source = urlsplit(self.source(camera, credentials=False))
        token = self.store.decrypt(camera.get("password_encrypted"))
        scheme = "wss" if source.scheme == "https" else "ws"
        uri = urlunsplit((scheme, source.netloc, "/websocket", urlencode({"token": token}), ""))
        options = {"open_timeout": 5, "max_size": 65536, "proxy": None}
        if scheme == "wss" and not camera.get("verify_tls", True):
            import ssl
            options["ssl"] = ssl._create_unverified_context()
        async with websockets.connect(uri, **options) as websocket:
            await websocket.send(json.dumps({
                "id": 1, "jsonrpc": "2.0", "method": "camera.start_monitor",
                "params": {"domain": "lan", "interval": 0},
            }))
            # Some firmwares do not immediately acknowledge; the next snapshot is the proof.
            with contextlib.suppress(TimeoutError):
                result = await asyncio.wait_for(websocket.recv(), 2)
                if "error" in json.loads(result):
                    raise CameraError("U1 rejected camera keepalive; verify its token and firmware")

    async def read_http_frame(self, camera):
        url = self.source(camera, credentials=False)
        auth = None
        if camera.get("username") and camera["kind"] != "u1":
            auth = httpx.BasicAuth(
                camera["username"], self.store.decrypt(camera.get("password_encrypted"))
            )
        # Separate clients keep an explicit per-camera TLS trust setting.
        async with httpx.AsyncClient(
            verify=camera.get("verify_tls", True), timeout=10, trust_env=False
        ) as client:
            async with client.stream(
                "GET", url, auth=auth, headers={"Cache-Control": "no-cache"}
            ) as response:
                if response.status_code in (401, 403):
                    raise CameraError("Camera authentication was rejected")
                if response.status_code != 200:
                    raise CameraError("Camera did not return an image; check its endpoint and firmware")
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > 12 * 1024 * 1024:
                        raise CameraError("Camera frame exceeded 12 MB")
                    if camera["kind"] == "mjpeg":
                        start = buffer.find(b"\xff\xd8")
                        end = buffer.find(b"\xff\xd9", max(start, 0))
                        if start >= 0 and end > start:
                            return bytes(buffer[start:end + 2])
                if bytes(buffer[:3]) == b"\xff\xd8\xff":
                    return bytes(buffer)
                if bytes(buffer[:8]) == b"\x89PNG\r\n\x1a\n":
                    return bytes(buffer)
                raise CameraError("Camera returned no supported JPEG/PNG image")

    async def poll(self, camera):
        camera_id = camera["id"]
        last_wake = 0
        failures = 0
        while time.monotonic() - self.last_used.get(camera_id, 0) < 35:
            try:
                if camera["kind"] == "u1" and time.monotonic() - last_wake >= 10:
                    await self.wake_u1(camera)
                    last_wake = time.monotonic()
                frame = await self.read_http_frame(camera)
                now = time.monotonic()
                previous = self.frames.get(camera_id)
                changed = now if not previous or previous[0] != frame else previous[2]
                self.frames[camera_id] = (frame, now, changed)
                stale = now - changed > 120
                self.status[camera_id] = {
                    "status": "stale" if stale else "online",
                    "error": "Image unchanged for 2 minutes; camera may be idle" if stale else "",
                }
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                self.status[camera_id] = {
                    "status": "offline",
                    "error": "Cannot fetch a fresh camera image. Check address, token, and firmware.",
                }
            await asyncio.sleep(min(2 ** failures, 15))

    async def snapshot(self, camera):
        if not camera.get("enabled"):
            raise CameraError("Camera is disabled")
        camera_id = camera["id"]
        if camera["kind"] in ("bambu", "rtsp"):
            await self.register(camera)
            try:
                response = await self.client.get(
                    self.gateway + "/api/frame.jpeg", params={"src": camera_id}
                )
                if response.status_code != 200 or not response.content.startswith(b"\xff\xd8"):
                    raise CameraError("No JPEG snapshot available for this stream; use live video")
                return response.content
            except httpx.HTTPError:
                raise CameraError("Gateway could not obtain a camera image") from None
        await validate_destination(self.source(camera, credentials=False))
        self.last_used[camera_id] = time.monotonic()
        if camera_id not in self.tasks or self.tasks[camera_id].done():
            self.tasks[camera_id] = asyncio.create_task(self.poll(camera))
        for _ in range(110):
            cached = self.frames.get(camera_id)
            if cached and time.monotonic() - cached[1] < 20:
                return cached[0]
            if self.status.get(camera_id, {}).get("status") == "offline":
                break
            await asyncio.sleep(0.1)
        raise CameraError(self.status.get(camera_id, {}).get("error", "Waiting for a camera image"))

    async def test(self, camera):
        if camera["kind"] not in ("bambu", "rtsp"):
            await self.snapshot(camera)
            return "Camera image received. U1 stock firmware may update at approximately 1 fps."
        await self.register(camera)
        # Request a bounded MP4 initialization segment to verify actual media, not just registration.
        try:
            async with self.client.stream(
                "GET", self.gateway + "/api/stream.mp4", params={"src": camera["id"]},
                timeout=15,
            ) as response:
                if response.status_code != 200:
                    raise CameraError("No video received. Check LAN live view, access code, and TLS trust.")
                async for chunk in response.aiter_bytes():
                    if chunk:
                        self.status[camera["id"]] = {"status": "online", "error": ""}
                        return "Video data received through the gateway. Verify playback on your display."
            raise CameraError("Camera connected but did not send video")
        except httpx.HTTPError:
            self.registered.pop(camera["id"], None)
            raise CameraError("Camera timed out. Check local live view, access code, and TLS trust.") from None

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.client.aclose()
