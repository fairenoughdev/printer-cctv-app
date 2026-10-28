"""Content versions for the browser bundle, independent of user configuration."""

import hashlib
import threading
from pathlib import Path


class FrontendVersion:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = threading.Lock()
        self.signature = None
        self.digest = ""

    def current(self):
        with self.lock:
            paths = sorted(
                p for p in self.directory.rglob("*")
                if p.is_file() and p.suffix in (".html", ".css", ".js")
            )
            signature = tuple(
                (str(p.relative_to(self.directory)), p.stat().st_mtime_ns, p.stat().st_size)
                for p in paths
            )
            if signature != self.signature:
                digest = hashlib.sha256()
                for path in paths:
                    digest.update(str(path.relative_to(self.directory)).encode())
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
                self.digest = digest.hexdigest()
                self.signature = signature
            return self.digest
