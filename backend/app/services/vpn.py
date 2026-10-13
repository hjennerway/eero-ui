"""Read-only OpenWrt tunnel monitoring, collected independently every 30 seconds."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import shlex
import socket
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import paramiko
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .victoria import Sample, VictoriaMetricsClient, VictoriaWriteError

INTERVAL_SECONDS = 30
_LOGGER = logging.getLogger(__name__)


class VPNConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    host: str = "192.168.4.10"
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(
        default="root", min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$"
    )
    interface: str = Field(
        default="tun0", min_length=1, max_length=15, pattern=r"^[a-zA-Z0-9_.-]+$"
    )
    password: SecretStr | None = Field(default=None, max_length=4096)
    host_key: str | None = Field(default=None, max_length=128)

    @field_validator("host")
    @classmethod
    def private_address(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        if (
            not address.is_private
            or address.is_loopback
            or address.is_unspecified
            or address.is_link_local
            or address.is_multicast
        ):
            raise ValueError("Enter a private LAN IP address.")
        return str(address)


class VPNConnectionUpdate(VPNConfig):
    # Null preserves an existing password; disabling removes it.
    host_key: str | None = Field(default=None, exclude=True)


class VPNConnection(BaseModel):
    enabled: bool
    host: str
    port: int
    username: str
    interface: str
    password_set: bool
    host_key: str | None


class VPNSnapshot(BaseModel):
    host: str = "192.168.4.10"
    interface: str = "tun0"
    provider: str = "NordVPN"
    status: Literal[
        "connected", "disconnected", "unknown", "unreachable", "disabled"
    ] = "unknown"
    reachable: bool = False
    connected_clients: int | None = None
    download_mbps: float | None = None
    upload_mbps: float | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    collected_at: datetime | None = None
    last_success_at: datetime | None = None
    stale: bool = True
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    history_error: str | None = None
    interval_seconds: int = INTERVAL_SECONDS


class VPNReadError(Exception):
    """A static, safe message suitable for display without leaking SSH details."""


def save_config(path: Path, config: VPNConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude={"password"})
    data["password"] = config.password.get_secret_value() if config.password else None
    fd, temporary = tempfile.mkstemp(prefix=".vpn-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_config(path: Path) -> VPNConfig:
    if not path.exists():
        return VPNConfig()
    if path.stat().st_size > 32768:
        raise ValueError("VPN configuration is too large")
    return VPNConfig.model_validate_json(path.read_text(encoding="utf-8"))


def connection_status(config: VPNConfig) -> VPNConnection:
    return VPNConnection(
        **config.model_dump(exclude={"password"}), password_set=bool(config.password)
    )


def collector_command(interface: str) -> str:
    # Fixed read-only commands. Never read OpenVPN configs or WireGuard private keys.
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
        raise ValueError("Invalid tunnel interface")
    return f"""LC_ALL=C
printf '\\n__VPN_BOOT__\\n'; cat /proc/sys/kernel/random/boot_id
printf '\\n__VPN_UPTIME__\\n'; cat /proc/uptime
printf '\\n__VPN_LINKS__\\n'; ip -j -s link show
printf '\\n__VPN_ADDRESSES__\\n'; ip -j address show
printf '\\n__VPN_INTERFACES__\\n'; ubus call network.interface dump
printf '\\n__VPN_CONNTRACK__\\n'
if [ -r /proc/net/nf_conntrack ]; then head -n 20001 /proc/net/nf_conntrack
elif [ -r /proc/net/ip_conntrack ]; then head -n 20001 /proc/net/ip_conntrack
else printf 'UNAVAILABLE\\n'; fi
printf '\\n__VPN_PROBE__\\n'
if [ -d /sys/class/net/{shlex.quote(interface)} ]; then
  ping -I {shlex.quote(interface)} -c 1 -W 2 1.1.1.1 >/dev/null 2>&1
  printf '%s\\n' "$?"
