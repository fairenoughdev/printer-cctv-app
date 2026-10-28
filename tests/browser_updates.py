"""Run with `uv run --with playwright python tests/browser_updates.py`."""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def run():
    with tempfile.TemporaryDirectory(prefix="printwatch-updates-") as temporary:
        temporary = Path(temporary)
        frontend = temporary / "static"
        shutil.copytree(ROOT / "static", frontend)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,uvicorn; from app.main import create_app; "
             "uvicorn.run(create_app(sys.argv[1],frontend_dir=sys.argv[2]), "
             "host='127.0.0.1',port=int(sys.argv[3]),access_log=False,proxy_headers=False)",
             str(temporary / "data"), str(frontend), str(port)],
            cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with httpx.Client(base_url=origin, trust_env=False) as api:
                for _ in range(100):
                    try:
                        if api.get("/api/frontend-version", timeout=1).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError("Temporary update server did not start")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def opened(path="/"):
                        page.goto(origin + path)
                        page.wait_for_timeout(200)

                    def build():
                        return page.locator('meta[name="printwatch-build"]').get_attribute("content")

                    def update(label):
                        with (frontend / "styles.css").open("a") as stream:
                            stream.write(f"\n/* isolated test update: {label} */\n")
                        return api.get("/api/frontend-version").json()["version"]

                    def check():
                        page.evaluate("window.dispatchEvent(new Event('online'))")
                        page.wait_for_timeout(200)

                    def expect_reload(expected):
                        with page.expect_navigation():
                            page.evaluate("window.dispatchEvent(new Event('online'))")
                        page.wait_for_timeout(200)
                        assert build() == expected

                    opened()
                    expect(page.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
                    first = build()
                    latest = update("clean admin")
                    check()
                    assert build() == first
                    expect_reload(latest)
                    assert f"?v={latest}" in page.locator('script[type="module"]').get_attribute("src")

                    page.locator("nav").get_by_role("button", name="Playlists", exact=True).click()
                    page.get_by_label("Playlist name", exact=True).fill("Keep this draft")
                    previous = build()
                    latest = update("dirty playlist")
                    check()
                    check()
                    expect(page.get_by_text("Update ready", exact=True)).to_be_visible()
                    expect(page.get_by_label("Playlist name", exact=True)).to_have_value("Keep this draft")
                    assert build() == previous
                    page.once("dialog", lambda dialog: dialog.dismiss())
                    page.get_by_role("button", name="Refresh now", exact=True).click()
                    assert build() == previous
                    page.get_by_role("button", name="Save playlist", exact=True).click()
                    expect(page.get_by_text("Playlist saved", exact=True)).to_be_visible()
                    expect_reload(latest)
                    expect(page.get_by_label("Playlist name", exact=True)).to_have_value("Keep this draft")

                    page.locator("nav").get_by_role("button", name="Settings", exact=True).click()
                    page.get_by_label("Workshop name", exact=True).fill("Unsaved settings")
                    previous = build()
                    update("dirty settings form")
                    check()
                    check()
                    assert build() == previous
                    expect(page.get_by_text("Update ready", exact=True)).to_be_visible()
                    expect(page.get_by_label("Workshop name", exact=True)).to_have_value("Unsaved settings")
                    # Never save this form; re-open the page with its current version.
                    page.once("dialog", lambda dialog: dialog.accept())
                    opened()
                    page.locator("nav").get_by_role("button", name="Cameras", exact=True).click()
                    page.get_by_role("button", name="+ Add camera").first.click()
                    previous = build()
                    latest = update("open camera dialog")
                    check()
                    check()
                    assert build() == previous
                    expect(page.get_by_role("dialog")).to_be_visible()
                    page.get_by_role("button", name="Close dialog", exact=True).click()
                    expect_reload(latest)

                    layout = api.get("/api/state").json()["layouts"][0]
                    api.post(f"/api/layouts/{layout['id']}/publish")
                    opened(f"/display/{layout['display_token']}")
                    latest = update("display")
                    check()
                    expect_reload(latest)
                    assert "/display/" in page.url

                    previous = build()
                    update("server temporarily unavailable")
                    page.route("**/api/frontend-version", lambda route: route.fulfill(
                        status=503, json={"detail": "Restarting"}
                    ))
                    check()
                    check()
                    assert build() == previous
                    assert not errors, errors
                    browser.close()
                    print("Browser update checks passed: clean admin/display reload, two-check "
                          "stability, dirty playlist/settings/dialog protection, discard cancel, "
                          "save-then-refresh, and restart failure handling.")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    run()
