#!/usr/bin/env python3
"""Explicit, user-requested installation of an authenticated upstream release."""
import hashlib
import os
from pathlib import Path
import platform
import tempfile
import urllib.request

VERSION = "v1.9.15"
# Source: https://api.github.com/repos/AlexxIT/go2rtc/releases/tags/v1.9.15
# GitHub asset digest fields, verified 2026-10-26.
DIGESTS = {
    "amd64": "20b46c6910734cb05ecefb234066f36868ff19b42ba1418a7a32e13a3e035712",
    "arm64": "af416f22b21a24ab83b8d78642190b4932ae695b4e386853f11d1b0319781763",
    "arm": "cbffe4075a004e2015fd9d7e324800ab7968b52f49e9ce803361879d5e246028",
}


def install(root: Path) -> Path:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(machine)
    if platform.system() != "Linux" or arch not in DIGESTS:
        raise RuntimeError("Gateway installer supports Linux x86_64, aarch64 and armv7l only.")
    destination = root / ".runtime" / "bin" / "go2rtc"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    url = f"https://github.com/AlexxIT/go2rtc/releases/download/{VERSION}/go2rtc_linux_{arch}"
    request = urllib.request.Request(url, headers={"User-Agent": "PrinterCCTV-installer"})
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            digest = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(request, timeout=45) as response:
                while chunk := response.read(65536):
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise RuntimeError("Gateway download exceeded the expected size limit.")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != DIGESTS[arch]:
            raise RuntimeError("Gateway checksum mismatch; downloaded code will not be executed.")
        temporary.chmod(0o700)
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
