"""Small transactional SQLite document store; camera secrets never leave this module unencrypted."""

import copy
import json
import os
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path

from cryptography.fernet import Fernet


def new_id():
    return str(uuid.uuid4())


def default_state():
    playlist_id = new_id()
    return {
        "revision": 1,
        "cameras": [],
        "assets": [],
        "playlists": [{
            "id": playlist_id, "name": "Shop announcements", "transition": "fade",
            "transition_ms": 600, "slide_direction": "left", "items": [],
        }],
        "layouts": [{
            "id": new_id(), "name": "Main camera wall", "width": 1920, "height": 1080,
            "background": "#090d14", "tiles": [], "published": None,
            "display_token": secrets.token_urlsafe(32),
        }],
        "settings": {"shop_name": "PrintWatch", "lan_host": os.getenv("PRINTWATCH_LAN_HOST", "")},
    }


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.lock = threading.RLock()
        keyfile = directory / "credentials.key"
        if not keyfile.exists():
            try:
                fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
        os.chmod(keyfile, 0o600)
        self.cipher = Fernet(keyfile.read_bytes())
        self.db = sqlite3.connect(directory / "printwatch.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        if not self.db.execute("SELECT 1 FROM state WHERE id=1").fetchone():
            self.db.execute("INSERT INTO state VALUES (1, ?)", (json.dumps(default_state()),))
            self.db.commit()
        os.chmod(directory / "printwatch.sqlite3", 0o600)

    def read(self):
        with self.lock:
            return json.loads(self.db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])

    def change(self, operation):
        with self.lock:
            state = self.read()
            result = operation(state)
            state["revision"] += 1
            self.db.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(state),))
            self.db.commit()
            return copy.deepcopy(result)

    def encrypt(self, text):
        return self.cipher.encrypt(text.encode()).decode() if text else ""

    def decrypt(self, text):
        return self.cipher.decrypt(text.encode()).decode() if text else ""

    def close(self):
        self.db.close()


def public_camera(camera, display=False):
    if display:
        return {key: camera[key] for key in ("id", "name", "kind", "model", "enabled")}
    result = {key: value for key, value in camera.items() if key != "password_encrypted"}
    result["has_password"] = bool(camera.get("password_encrypted"))
    return result
