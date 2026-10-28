import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.cameras import CameraError, CameraManager
from app.store import Store


def configured(store, kind="bambu"):
    return {
        "id": "camera-fixture", "kind": kind, "host": "192.168.1.50", "enabled": True,
        "url": "", "username": "", "password_encrypted": store.encrypt("fixture-access"),
        "verify_tls": True,
    }


def test_gateway_registration_recovery_and_secret_boundaries(tmp_path):
    async def check():
        store = Store(tmp_path)
        manager = CameraManager(store)
        requests = []
        registered = {}

        def gateway(request):
            requests.append(request)
            if request.method == "PATCH":
                registered[request.url.params["name"]] = {}
                return httpx.Response(200)
            if request.method == "DELETE":
                registered.pop(request.url.params["src"], None)
                return httpx.Response(200)
            return httpx.Response(200, json=registered)

        await manager.client.aclose()
        manager.client = httpx.AsyncClient(
            base_url=manager.gateway, transport=httpx.MockTransport(gateway)
        )
        camera = configured(store)
        await manager.register(camera)
        assert len([r for r in requests if r.method == "PATCH"]) == 1
        assert "fixture-access" in requests[0].url.params["src"]
        assert manager.source(camera, credentials=False) == (
            "rtsps://192.168.1.50:322/streaming/live/1"
        )
        await manager.register(camera)
        assert len([r for r in requests if r.method == "PATCH"]) == 1
        registered.clear()
        await manager.register(camera)
        assert len([r for r in requests if r.method == "PATCH"]) == 2
        camera["verify_tls"] = False
        assert manager.source(camera).startswith("rtspx:")
        await manager.forget(camera["id"])
        assert not manager.registered
        await manager.close()
        store.close()

    asyncio.run(check())


def test_snapshot_session_shared_across_viewers(tmp_path):
    async def check():
        store = Store(tmp_path)
        manager = CameraManager(store)
        camera = configured(store, "snapshot")
        camera["url"] = "http://192.168.1.50/snapshot"
        frame = b"\xff\xd8\xfffixture\xff\xd9"
        manager.read_http_frame = AsyncMock(return_value=frame)
        first, second = await asyncio.gather(
            manager.snapshot(camera), manager.snapshot(camera)
        )
        assert first == second == frame
        assert manager.read_http_frame.await_count == 1
        assert len(manager.tasks) == 1
        manager.frames[camera["id"]] = (frame, time.monotonic() - 50, time.monotonic() - 50)
        manager.status[camera["id"]] = {"status": "offline", "error": "Offline"}
        with pytest.raises(CameraError):
            await manager.snapshot(camera)
        await manager.close()
        store.close()

    asyncio.run(check())


def test_u1_keepalive_payload_and_auth_not_in_source(tmp_path):
    async def check():
        store = Store(tmp_path)
        manager = CameraManager(store)
        camera = configured(store, "u1")
        connection = AsyncMock()
        connection.recv.return_value = json.dumps({"id": 1, "result": True})
        context = AsyncMock()
        context.__aenter__.return_value = connection
        with patch("app.cameras.websockets.connect", return_value=context) as connect:
            await manager.wake_u1(camera)
            assert "token=fixture-access" in connect.call_args.args[0]
            payload = json.loads(connection.send.call_args.args[0])
            assert payload["method"] == "camera.start_monitor"
            assert payload["params"] == {"domain": "lan", "interval": 0}
        assert "fixture-access" not in manager.source(camera, credentials=False)
        await manager.close()
        store.close()

    asyncio.run(check())


def test_http_auth_error_is_actionable_without_secret(tmp_path):
    async def check():
        store = Store(tmp_path)
        manager = CameraManager(store)
        camera = configured(store, "snapshot")
        camera["url"] = "http://192.168.1.50/frame"
        camera["username"] = "viewer"
        real_client = httpx.AsyncClient
        def client(**options):
            return real_client(**options, transport=httpx.MockTransport(
                lambda request: httpx.Response(401)
            ))
        with patch("app.cameras.httpx.AsyncClient", side_effect=client):
            with pytest.raises(CameraError, match="authentication was rejected") as caught:
                await manager.read_http_frame(camera)
            assert "fixture-access" not in str(caught.value)
        await manager.close()
        store.close()

    asyncio.run(check())
