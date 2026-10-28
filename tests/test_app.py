import asyncio
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.cameras import CameraError, validate_destination
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://localhost",
                    client=("127.0.0.1", 12345)) as test:
        yield test


def camera(client, **overrides):
    data = {"name": "Bambu H2D", "kind": "bambu", "host": "192.168.1.50",
            "password": "test-access-code"}
    data.update(overrides)
    response = client.post("/api/cameras", json=data)
    assert response.status_code == 200, response.text
    return response.json()


def upload_ready(client, contents=None, name="poster.png"):
    if contents is None:
        buffer = io.BytesIO()
        Image.new("RGB", (120, 80), "#d9fb69").save(buffer, "PNG")
        contents = buffer.getvalue()
    response = client.post("/api/assets", files={"file": (name, contents)})
    assert response.status_code == 200, response.text
    asset_id = response.json()["id"]
    for _ in range(100):
        asset = next(a for a in client.get("/api/state").json()["assets"] if a["id"] == asset_id)
        if asset["status"] != "processing":
            return asset
        time.sleep(0.05)
    pytest.fail("Upload did not finish processing")


def publish(client, cameras=(), playlist=None):
    layout = client.get("/api/state").json()["layouts"][0]
    tiles = [
        {"id": f"tile-{index}", "type": "camera", "source_id": c["id"],
         "x": index * 400, "y": 0, "width": 400, "height": 300}
        for index, c in enumerate(cameras)
    ]
    if playlist:
        tiles.append({"id": "advertisement", "type": "advertisement",
                      "source_id": playlist["id"], "x": 0, "y": 400, "width": 500, "height": 500})
    layout["tiles"] = tiles
    assert client.put(f"/api/layouts/{layout['id']}", json=layout).status_code == 200
    response = client.post(f"/api/layouts/{layout['id']}/publish")
    assert response.status_code == 200, response.text
    return response.json()


def test_empty_initial_state(client):
    state = client.get("/api/state").json()
    assert state["cameras"] == []
    assert state["assets"] == []
    assert state["layouts"][0]["published"] is None
    assert client.get("/api/health").json()["status"] == "ok"


def test_secrets_encrypted_and_redacted(client):
    result = camera(client)
    assert result["has_password"]
    assert "password" not in result
    state = client.get("/api/state").json()
    assert "test-access-code" not in json.dumps(state)
    raw = client.app.state.store.read()["cameras"][0]
    assert raw["password_encrypted"] != "test-access-code"
    assert client.app.state.store.decrypt(raw["password_encrypted"]) == "test-access-code"
    response = client.put(f"/api/cameras/{result['id']}", json={
        "name": "Renamed", "kind": "bambu", "host": "192.168.1.50",
    })
    assert response.json()["has_password"]


def test_management_requires_localhost(client):
    assert client.get("/api/state", headers={"host": "malicious.example"}).status_code == 403
    assert client.get("/api/state", headers={"origin": "https://malicious.example"}).status_code == 403
    with TestClient(create_app(client.app.state.store.directory), base_url="http://localhost",
                    client=("192.168.1.99", 1234)) as remote:
        assert remote.get("/api/state").status_code == 403
        assert remote.post("/api/cameras", json={}).status_code == 403


@pytest.mark.parametrize("url", [
    "exec:some-command", "file:///etc/passwd", "rtsp://user:password@192.168.1.2/live",
    "http://192.168.1.2/frame#exec:bad",
])
def test_reject_unsafe_urls(client, url):
    response = client.post("/api/cameras", json={"name": "Unsafe", "kind": "rtsp", "url": url})
    assert response.status_code == 422


def test_validation_does_not_echo_secrets(client):
    response = client.post("/api/cameras", json={
        "name": "Camera", "password": "VERYSECRET" * 1000,
    })
    assert response.status_code == 422
    assert "VERYSECRET" not in response.text


def test_publication_snapshot_and_display_scoping(client):
    selected = camera(client)
    unrelated = camera(client, name="Not on display", host="192.168.1.51")
    layout = publish(client, [selected])
    token = layout["display_token"]
    response = client.get(f"/api/display/{token}")
    assert response.status_code == 200
    data = response.json()
    assert len(data["cameras"]) == 1
    assert "host" not in data["cameras"][0]
    assert "url" not in data["cameras"][0]
    assert "password_encrypted" not in json.dumps(data)
    assert data["layout"]["tiles"][0]["source_id"] == selected["id"]
    layout["tiles"] = []
    client.put(f"/api/layouts/{layout['id']}", json=layout)
    assert len(client.get(f"/api/display/{token}").json()["layout"]["tiles"]) == 1
    with TestClient(create_app(client.app.state.store.directory), base_url="http://printer.local",
                    client=("192.168.1.99", 1234)) as remote:
        assert remote.get(f"/api/cameras/{unrelated['id']}/snapshot?token={token}").status_code == 403
    client.post(f"/api/layouts/{layout['id']}/rotate-token")
    assert client.get(f"/api/display/{token}").status_code == 403