else printf 'MISSING\\n'; fi
"""


def read_openwrt(config: VPNConfig) -> tuple[dict[str, str], str]:
    """Bounded SSH session with host-key pinning before sending credentials."""
    try:
        with socket.create_connection((config.host, config.port), timeout=4) as sock:
            with paramiko.Transport(sock) as transport:
                transport.banner_timeout = 4
                transport.auth_timeout = 4
                transport.start_client(timeout=4)
                fingerprint = "SHA256:" + base64.b64encode(
                    hashlib.sha256(transport.get_remote_server_key().asbytes()).digest()
                ).decode().rstrip("=")
                if config.host_key and config.host_key != fingerprint:
                    raise VPNReadError(
                        "The OpenWrt SSH host key changed. Check the device before updating the stored fingerprint."
                    )
                if config.password:
                    transport.auth_password(
                        config.username, config.password.get_secret_value()
                    )
                else:
                    try:
                        transport.auth_none(config.username)
                    except paramiko.BadAuthenticationType:
                        transport.auth_password(config.username, "")
                with transport.open_session(timeout=4) as channel:
                    channel.settimeout(4)
                    channel.set_combine_stderr(True)
                    channel.exec_command(collector_command(config.interface))
                    chunks: list[bytes] = []
                    size = 0
                    deadline = time.monotonic() + 7
                    while True:
                        if channel.recv_ready():
                            chunk = channel.recv(65536)
                            size += len(chunk)
                            if size > 4 * 1024 * 1024:
                                raise VPNReadError(
                                    "OpenWrt returned too much monitoring data."
                                )
                            chunks.append(chunk)
                        elif channel.exit_status_ready():
                            break
                        elif time.monotonic() >= deadline:
                            raise VPNReadError(
                                "The OpenWrt monitoring command timed out."
                            )
                        else:
                            time.sleep(0.01)
                    if channel.recv_exit_status() != 0:
                        raise VPNReadError(
                            "OpenWrt could not run the monitoring commands."
                        )
        output = b"".join(chunks).decode("utf-8", errors="replace")
        sections = re.split(r"\n__VPN_([A-Z]+)__\n", output)
        return {
            sections[i]: sections[i + 1].strip() for i in range(1, len(sections), 2)
        }, fingerprint
    except paramiko.AuthenticationException as exc:
        raise VPNReadError(
            "SSH login failed. Check the OpenWrt username and password in Connection."
        ) from exc
    except (paramiko.SSHException, OSError, EOFError) as exc:
        raise VPNReadError(
            "Cannot reach OpenWrt over SSH. Check its address, SSH service and network access."
        ) from exc


def parse_reading(
    config: VPNConfig, data: dict[str, str]
) -> tuple[VPNSnapshot, tuple | None]:
    now = datetime.now(UTC)
    links = json.loads(data["LINKS"])
    addresses = json.loads(data["ADDRESSES"])
    # A valid netifd response also verifies that this is an OpenWrt instance.
    json.loads(data["INTERFACES"])["interface"]
    link = next((item for item in links if item["ifname"] == config.interface), None)
    local_addresses = {
        a["local"] for item in addresses for a in item.get("addr_info", [])
    }
    tunnel_addresses = {
        a["local"]
        for item in addresses
        if item["ifname"] == config.interface
        for a in item.get("addr_info", [])
        if a.get("scope") == "global"
    }
    snapshot = VPNSnapshot(
        host=config.host,
        interface=config.interface,
        reachable=True,
        collected_at=now,
        last_success_at=now,
        stale=False,
    )
    # netifd's availability can lag an OpenVPN-created device. Check the device itself.
    if not link or "UP" not in link.get("flags", []) or not tunnel_addresses:
        snapshot.status = "disconnected"
        snapshot.connected_clients = 0
        snapshot.download_mbps = snapshot.upload_mbps = 0.0
        snapshot.warnings.append(
            "The tunnel interface is missing, down, or has no assigned address."
        )
        return snapshot, None
    snapshot.status = "connected" if data.get("PROBE") == "0" else "unknown"
    if snapshot.status == "unknown":
        snapshot.warnings.append(
            "The tunnel exists, but a ping through it to 1.1.1.1 did not confirm connectivity. ICMP may be blocked."
        )
    rows = data.get("CONNTRACK", "UNAVAILABLE").splitlines()
    if rows == ["UNAVAILABLE"] or len(rows) > 20000:
        snapshot.warnings.append(
            "VPN client count is unavailable: connection tracking is missing or exceeds the collection limit."
        )
    else:
        clients: set[str] = set()
        for row in rows:
            if "UNREPLIED" in row or (" tcp " in row and " ESTABLISHED " not in row):
                continue
            sources = re.findall(r"\bsrc=([^\s]+)", row)
            destinations = re.findall(r"\bdst=([^\s]+)", row)
            if (
                len(sources) == len(destinations) == 2
                and sources[0] not in local_addresses
                and destinations[1] in tunnel_addresses
            ):
                clients.add(sources[0])
        snapshot.connected_clients = len(clients)
    snapshot.warnings.append(
        "Clients are unique source IPs with tracked, replied connections NATed through this tunnel; idle clients and traffic without NAT are not counted."
    )
    stats = link.get("stats64", link.get("stats", {}))
    if not stats:
        snapshot.warnings.append("Tunnel traffic counters are unavailable.")
        return snapshot, None
    snapshot.rx_bytes = int(stats["rx"]["bytes"])
    snapshot.tx_bytes = int(stats["tx"]["bytes"])
    uptime = float(data["UPTIME"].split()[0])
    baseline = (
        data["BOOT"],
        link["ifindex"],
        uptime,
        snapshot.rx_bytes,
        snapshot.tx_bytes,
    )
    return snapshot, baseline


class VPNService:
    def __init__(self, path: Path, victoria: VictoriaMetricsClient):
        self.path = path
        self.victoria = victoria
        self._lock = asyncio.Lock()
        self._baseline: tuple | None = None
        self.snapshot = VPNSnapshot()

    async def connection(self) -> VPNConnection:
        async with self._lock:
            return connection_status(await asyncio.to_thread(read_config, self.path))

    async def update(self, update: VPNConnectionUpdate) -> VPNConnection:
        async with self._lock:
            current = await asyncio.to_thread(read_config, self.path)
            same_device = (current.host, current.port, current.username) == (
                update.host,
                update.port,
                update.username,
            )
            config = VPNConfig(
                **update.model_dump(exclude={"password", "host_key"}),
                password=(
                    (update.password or current.password)
                    if same_device and update.enabled
                    else update.password if update.enabled else None
                ),
                host_key=current.host_key if same_device else None,
            )
            # Authenticate and inspect before saving. A down tunnel is still a valid SSH connection.
            if config.enabled:
                data, fingerprint = await asyncio.to_thread(read_openwrt, config)
                parse_reading(config, data)
                config.host_key = fingerprint
            await asyncio.to_thread(save_config, self.path, config)
            self._baseline = None
            self.snapshot = VPNSnapshot(
                host=config.host,
                interface=config.interface,
                status="unknown" if config.enabled else "disabled",
            )
            return connection_status(config)

    async def collect_once(self) -> VPNSnapshot:
        async with self._lock:
            try:
                config = await asyncio.to_thread(read_config, self.path)
                if not config.enabled:
                    self._baseline = None
                    self.snapshot = VPNSnapshot(
                        host=config.host,
                        interface=config.interface,
                        status="disabled",
                        stale=False,
                    )
                    return self.snapshot
                data, fingerprint = await asyncio.to_thread(read_openwrt, config)
                snapshot, baseline = parse_reading(config, data)
                if not config.host_key:
                    config.host_key = fingerprint
                    await asyncio.to_thread(save_config, self.path, config)
                if baseline and self._baseline and baseline[:2] == self._baseline[:2]:
                    elapsed = baseline[2] - self._baseline[2]
                    rx_delta = baseline[3] - self._baseline[3]
                    tx_delta = baseline[4] - self._baseline[4]
                    if (
                        0 < elapsed <= INTERVAL_SECONDS * 3
                        and rx_delta >= 0
                        and tx_delta >= 0
                    ):
                        snapshot.download_mbps = rx_delta * 8 / elapsed / 1_000_000
                        snapshot.upload_mbps = tx_delta * 8 / elapsed / 1_000_000
                self._baseline = baseline
            except (
                VPNReadError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
            ) as exc:
                self._baseline = None
                snapshot = VPNSnapshot(
                    host=self.snapshot.host,
                    interface=self.snapshot.interface,
                    status=(
                        "unreachable" if isinstance(exc, VPNReadError) else "unknown"
                    ),
                    collected_at=datetime.now(UTC),
                    last_success_at=self.snapshot.last_success_at,
                    error=(
                        str(exc)
                        if isinstance(exc, VPNReadError)
                        else "Could not read VPN settings or OpenWrt monitoring data."
                    ),
                )
            self.snapshot = snapshot
            await self._write(snapshot)
            return snapshot

    async def _write(self, snapshot: VPNSnapshot) -> None:
        timestamp = int(snapshot.collected_at.timestamp() * 1000)
        labels = {"host": snapshot.host, "interface": snapshot.interface}
        values = {
            "reachable": int(snapshot.reachable),
            "connection_status": (
                1
                if snapshot.status == "connected"
                else 0 if snapshot.status == "disconnected" else -1
            ),
            "connected_clients": snapshot.connected_clients,
            "download_mbps": snapshot.download_mbps,
            "upload_mbps": snapshot.upload_mbps,
        }
        samples = [
            Sample(f"vpn_{name}", value, timestamp, labels)
            for name, value in values.items()
            if value is not None
        ]
        try:
            await asyncio.wait_for(self.victoria.write(samples), timeout=5)
        except (VictoriaWriteError, TimeoutError):
            snapshot.history_error = "VPN readings are live, but the metrics store is unavailable. This sample could not be recorded."

    async def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.collect_once()
            except (
                Exception
            ):  # Keep this collector independent of all other integrations.
                _LOGGER.exception("VPN collection failed")
            await asyncio.sleep(max(0, INTERVAL_SECONDS - (time.monotonic() - started)))
