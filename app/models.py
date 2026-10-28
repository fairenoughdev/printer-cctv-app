"""Validated, bounded API models."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Camera(Input):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["bambu", "u1", "rtsp", "mjpeg", "snapshot"] = "bambu"
    model: str = Field(default="H2 / P2", max_length=100)
    host: str = Field(default="", max_length=253)
    url: str = Field(default="", max_length=2048)
    username: str = Field(default="", max_length=128)
    password: str | None = Field(default=None, max_length=1024)
    verify_tls: bool = True
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        if not value.strip():
            raise ValueError("A name is required")
        return value.strip()

    @field_validator("host")
    @classmethod
    def valid_host(cls, value):
        value = value.strip()
        if value and (any(c in value for c in "/\\@?# \r\n") or "://" in value):
            raise ValueError("Enter a hostname or IP address, without a protocol or path")
        return value

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        if value:
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https", "rtsp", "rtsps"):
                raise ValueError("Only HTTP, HTTPS, RTSP, and RTSPS camera URLs are allowed")
            if not parts.hostname or parts.username or parts.password:
                raise ValueError("Enter a camera URL without credentials; use the credential fields")
            if "#" in value or any(c in value for c in "\r\n"):
                raise ValueError("URL fragments and control characters are not allowed")
        return value


class PlaylistItem(Input):
    asset_id: str
    page: int = Field(default=0, ge=0, le=199)
    duration: float = Field(default=10, ge=1, le=3600)
    enabled: bool = True


class Playlist(Input):
    name: str = Field(min_length=1, max_length=100)
    transition: Literal["fade", "slide", "cut"] = "fade"
    transition_ms: int = Field(default=600, ge=0, le=3000)
    slide_direction: Literal["left", "right", "up", "down"] = "left"
    items: list[PlaylistItem] = Field(default_factory=list, max_length=1000)


class Tile(Input):
    id: str = Field(min_length=1, max_length=100)
    type: Literal["camera", "advertisement"]
    source_id: str
    x: float = Field(default=0, ge=0, le=7680)
    y: float = Field(default=0, ge=0, le=7680)
    width: float = Field(default=640, ge=32, le=7680)
    height: float = Field(default=360, ge=32, le=7680)
    fit: Literal["contain", "cover"] = "contain"
    label: bool = True
    z: int = Field(default=0, ge=0, le=100)


class Layout(Input):
    name: str = Field(min_length=1, max_length=100)
    width: int = Field(default=1920, ge=320, le=7680)
    height: int = Field(default=1080, ge=240, le=7680)
    background: str = Field(default="#090d14", pattern=r"^#[0-9a-fA-F]{6}$")
    tiles: list[Tile] = Field(default_factory=list, max_length=64)


class Settings(Input):
    shop_name: str = Field(default="PrintWatch", min_length=1, max_length=100)
    lan_host: str = Field(default="", max_length=253, pattern=r"^[a-zA-Z0-9.:\-\[\]]*$")