def test_invalid_layout_bounds_and_references(client):
    layout = client.get("/api/state").json()["layouts"][0]
    configured = camera(client)
    for source, x in [("does-not-exist", 0), (configured["id"], 1900)]:
        layout["tiles"] = [{"id": "a", "type": "camera", "source_id": source,
                            "x": x, "width": 500, "height": 500}]
        assert client.put(f"/api/layouts/{layout['id']}", json=layout).status_code in (404, 422)


def test_image_upload_playlist_and_reference_protection(client):
    asset = upload_ready(client)
    assert asset["status"] == "ready", asset
    page = client.get(asset["pages"][0]["url"])
    assert page.status_code == 200
    assert page.content.startswith(b"\xff\xd8")
    playlist = client.post("/api/playlists", json={
        "name": "Offers", "items": [{"asset_id": asset["id"], "page": 0, "duration": 5}],
        "transition": "slide", "transition_ms": 400,
    }).json()
    assert playlist["items"][0]["duration"] == 5
    assert client.delete(f"/api/assets/{asset['id']}").status_code == 409
    layout = publish(client, playlist=playlist)
    token = layout["display_token"]
    assert len(client.get(f"/api/display/{token}").json()["assets"]) == 1
    assert client.delete(f"/api/playlists/{playlist['id']}").status_code == 409


def test_pdf_upload_multiple_pages(client):
    first = Image.new("RGB", (100, 160), "white")
    second = Image.new("RGB", (160, 100), "green")
    buffer = io.BytesIO()
    first.save(buffer, "PDF", save_all=True, append_images=[second])
    asset = upload_ready(client, buffer.getvalue(), "multipage.pdf")
    assert asset["status"] == "ready", asset
    assert len(asset["pages"]) == 2
    assert asset["pages"][0]["height"] > asset["pages"][0]["width"]
    assert asset["pages"][1]["width"] > asset["pages"][1]["height"]


def test_reject_unsupported_file(client):
    response = client.post("/api/assets", files={"file": ("poster.pdf", b"<script>bad</script>")})
    assert response.status_code == 422


def test_corrupt_pdf_reports_failure(client):
    asset = upload_ready(client, b"%PDF-invalid", "corrupt.pdf")
    assert asset["status"] == "error"


def test_playlist_page_and_timing_validation(client):
    asset = upload_ready(client)
    response = client.post("/api/playlists", json={
        "name": "Bad", "items": [{"asset_id": asset["id"], "page": 100}],
    })
    assert response.status_code == 422
    response = client.post("/api/playlists", json={
        "name": "Bad timing", "transition_ms": 2000,
        "items": [{"asset_id": asset["id"], "duration": 1}],
    })
    assert response.status_code == 422


@pytest.mark.parametrize("direction", ["left", "right", "up", "down"])
def test_slide_direction_persists_and_reaches_displays(client, direction):
    response = client.post("/api/playlists", json={
        "name": "Directional slides", "transition": "slide", "transition_ms": 750,
        "slide_direction": direction, "items": [],
    })
    assert response.status_code == 200, response.text
    playlist = response.json()
    layout = publish(client, playlist=playlist)
    displayed = client.get(f"/api/display/{layout['display_token']}").json()
    assert displayed["playlists"][0]["slide_direction"] == direction
    assert displayed["playlists"][0]["transition_ms"] == 750


def test_legacy_playlist_keeps_its_timing_and_defaults_to_left(client):
    playlist = client.get("/api/state").json()["playlists"][0]
    response = client.put(f"/api/playlists/{playlist['id']}", json={
        "name": playlist["name"], "transition": "slide", "transition_ms": 1250, "items": [],
    })
    assert response.status_code == 200
    assert response.json()["slide_direction"] == "left"
    assert response.json()["transition_ms"] == 1250


def test_invalid_slide_direction_rejected(client):
    response = client.post("/api/playlists", json={
        "name": "Invalid direction", "slide_direction": "diagonal",
    })
    assert response.status_code == 422


def test_cut_ignores_saved_animation_length(client):
    asset = upload_ready(client)
    response = client.post("/api/playlists", json={
        "name": "Instant cuts", "transition": "cut", "transition_ms": 3000,
        "items": [{"asset_id": asset["id"], "duration": 1}],
    })
    assert response.status_code == 200


def test_data_survives_restart(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://localhost",
                    client=("127.0.0.1", 1234)) as first:
        configured = camera(first)
    with TestClient(create_app(tmp_path), base_url="http://localhost",
                    client=("127.0.0.1", 1234)) as second:
        assert second.get("/api/state").json()["cameras"][0]["id"] == configured["id"]


def test_private_destination_restrictions():
    for host in ("127.0.0.1", "169.254.169.254", "8.8.8.8", "0.0.0.0", "[::1]"):
        with pytest.raises(CameraError):
            asyncio.run(validate_destination(f"http://{host}/snapshot"))
    asyncio.run(validate_destination("http://192.168.1.50/snapshot"))
