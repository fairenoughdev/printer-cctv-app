import re

from fastapi.testclient import TestClient

from app.frontend import FrontendVersion
from app.main import create_app


def test_content_fingerprint_changes_only_with_frontend_content(tmp_path):
    file = tmp_path / "app.js"
    file.write_text("const version = 1;")
    versions = FrontendVersion(tmp_path)
    first = versions.current()
    assert re.fullmatch(r"[a-f0-9]{64}", first)
    file.touch()
    assert versions.current() == first
    (tmp_path / "upload.jpg").write_bytes(b"image")
    assert versions.current() == first
    file.write_text("const version = 2;")
    second = versions.current()
    assert second != first
    (tmp_path / "styles.css").write_text("body { color: blue; }")
    assert versions.current() != second


def test_versioned_html_and_non_stale_headers(tmp_path):
    frontend = tmp_path / "static"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<meta name="printwatch-build" content="__PRINTWATCH_BUILD__">'
        '<link href="/static/styles.css"><script src="/static/app.js"></script>'
    )
    (frontend / "styles.css").write_text("body { color: blue; }")
    (frontend / "app.js").write_text("const test = 1;")
    application = create_app(tmp_path / "data", frontend_dir=frontend)
    with TestClient(application, base_url="http://localhost",
                    client=("127.0.0.1", 1234)) as client:
        first = client.get("/api/frontend-version").json()["version"]
        for path in ("/", "/display/example", "/static/styles.css", "/static/app.js",
                     "/api/frontend-version"):
            assert client.get(path).headers["cache-control"] == "no-store"
        html = client.get("/").text
        assert "__PRINTWATCH_BUILD__" not in html
        assert f'content="{first}"' in html
        assert f"/static/app.js?v={first}" in html
        assert f"/static/styles.css?v={first}" in html
        (frontend / "app.js").write_text("const test = 2;")
        second = client.get("/api/frontend-version").json()["version"]
        assert second != first
        assert f"/static/app.js?v={second}" in client.get("/").text


def test_display_clients_can_check_version_without_admin_access(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://printer.local",
                    client=("192.168.1.99", 1234)) as remote:
        response = remote.get("/api/frontend-version")
        assert response.status_code == 200
        assert set(response.json()) == {"version"}
        assert remote.get("/api/state").status_code == 403
