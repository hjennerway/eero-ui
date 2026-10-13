"""Read network-scoped ARP observations written by the isolated LAN scanner."""

from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator


class DiscoveredDevice(BaseModel):
    ip: IPv4Address
    mac: str
    last_seen: AwareDatetime
    connected: bool = False

    @field_validator("mac")
    @classmethod
    def valid_mac(cls, value: str) -> str:
        compact = value.replace(":", "").replace("-", "").lower()
        if (
            len(compact) != 12
            or any(c not in "0123456789abcdef" for c in compact)
            or int(compact, 16) == 0
            or int(compact[:2], 16) & 1
        ):
            raise ValueError("Expected a unicast MAC address")
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))


class DiscoverySnapshot(BaseModel):
    network_id: str = Field(min_length=1, max_length=128)
    subnet: IPv4Network
    interval_seconds: int = Field(default=3600, ge=3600, le=86400)
    status: Literal["ok", "error"] = "ok"
    last_scan: AwareDatetime
    last_success: AwareDatetime | None = None
    next_scan: AwareDatetime
    devices: list[DiscoveredDevice] = Field(default_factory=list, max_length=1024)

    @field_validator("subnet")
    @classmethod
    def private_lan(cls, subnet: IPv4Network) -> IPv4Network:
        private = [
            IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ]
        if (
            not any(subnet.subnet_of(n) for n in private)
            or not 22 <= subnet.prefixlen <= 30
        ):
            raise ValueError("Configure a private IPv4 LAN between /22 and /30")
        return subnet

    @model_validator(mode="after")
    def check_observations(self):
        for device in self.devices:
            if (
                device.ip not in self.subnet
                or device.ip
                in (self.subnet.network_address, self.subnet.broadcast_address)
                or device.last_seen > self.last_scan
            ):
                raise ValueError("Observation is outside the scan")
        return self


class DiscoveryResponse(BaseModel):
    status: Literal["disabled", "pending", "ok", "error", "stale"] = "disabled"
    subnet: IPv4Network | None = None
    interval_seconds: int = 3600
    last_scan: AwareDatetime | None = None
    next_scan: AwareDatetime | None = None
    warning: str | None = None
    devices: list[DiscoveredDevice] = Field(default_factory=list)


def read_snapshot(path: Path) -> DiscoverySnapshot:
    if path.stat().st_size > 512 * 1024:
        raise ValueError("Discovery file is too large")
    return DiscoverySnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def read_discovery(filename: str | None, network_id: str) -> DiscoveryResponse:
    if not filename:
        return DiscoveryResponse()
    try:
        snapshot = read_snapshot(Path(filename))
    except FileNotFoundError:
        return DiscoveryResponse(
            status="pending", warning="Waiting for the first LAN scan."
        )
    if snapshot.network_id != network_id:
        return DiscoveryResponse()
    now = datetime.now(UTC)
    stale = (now - snapshot.last_scan).total_seconds() > snapshot.interval_seconds + 600
    if snapshot.last_scan > now or snapshot.next_scan < snapshot.last_scan:
        raise ValueError("Invalid scan timestamps")
    status = "stale" if stale else snapshot.status
    warning = {
        "stale": "LAN discovery is overdue. Previously found devices are unconfirmed.",
        "error": "The last LAN scan failed. Previously found devices are unconfirmed.",
    }.get(status)
    return DiscoveryResponse(
        status=status,
        subnet=snapshot.subnet,
        interval_seconds=snapshot.interval_seconds,
        last_scan=snapshot.last_scan,
        next_scan=snapshot.next_scan,
        warning=warning,
        devices=[
            d.model_copy(update={"connected": d.connected and status == "ok"})
            for d in snapshot.devices
        ],
    )
