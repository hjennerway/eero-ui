"""Public inventory and private connection models for virtualization."""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator


class ConnectionUpdate(BaseModel):
    url: str = Field(max_length=512)
    token_id: str = Field(max_length=256)
    token_secret: SecretStr | None = None
    ca_certificate: str | None = Field(default=None, max_length=32768)
    docker_enabled: bool = False
    docker_port: int = Field(default=2376, ge=1, le=65535)
    docker_ca_certificate: str | None = Field(default=None, max_length=32768)
    docker_client_certificate: str | None = Field(default=None, max_length=32768)
    docker_client_key: SecretStr | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("Enter a valid Proxmox HTTPS address.") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or port == 0
        ):
            raise ValueError("Use an HTTPS address without a path or credentials.")
        return value.strip().rstrip("/")

    @field_validator("token_id")
    @classmethod
    def validate_token_id(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[\w.\-]+@[\w.\-]+![\w.\-]+", value):
            raise ValueError("Use a token ID in the form user@realm!token-name.")
        return value


class ConnectionConfig(ConnectionUpdate):
    network_id: str


class ConnectionSummary(BaseModel):
    configured: bool = False
    url: str = ""
    token_id: str = ""
    has_token: bool = False
    has_ca_certificate: bool = False
    docker_enabled: bool = False
    docker_port: int = 2376
    has_docker_certificates: bool = False


class DockerContainer(BaseModel):
    id: str
    name: str
    image: str = ""
    state: str = "unknown"
    health: str | None = None
    project: str | None = None


class DockerInventory(BaseModel):
    status: Literal[
        "disabled", "unknown", "available", "unavailable", "not_detected", "stale"
    ] = "unknown"
    engine_id: str | None = None
    version: str | None = None
    containers: list[DockerContainer] = Field(default_factory=list)
    collected_at: str | None = None
    message: str | None = None


class GuestInventory(BaseModel):
    id: str
    vmid: int
    kind: Literal["qemu", "lxc"]
    name: str
    status: str = "unknown"
    template: bool = False
    os_name: str | None = None
    os_version: str | None = None
    os_source: Literal["configured", "guest_agent", "docker"] = "configured"
    ips: list[str] = Field(default_factory=list)
    macs: list[str] = Field(default_factory=list)
    eero_device_ids: list[str] = Field(default_factory=list)
    match_status: Literal["matched", "unmatched", "ambiguous"] = "unmatched"
    issues: list[str] = Field(default_factory=list)
    docker: DockerInventory = Field(default_factory=DockerInventory)


class NodeInventory(BaseModel):
    id: str
    name: str
    version: str | None = None
    status: str = "unknown"
    guests: list[GuestInventory] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class VirtualizationSnapshot(BaseModel):
    configured: bool = False
    refreshing: bool = False
    collected_at: str | None = None
    attempted_at: str | None = None
    stale: bool = False
    error: str | None = None
    nodes: list[NodeInventory] = Field(default_factory=list)
    background_interval_seconds: int = 600
    visible_interval_seconds: int = 60
