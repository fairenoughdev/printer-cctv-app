#!/usr/bin/env python3
"""Linux-only lifecycle implementation; launch.sh/stop.sh serialize it with flock."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import webbrowser

from install_gateway import install
from service import redact

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
SERVICE = ROOT / "scripts" / "service.py"
# Never inherit a proxy for private health checks.
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def proc_identity(pid: int):
    try:
        path = Path(f"/proc/{pid}")
        stat = (path / "stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return {
            "start": fields[19],
            "uid": path.stat().st_uid,
            "cmd": (path / "cmdline").read_bytes().split(b"\0")[:-1],
            "pgid": int(fields[2]),
        }
    except (OSError, ValueError, IndexError):
        return None


def load_owned(role: str):
    path = RUNTIME / f"{role}.pid.json"
    try:
        saved = json.loads(path.read_text())
        pid = int(saved["pid"])
        current = proc_identity(pid)
        expected = [str(SERVICE).encode(), role.encode(), str(ROOT).encode()]
        cmd = current["cmd"] if current else []
        if (pid > 1 and current and current["start"] == saved["start"]
                and current["uid"] == os.getuid() == saved["uid"]
                and current["pgid"] == pid
                and cmd[1:4] == expected):
            return saved
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def stop_role(role: str):
    saved = load_owned(role)
    if saved:
        pid = saved["pid"]
        # Recheck immediately before each signal to avoid stale PID reuse.
        if load_owned(role):
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 12
        while load_owned(role) and time.monotonic() < deadline:
            time.sleep(0.1)
        if load_owned(role):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        print(f"Stopped owned {role} process group.")
    else:
        print(f"{role.capitalize()} is not running (no matching owned process).")
    (RUNTIME / f"{role}.pid.json").unlink(missing_ok=True)


def start_role(role: str, command: list[str], env: dict, **metadata):
    process = subprocess.Popen(
        [sys.executable, str(SERVICE), role, str(ROOT), *command],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
    )
    identity = proc_identity(process.pid)
    if not identity:
        raise RuntimeError(f"{role} exited before its process identity could be recorded.")
    record = {"pid": process.pid, "start": identity["start"], "uid": os.getuid(), **metadata}
    path = RUNTIME / f"{role}.pid.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    temporary.chmod(0o600)
    temporary.replace(path)
    return process


def healthy(url: str, app: bool = False) -> bool:
    try:
        opener = HTTP
        if app and url.startswith("https://127.0.0.1:"):
            # A self-signed certificate is expected; verification is bypassed ONLY
            # for a launcher-controlled local readiness check, never cameras.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        with opener.open(url, timeout=1) as response:
            if response.status != 200:
                return False
            if app:
                body = json.load(response)
                return isinstance(body, dict) and "status" in body and "version" in body
            return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def await_ready(role: str, url: str, seconds: int, app: bool = False):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not load_owned(role):
            return False
        if healthy(url, app):
            return True
        time.sleep(0.2)
    return False


def available(port: int):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Match uvicorn's listener behavior: TIME_WAIT must not block a restart.
        # This does not permit binding over an active listener.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            raise RuntimeError(f"Port {port} is already in use; choose another with --port.")


def sync_dependencies():
    if not (ROOT / "pyproject.toml").is_file():
        raise RuntimeError("pyproject.toml is missing; the application is not fully installed.")
    if uv := shutil.which("uv"):
        command = [uv, "sync", "--locked"] if (ROOT / "uv.lock").is_file() else [uv, "sync"]
    else:
        print("uv is unavailable; using the Python venv/pip fallback.")
        if not (ROOT / ".venv" / "bin" / "python").exists():
            result = subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise RuntimeError("Cannot create a venv. Install uv for your user and retry.")
        project = tomllib.loads((ROOT / "pyproject.toml").read_text()).get("project", {})
        if project.get("dynamic"):
            raise RuntimeError("Dynamic project metadata requires uv; install uv and retry.")
        command = [str(ROOT / ".venv/bin/python"), "-m", "pip", "install",
                   *project.get("dependencies", [])]
        if not project.get("dependencies"):
            raise RuntimeError("No static dependencies found; install uv and retry.")
    with (RUNTIME / "setup.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace")
        assert process.stdout is not None
        while line := process.stdout.readline(65536):
            log.write(redact(line))
        if process.wait():
            raise RuntimeError("Dependency setup failed. Review private, redacted .runtime/setup.log.")
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        raise RuntimeError("Dependency setup did not create .venv/bin/python.")
    return python


def lan_addresses():
    addresses = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = result[4][0]
            if not ipaddress.ip_address(addr).is_loopback:
                addresses.add(addr)
    except OSError:
        pass
    # Select the default LAN interface without sending any packets.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    return sorted(addresses)


def setup_gateway(args, env):
    url = env.setdefault("PRINTWATCH_GATEWAY_URL", "http://127.0.0.1:1984")
    if args.install_gateway:
        print("Installing pinned official go2rtc v1.9.15 with SHA-256 verification…")
        install(ROOT)
    if healthy(url.rstrip("/") + "/api/streams"):
        if load_owned("gateway"):
            print("Owned private camera gateway ready.")
        else:
            print("Using the existing camera gateway (not managed by stop.sh).")
        return
    if load_owned("gateway"):
        print("Warning: owned gateway is running but not healthy; see .runtime/gateway.log.")
        return
    if url not in ("http://127.0.0.1:1984", "http://localhost:1984"):
        print("Warning: configured external camera gateway is unavailable; continuing without it.")
        return
    bundled = RUNTIME / "bin" / "go2rtc"
    binary = str(bundled) if bundled.is_file() and os.access(bundled, os.X_OK) else shutil.which("go2rtc")
    if not binary:
        print("Warning: camera gateway unavailable. Snapshot cameras and administration remain usable.")
        print("Optional: ./launch.sh --install-gateway (verified official binary; no system install).")
        return
    # JSON is valid YAML. No camera addresses or credentials are persisted here.
    hosts = lan_addresses()
    lan_host = env.get("PRINTWATCH_LAN_HOST")
    if lan_host:
        if not all(c.isalnum() or c in ".-:" for c in lan_host):
            raise RuntimeError("PRINTWATCH_LAN_HOST must be a plain hostname or IP address.")
        hosts = [lan_host]
    config = {"api": {"listen": "127.0.0.1:1984"}, "rtsp": {"listen": ""},
              "webrtc": {"listen": ":8555", "ice_servers": [],
                         "candidates": [f"{host}:8555" for host in hosts]},
              "streams": {}, "log": {"level": "warn"}}
    path = RUNTIME / "go2rtc.yaml"
    path.write_text(json.dumps(config, indent=2))
    path.chmod(0o600)
    start_role("gateway", [binary, "-config", str(path)], env)
    if await_ready("gateway", url + "/api/streams", 10):
        print("Private camera gateway ready.")
    else:
        stop_role("gateway")
        print("Warning: camera gateway failed; see .runtime/gateway.log. Continuing without it.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        prog="./launch.sh" if "launch" in sys.argv[1:2] else "./stop.sh",
        description="Printer CCTV native lifecycle (Linux, Python 3.12+). Administration is localhost-only.")
    parser.add_argument("action", choices=["launch", "stop"])
    parser.add_argument("--no-browser", action="store_true", help="Do not open the local admin page.")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default 8080).")
    parser.add_argument("--status", action="store_true", help="Show owned process status; do not launch.")
    parser.add_argument("--install-gateway", action="store_true",
                        help="Download verified official go2rtc v1.9.15 into .runtime/bin.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not Path("/proc/self/stat").exists():
        raise RuntimeError("Safe native lifecycle requires Linux /proc.")
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    RUNTIME.chmod(0o700)
    if args.action == "stop":
        stop_role("app")
        stop_role("gateway")
        return
    if args.status:
        for role in ("app", "gateway"):
            saved = load_owned(role)
            print(f"{role}: running (owned PID {saved['pid']})" if saved else f"{role}: not running")
        return
    if saved := load_owned("app"):
        print(f"Printer CCTV is already running at {saved.get('scheme', 'http')}://localhost:{saved.get('port', 8080)}/")
        if args.install_gateway:
            setup_gateway(args, os.environ.copy())
        return
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12+ is required. Install it alongside uv and retry.")
    available(args.port)
    print("Preparing Python dependencies…")
    python = sync_dependencies()
    env = os.environ.copy()
    tls_cert = env.get("PRINTWATCH_TLS_CERT")
    tls_key = env.get("PRINTWATCH_TLS_KEY")
    if bool(tls_cert) != bool(tls_key):
        raise RuntimeError("Set both PRINTWATCH_TLS_CERT and PRINTWATCH_TLS_KEY to enable HTTPS.")
    tls_args = []
    if tls_cert and tls_key:
        cert_path = Path(tls_cert).expanduser().resolve()
        key_path = Path(tls_key).expanduser().resolve()
        if not cert_path.is_file() or not key_path.is_file():
            raise RuntimeError("Configured TLS certificate/key files do not exist.")
        if key_path.stat().st_mode & 0o077:
            raise RuntimeError("TLS private key must not be group/world readable; use chmod 600.")
        tls_args = ["--ssl-certfile", str(cert_path), "--ssl-keyfile", str(key_path)]
    scheme = "https" if tls_args else "http"
    env.setdefault("PRINTWATCH_DATA_DIR", str(ROOT / "data"))
    # A relative override must resolve against the project, not the invoking shell.
    data = Path(env["PRINTWATCH_DATA_DIR"]).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    env["PRINTWATCH_DATA_DIR"] = str(data)
    try:
        setup_gateway(args, env)
        start_role("app", [str(python), "-m", "uvicorn", "app.main:app",
                          "--host", "0.0.0.0", "--port", str(args.port),
                          "--no-proxy-headers", "--no-access-log", *tls_args],
                   env, port=args.port, scheme=scheme)
        if not await_ready("app", f"{scheme}://127.0.0.1:{args.port}/api/health", 40, app=True):
            raise RuntimeError("Application did not become ready; inspect private, redacted .runtime/app.log.")
    except BaseException:
        stop_role("app")
        stop_role("gateway")
        raise
    print(f"\nPrinter CCTV ready\nAdmin (this computer only): {scheme}://localhost:{args.port}/")
    for host in lan_addresses():
        print(f"LAN display host: {scheme}://{host}:{args.port}/ (use a published display-token URL)")
    if not tls_args:
        print("Warning: HTTP display tokens/video are unencrypted. Use a trusted LAN only, or configure TLS.")
    else:
        print("For a local self-signed certificate, trust the certificate on each display device.")
    print("Logs: .runtime/app.log and .runtime/gateway.log\nStop: ./stop.sh")
    if not args.no_browser:
        try:
            webbrowser.open(f"{scheme}://localhost:{args.port}/")
        except webbrowser.Error:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"Printer CCTV: {redact(str(error))}", file=sys.stderr)
        raise SystemExit(1)
