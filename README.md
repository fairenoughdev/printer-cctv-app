# PrintWatch
A local printer-camera wall and digital-signage app. Arrange cameras and advertising zones on a canvas, rotate PDF pages/images, and open view-only layouts on browsers around your workshop.

## Start and stop
Requires Linux, Python 3.12+, and preferably [uv](https://docs.astral.sh/uv/). No Node, cloud account, or Docker is needed.

```bash
./launch.sh
./launch.sh --status
./stop.sh
```

The launcher prepares a project-local Python environment, checks readiness, opens `http://localhost:8080`, and leaves the server running in the background. It works from other directories, including paths containing spaces. Stop is idempotent and only signals verified app-owned processes.

Options:
```bash
./launch.sh --no-browser
./launch.sh --port 8090
./launch.sh --help
```

Dependencies need internet on first setup; normal operation uses local assets and LAN camera connections. Data is under `data/`; private logs/process records are under `.runtime/`. Keep both `data/printwatch.sqlite3` and `data/credentials.key` in a secure backup, together with uploads/pages. Losing the key means re-entering printer credentials. Never share the private key or upload your data directory.

## Camera gateway
Bambu video needs go2rtc. Snapshot-based cameras do not.

The launcher can download the official **go2rtc v1.9.15** binary into `.runtime/bin/`, checking a pinned SHA-256 digest before it is made executable:
```bash
./launch.sh --install-gateway
```

This is optional and explicit; a normal launch never silently downloads the gateway. An existing `go2rtc` on `PATH` is also supported. No system package installation or root permissions are required.

The gateway management API is bound to loopback port 1984; WebRTC media uses port 8555 TCP/UDP on the LAN. Do not expose the management API or forward these ports to the internet. Public STUN/TURN servers are disabled. The gateway config contains no saved printer credentials; the backend registers sources into gateway memory.

Set `PRINTWATCH_LAN_HOST` before launch if automatic interface selection is wrong. The display URL host can also be configured in Settings. Firewall/router rules must allow display devices to reach the app port and WebRTC media. MSE-over-WebSocket is the playback fallback when WebRTC cannot connect. Changing the advertised gateway address requires restarting the app/gateway.

FFmpeg is not installed automatically. It may be needed for cameras whose codec is not supported by a target browser or for JPEG snapshots of video sources. The application does not automatically create transcoding profiles; begin with modern desktop/Chromium-based display players and test your actual cameras.

## Configure your workshop
1. Open administration on **this computer** at `http://localhost:8080`.
2. Add cameras under **Cameras** and use **Test connection**.
3. Upload PDF, JPEG, PNG, or WebP advertisements in **Media library**. PDFs are rendered to images in an isolated bounded worker. Files are limited to 50 MB, PDFs to 200 pages, and images to 40 megapixels. Encrypted/corrupt documents are rejected.
4. In **Playlists**, add individual pages or all pages, reorder them, and set **Show for (seconds)**: the time until the next transition starts. Choose cut/fade/slide transitions. **Animation length (seconds)** controls the effect speed (for example, `0.6` seconds), separately from page timing. Slides can move **left, right, up, or down**; the next page enters from the opposite edge. Cut is instant and hides animation/direction controls. Existing playlists retain their timings and default to leftward slides.
5. In **Layout studio**, start with a preset or add individual camera/ad zones. Drag/resize tiles, use numeric controls, and save drafts.
6. Publish the layout. In **Displays**, open/copy its view-only link. Set the LAN display host first if links currently point to localhost.

Draft edits do not alter the published layout. Playlist changes are live: saving a playlist updates displays using it. Display clients poll for configuration updates every five seconds and preserve unchanged video players. Playlists are not frame-synchronized between devices.

### Automatic interface updates
Open pages check the frontend's content version every 15 seconds. After the same new version is observed twice (normally within 30 seconds), idle admin tabs and view-only displays reload themselves. HTML, scripts, and styles are served with explicit no-store headers and versioned asset URLs.

Unsaved playlist/layout changes, edited settings forms, open dialogs, and active saves/uploads defer automatic reload. A persistent **Update ready** notice offers **Refresh now** (with discard confirmation if needed). Saving your changes or closing the dialog lets the next update check reload safely. Network outages and ordinary playlist/layout publishing do not trigger interface reloads. Installing this feature requires one initial browser refresh; subsequent frontend updates are automatic. Browser fullscreen may need re-entering after a page reload unless the display uses kiosk mode.

### Bambu H2 / P2
Use the Bambu profile, printer IP/hostname, and LAN access code. The local camera endpoint is generated internally on port 322. Enable **LAN Mode Liveview / LAN Only Liveview / Local RTSP** on the printer (wording depends on firmware). This setting is separate from whether the printer uses cloud services.

These profiles target community-documented H2D/H2S and P2S camera behavior; exact variants/firmware need testing. Printer certificates are often self-signed: if necessary, explicitly uncheck **Verify TLS certificate** for that camera after confirming the device address. This weakens identity verification only for that specific source. Changing a printer's access code requires updating its saved camera.

### Snapmaker U1
The U1 profile uses the stock-firmware snapshot endpoint `/server/files/camera/monitor.jpg` and a server-side `camera.start_monitor` WebSocket keepalive. Put the printer's local authentication token in the camera password/token field.

To obtain the token on compatible firmware, open the printer's Fluidd interface in your browser, use Developer Tools → Network → WS, and inspect the local WebSocket connection's `token` parameter. Enter it only in the app's local configuration form. It grants access to your printer; do not paste it into chat or share screenshots containing it.

Stock-firmware compatibility is firmware-dependent; expect roughly snapshot-rate updates rather than smooth video. The app tests image arrival but cannot prove a frozen scene represents an active print. It warns when fetched image bytes have not changed for two minutes (a genuinely still scene can also trigger this warning).

If your U1 already runs custom firmware that exposes RTSP, use the generic RTSP profile with the endpoint provided by that firmware. PrintWatch never installs or changes printer firmware.

## LAN display security and HTTPS
Administration is intentionally **localhost-only**, with exact host/origin checks. LAN devices cannot edit configuration even if they know a display link. Each layout has a long, revocable read-only link, scoped to its cameras and advertisement assets. Regenerate a link to invalidate old URLs. Anyone holding a link can view that layout; treat it like a password.

Default HTTP is convenient for setup but **does not encrypt display tokens or video transport over HTTP/WebSocket**. Use only a trusted LAN in this mode. For HTTPS:
```bash
.venv/bin/python scripts/create-certificate.py --lan-host 192.168.1.10
./stop.sh
PRINTWATCH_TLS_CERT="$PWD/.runtime/tls/certificate.pem" \
PRINTWATCH_TLS_KEY="$PWD/.runtime/tls/private-key.pem" \
./launch.sh
```

Replace the IP with the server address. Trust the generated certificate on each display device, or provide a certificate already trusted by those devices. Some TV browsers cannot install/trust local certificates; use a suitable external browser player rather than weakening authentication. Keep the private key private. The helper will not overwrite existing certificate files; use `--output-dir` for a new set.

Remote administration is not provided. If needed, use a properly secured tunnel to localhost instead of disabling the checks. App access logs are disabled to avoid recording private display links; supervised logs redact URLs and credential-like output.

## Tests and development
```bash
uv sync --locked
uv run pytest -q
uv run ruff check app tests scripts
uv run python -m compileall -q app scripts
```

Optional real-browser smoke test (temporary database, no real printer required):
```bash
uv run --with playwright python -m playwright install chromium --only-shell
uv run --with playwright python tests/browser_smoke.py
```

The browser test creates a temporary server, exercises setup/upload/playlists/layout/publication/display and mobile navigation, and stops its server afterward. Test screenshots are stored under `.runtime/verification/`.

Run directly for development:
```bash
uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-proxy-headers --no-access-log
```

Use one app worker: camera sessions and upload tasks are process-local. Do not run a second instance against the same data directory.

## Optional Docker
`compose.yaml` is an alternative for **Linux**, using host networking to preserve localhost-only administration and LAN WebRTC routing. Requires Docker Compose 2.23.1+.
```bash
PRINTWATCH_LAN_HOST=192.168.1.10 docker compose up --build -d
docker compose down
```

Do not run native and container deployments on the same ports simultaneously. The container deployment uses a separate named data volume. Docker has not been runtime-validated on this machine because Docker is not installed. Native launch is the recommended, tested path.

## Practical limits
- Real printer connectivity and sustained 5–9-feed performance must be validated on your hardware. Simulated tests do not establish printer compatibility.
- Smart-TV decoding capacity varies. An external Chromium-capable mini-PC/player may handle multiple HD feeds better than a built-in TV browser.
- Live video stops if the server stops. Already cached advertisement slides can continue through a brief outage, but a fresh page load needs the server.
- Fullscreen requires a user gesture; unattended kiosk startup is a display/browser setting.
- No recording, printer control, cloud relay, AI monitoring, or scheduled advertising is included.
