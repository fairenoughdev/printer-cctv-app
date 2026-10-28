"""Run with `uv run --with playwright python tests/browser_smoke.py`.

Uses an isolated temporary database and ephemeral port; never changes shop configuration.
"""

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def run():
    with tempfile.TemporaryDirectory(prefix="printwatch-browser-") as temporary:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log", "--no-proxy-headers"],
            cwd=ROOT, env=os.environ | {"PRINTWATCH_DATA_DIR": temporary},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with httpx.Client(base_url=origin, trust_env=False) as api:
                for _ in range(100):
                    try:
                        if api.get("/api/health", timeout=1).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError("Test server did not start")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    context = browser.new_context(viewport={"width": 1500, "height": 1000})
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin)
                    expect(page.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
                    evidence = ROOT / ".runtime" / "verification"
                    evidence.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(evidence / "overview.png"), full_page=True)
                    page.locator("nav").get_by_role("button", name="Cameras").click()
                    page.get_by_role("button", name="+ Add camera").first.click()
                    page.get_by_label("Camera name", exact=True).fill("Test H2D")
                    page.get_by_label("Host / IP address", exact=True).fill("192.168.250.250")
                    page.get_by_label("Printer model", exact=True).fill("H2D")
                    page.locator('form input[type="password"]').fill("fixture-only")
                    page.locator("form").get_by_role("button", name="Add camera", exact=True).click()
                    expect(page.get_by_role("heading", name="Test H2D", exact=True)).to_be_visible()
                    page.locator("nav").get_by_role("button", name="Media library").click()
                    buffer = io.BytesIO()
                    first = Image.new("RGB", (600, 800), "#e8efcb")
                    second = Image.new("RGB", (600, 800), "#ffa966")
                    first.save(buffer, "PDF", save_all=True, append_images=[second])
                    page.locator('input[type="file"]').set_input_files({
                        "name": "Workshop offers.pdf", "mimeType": "application/pdf",
                        "buffer": buffer.getvalue(),
                    })
                    expect(page.get_by_text("2 display-ready pages")).to_be_visible(timeout=15000)
                    page.locator("nav").get_by_role("button", name="Playlists").click()
                    page.locator(".playlist-add select").select_option(
                        api.get("/api/state").json()["assets"][0]["id"]
                    )
                    page.get_by_role("button", name="Add all pages", exact=True).click()
                    expect(page.locator(".playlist-row")).to_have_count(2)
                    expect(page.get_by_label("Animation length (seconds)", exact=True)).to_have_value("0.6")
                    expect(page.get_by_label("Slide direction", exact=True)).to_be_hidden()
                    page.get_by_label("Transition", exact=True).select_option("slide")
                    page.get_by_label("Slide direction", exact=True).select_option("up")
                    animation = page.get_by_label("Animation length (seconds)", exact=True)
                    animation.click()
                    animation.press("Control+a")
                    animation.press("Backspace")
                    expect(animation).to_have_value("")
                    animation.press_sequentially("invalid")
                    page.get_by_role("button", name="Save playlist", exact=True).click()
                    expect(page.get_by_text("Enter seconds between 0 and 3.", exact=True)).to_be_visible()
                    animation.fill("")
                    animation.press_sequentially("0.25")
                    for seconds in page.get_by_label("Show for (seconds)", exact=True).all():
                        seconds.click()
                        seconds.press("Control+a")
                        seconds.press("Backspace")
                        expect(seconds).to_have_value("")
                        seconds.press_sequentially("12.5")
                        expect(seconds).to_have_value("12.5")
                        seconds.fill("2")
                    assert page.locator(".playlist-row[draggable=true]").count() == 0
                    expect(page.locator(".drag-grip[draggable=true]")).to_have_count(2)
                    page.get_by_role("button", name="Save playlist", exact=True).click()
                    expect(page.get_by_text("Playlist saved", exact=True)).to_be_visible()
                    saved_playlist = api.get("/api/state").json()["playlists"][0]
                    assert saved_playlist["transition_ms"] == 250
                    assert saved_playlist["slide_direction"] == "up"
                    expect(page.get_by_label("Animation length (seconds)", exact=True)).to_have_value("0.25")
                    expect(page.get_by_label("Slide direction", exact=True)).to_have_value("up")
                    page.screenshot(path=str(evidence / "playlist-controls.png"), full_page=True)
                    page.get_by_label("Transition", exact=True).select_option("cut")
                    expect(page.get_by_label("Animation length (seconds)", exact=True)).to_be_hidden()
                    expect(page.get_by_label("Slide direction", exact=True)).to_be_hidden()
                    page.get_by_label("Transition", exact=True).select_option("slide")
                    page.get_by_role("button", name="Save playlist", exact=True).click()
                    page.locator("nav").get_by_role("button", name="Layout studio").click()
                    page.locator(".editor-toolbar select").nth(1).select_option("showcase")
                    expect(page.locator(".composition-tile")).to_have_count(2)
                    page.get_by_role("button", name="Save draft", exact=True).click()
                    expect(page.get_by_text("Layout draft saved", exact=True)).to_be_visible()
                    page.wait_for_timeout(800)
                    page.screenshot(path=str(evidence / "layout-studio.png"), full_page=True)
                    page.get_by_role("button", name="Publish layout", exact=False).click()
                    expect(page.get_by_text(
                        "Layout published. Connected displays will update shortly.", exact=True
                    )).to_be_visible()
                    page.locator("nav").get_by_role("button", name="Displays").click()
                    display_url = page.get_by_label("View-only display link").first.input_value()
                    display = context.new_page()
                    display.on("pageerror", lambda error: errors.append(str(error)))
                    display.goto(display_url)
                    expect(display.locator(".composition-tile")).to_have_count(2)
                    expect(display.locator(".ad-page")).to_have_count(1)
                    expect(display.locator(".ad-page")).to_be_visible()
                    assert display.locator(".ad-page").evaluate("(img) => img.complete && img.naturalWidth > 0")
                    expect(display.locator(".ad-player")).to_have_class("ad-player transition-slide slide-up")
                    # Exercise rendering beyond one refresh without recreating unchanged players.
                    original_player = display.locator(".ad-player").element_handle()
                    display.wait_for_timeout(5500)
                    assert original_player.evaluate("(node) => node.isConnected")
                    # Confirm the slide actually rotates, not just that an image is mounted.
                    expect(display.locator(".ad-page:not(.leaving)")).to_have_attribute(
                        "alt", "Workshop offers.pdf, page 2", timeout=6000
                    )
                    # Check all movement vectors with the actual renderer and stylesheet.
                    for direction, axis, sign in [
                        ("left", 0, 1), ("right", 0, -1), ("up", 1, 1), ("down", 1, -1),
                    ]:
                        saved_playlist["slide_direction"] = direction
                        response = api.put(f"/api/playlists/{saved_playlist['id']}", json=saved_playlist)
                        assert response.status_code == 200
                        display.reload()
                        expect(display.locator(".ad-page")).to_have_count(1)
                        vector = display.locator(".ad-page").evaluate("""image => {
                            image.style.transition = 'none';
                            image.classList.add('entering');
                            const matrix = new DOMMatrixReadOnly(getComputedStyle(image).transform);
                            image.classList.remove('entering');
                            return [matrix.m41, matrix.m42];
                        }""")
                        assert vector[axis] * sign > 0, (direction, vector)
                        assert vector[1 - axis] == 0, (direction, vector)
                    # The display API always validates its token, including on the host.
                    published = api.get("/api/state").json()["layouts"][0]
                    api.post(f"/api/layouts/{published['id']}/rotate-token")
                    expect(display.locator(".composition-tile")).to_have_count(0, timeout=8000)
                    # Mobile navigation remains available and does not overflow the viewport.
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.locator("nav").get_by_role("button", name="Cameras").click()
                    expect(page.get_by_role("heading", name="Cameras", exact=True)).to_be_visible()
                    assert not errors, errors
                    browser.close()
                    print("Browser smoke passed: camera form, PDF upload, playlist, layout, "
                          "publish, display rendering, polling stability, mobile navigation.")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    run()
