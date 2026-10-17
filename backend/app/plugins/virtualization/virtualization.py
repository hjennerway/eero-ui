"""Proxmox/Docker inventory, persistence, and browser-independent polling."""

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import ssl
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ...transformers import extract_id_from_url, extract_list, normalize_device
from .models import (
    ConnectionConfig,
    ConnectionSummary,
    ConnectionUpdate,
    DockerContainer,
    DockerInventory,
    GuestInventory,
    NodeInventory,
    VirtualizationSnapshot,
)

_LOGGER = logging.getLogger(__name__)
BACKGROUND_INTERVAL = 600
VISIBLE_INTERVAL = 60
_MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", re.I)
_LAN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lan_ip(value: Any) -> str | None:
    try:
        address = ipaddress.ip_address(str(value).split("/")[0])
        if any(address in network for network in _LAN_NETWORKS):
            return str(address)
    except ValueError:
        pass
    return None


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".inventory-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.chmod(name, 0o600)
            json.dump(data, handle)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _tls_context(
    ca: str | None, certificate: str | None = None, key: str | None = None
) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=ca or None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if certificate and key:
        # SSLContext loads the key into memory; never retain extra PEM files.
        with tempfile.TemporaryDirectory(prefix="eero-inventory-tls-") as directory:
            paths = [Path(directory) / "client.pem", Path(directory) / "key.pem"]
            for path, content in zip(paths, [certificate, key], strict=True):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
            # Fail on encrypted keys rather than prompting on server stdin.
            context.load_cert_chain(str(paths[0]), str(paths[1]), password=b"")
    return context


def _failure(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 401:
            return "Authentication failed. Check the connection credentials."
        if exc.response.status_code == 403:
            return "Permission denied. Check the monitoring token privileges."
        return "The inventory service returned an unexpected response."
    if isinstance(exc, (ssl.SSLError, ValueError)):
        return "Check certificate trust, hostnames, and the API response format."
    return "Could not reach the inventory service. Check its address and HTTPS certificates."


@dataclass
class _Entry:
    config: ConnectionConfig
    snapshot: VirtualizationSnapshot = field(default_factory=VirtualizationSnapshot)
    last_attempt: float = -BACKGROUND_INTERVAL
    task: asyncio.Task | None = None


class VirtualizationService:
    def __init__(self, directory: Path, client_provider, *, clock=None):
        self.directory = directory
        self.client_provider = client_provider
        self.clock = clock or time.monotonic
        self.entries: dict[str, _Entry] = {}
        self.wake = asyncio.Event()
        self.io_slots = asyncio.Semaphore(8)

    def _path(self, network_id: str, suffix: str) -> Path:
        identity = hashlib.sha256(network_id.encode()).hexdigest()
        return self.directory / f"{identity}.{suffix}.json"

    def load(self) -> None:
        if not self.directory.exists():
            return
        for path in self.directory.glob("*.connection.json"):
            try:
                config = ConnectionConfig.model_validate_json(path.read_text("utf-8"))
                entry = _Entry(config=config)
                snapshot_path = self._path(config.network_id, "snapshot")
                if snapshot_path.exists():
                    try:
                        raw_snapshot = json.loads(snapshot_path.read_text("utf-8"))
                        if raw_snapshot.get("_source_url") == config.url:
                            entry.snapshot = VirtualizationSnapshot.model_validate(
                                raw_snapshot
                            )
                            entry.snapshot.stale = True
                            entry.snapshot.refreshing = False
                    except Exception:
                        _LOGGER.warning("Could not restore a virtualization snapshot")
                self.entries[config.network_id] = entry
            except Exception:
                _LOGGER.warning(
                    "Could not restore a virtualization connection or snapshot"
                )

    def summary(self, network_id: str) -> ConnectionSummary:
        entry = self.entries.get(network_id)
        if not entry:
            return ConnectionSummary()
        config = entry.config
        return ConnectionSummary(
            configured=True,
            url=config.url,
            token_id=config.token_id,
            has_token=bool(config.token_secret),
            has_ca_certificate=bool(config.ca_certificate),
            docker_enabled=config.docker_enabled,
            docker_port=config.docker_port,
            has_docker_certificates=bool(config.docker_client_key),
        )

    def save(self, network_id: str, update: ConnectionUpdate) -> ConnectionSummary:
        previous = self.entries.get(network_id)
        if previous and previous.task and not previous.task.done():
            raise RuntimeError(
                "Wait for the current refresh before changing the connection."
            )
        data = update.model_dump(mode="python")
        # Empty secret/certificate inputs preserve saved credentials on edits.
        for key in (
            "token_secret",
            "ca_certificate",
            "docker_ca_certificate",
            "docker_client_certificate",
            "docker_client_key",
        ):
            value = data[key]
            if hasattr(value, "get_secret_value"):
                value = value.get_secret_value()
            if not value or not value.strip():
                data[key] = getattr(previous.config, key) if previous else None
        config = ConnectionConfig(network_id=network_id, **data)
        if not config.token_secret:
            raise ValueError("Enter a Proxmox API token secret.")
        if len(config.token_secret.get_secret_value()) > 1024:
            raise ValueError("The Proxmox token secret is too long.")
        try:
            _tls_context(config.ca_certificate)
            if config.docker_enabled:
                if not all(
                    (
                        config.docker_ca_certificate,
                        config.docker_client_certificate,
                        config.docker_client_key,
                    )
                ):
                    raise ValueError("missing Docker certificates")
                key = config.docker_client_key.get_secret_value()
                if len(key) > 32768:
                    raise ValueError("Docker key is too long")
                _tls_context(
                    config.docker_ca_certificate, config.docker_client_certificate, key
                )
        except (ValueError, ssl.SSLError, OSError):
            raise ValueError(
                "Check the PEM certificates and private key. Docker discovery needs a CA, client certificate, and matching key."
            ) from None
        private_data = config.model_dump(mode="json")
        for key in ("token_secret", "docker_client_key"):
            secret = getattr(config, key)
            private_data[key] = secret.get_secret_value() if secret else None
        _atomic_json(self._path(network_id, "connection"), private_data)
        # A changed endpoint must not inherit another cluster's inventory.
        same_cluster = previous and previous.config.url == config.url
        self.entries[network_id] = _Entry(
            config=config,
            snapshot=(
                previous.snapshot.model_copy(deep=True)
                if same_cluster
                else VirtualizationSnapshot(configured=True)
            ),
        )
        self.wake.set()
        return self.summary(network_id)

    def snapshot(self, network_id: str) -> VirtualizationSnapshot:
        entry = self.entries.get(network_id)
        if not entry:
            return VirtualizationSnapshot()
        result = entry.snapshot.model_copy(deep=True)
        result.configured = True
        result.refreshing = bool(entry.task and not entry.task.done())
        return result

    async def refresh(self, network_id: str, *, force: bool = True):
        entry = self.entries.get(network_id)
        if not entry:
            raise ValueError("Connect your Proxmox cluster first.")
        if entry.task and not entry.task.done():
            await asyncio.shield(entry.task)
        elif force or self.clock() - entry.last_attempt >= VISIBLE_INTERVAL:
            entry.last_attempt = self.clock()
            entry.task = asyncio.create_task(self._collect(entry))
            self.wake.set()
            # A browser closing or timing out must not cancel collection.
            await asyncio.shield(entry.task)
        return self.snapshot(network_id)

    async def run_forever(self):
        while True:
            self.wake.clear()
            now = self.clock()
            for entry in list(self.entries.values()):
                if now - entry.last_attempt >= BACKGROUND_INTERVAL:
                    if not entry.task or entry.task.done():
                        entry.last_attempt = now
                        entry.task = asyncio.create_task(self._collect(entry))
            deadlines = [
                max(1, BACKGROUND_INTERVAL - (self.clock() - entry.last_attempt))
                for entry in self.entries.values()
            ]
            try:
                await asyncio.wait_for(
                    self.wake.wait(), timeout=min(deadlines, default=600)
                )
            except TimeoutError:
                pass

    async def aclose(self):
        tasks = [entry.task for entry in self.entries.values() if entry.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _get(self, client: httpx.AsyncClient, path: str) -> Any:
        async with self.io_slots:
            response = await client.get(path)
            response.raise_for_status()
            return response.json()

    async def _pve(self, client, path):
        raw = await self._get(client, f"/api2/json{path}")
        if not isinstance(raw, dict) or "data" not in raw:
            raise ValueError("Invalid Proxmox response")
        return raw["data"]

    async def _devices(self, network_id: str) -> tuple[list[dict], bool]:
        try:
            async for client in self.client_provider():
                if not client.is_authenticated:
                    return [], False
                raw = await client.get_devices(network_id, refresh_cache=False)
                return [
                    normalize_device(item) for item in extract_list(raw, "devices")
                ], True
        except Exception:
            pass
        return [], False

    async def _collect(self, entry: _Entry):
        attempted_at = _now()
        try:
            async with asyncio.timeout(55):
                config = entry.config
                headers = {
                    "Authorization": f"PVEAPIToken={config.token_id}={config.token_secret.get_secret_value()}"
                }
                async with httpx.AsyncClient(
                    base_url=config.url,
                    headers=headers,
                    verify=_tls_context(config.ca_certificate),
                    timeout=5,
                    trust_env=False,
                    follow_redirects=False,
                ) as pve:
                    resources, devices_result = await asyncio.gather(
                        self._pve(pve, "/cluster/resources"),
                        self._devices(config.network_id),
                    )
                    if not isinstance(resources, list):
                        raise ValueError("Invalid cluster resources")
                    devices, devices_available = devices_result
                    nodes = await self._nodes(pve, resources, entry.snapshot)
                    guests = await asyncio.gather(
                        *[
                            self._guest(pve, resource, entry.snapshot)
                            for resource in resources
                            if resource.get("type") in ("qemu", "lxc")
                        ]
                    )
                    self._match(guests, devices)
                    if not devices_available:
                        for guest in guests:
                            guest.issues.append("eero device matching is unavailable.")
                    docker_context = (
                        _tls_context(
                            config.docker_ca_certificate,
                            config.docker_client_certificate,
                            config.docker_client_key.get_secret_value(),
                        )
                        if config.docker_enabled
                        else None
                    )
                    await asyncio.gather(
                        *[
                            self._docker(guest, config, docker_context, entry.snapshot)
                            for guest in guests
                        ]
                    )
                    by_node = {node.id: node for node in nodes}
                    for resource, guest in zip(
                        [r for r in resources if r.get("type") in ("qemu", "lxc")],
                        guests,
                        strict=True,
                    ):
                        node_name = str(resource["node"])
                        if node_name not in by_node:
                            node = NodeInventory(id=node_name, name=node_name)
                            nodes.append(node)
                            by_node[node_name] = node
                        by_node[node_name].guests.append(guest)
                    for node in nodes:
                        node.guests.sort(
                            key=lambda guest: (guest.name.casefold(), guest.vmid)
                        )
                    entry.snapshot = VirtualizationSnapshot(
                        configured=True,
                        collected_at=_now(),
                        attempted_at=attempted_at,
                        nodes=sorted(nodes, key=lambda node: node.name.casefold()),
                    )
        except Exception as exc:
            entry.snapshot = entry.snapshot.model_copy(deep=True)
            entry.snapshot.stale = True
            entry.snapshot.error = _failure(exc)
            entry.snapshot.attempted_at = attempted_at
            entry.snapshot.configured = True
            for node in entry.snapshot.nodes:
                for guest in node.guests:
                    if guest.docker.collected_at:
                        guest.docker.status = "stale"
                        guest.docker.message = "Showing last reported Docker states."
        try:
            _atomic_json(
                self._path(entry.config.network_id, "snapshot"),
                {
                    **entry.snapshot.model_dump(mode="json"),
                    "_source_url": entry.config.url,
                },
            )
        except OSError:
            entry.snapshot.error = (
                "Inventory updated, but its saved snapshot could not be written."
            )
            _LOGGER.warning("Could not persist virtualization inventory")
        self.wake.set()

    async def _nodes(self, pve, resources, previous):
        async def collect(resource):
            name = str(resource["node"])
            node = NodeInventory(
                id=name, name=name, status=resource.get("status", "unknown")
            )
            old = next((item for item in previous.nodes if item.id == name), None)
            node.version = old.version if old else None
            if node.status == "online":
                try:
                    version = await self._pve(
                        pve, f"/nodes/{quote(name, safe='')}/version"
                    )
                    node.version = version.get("version") or version.get("release")
                except Exception:
                    node.issues.append("Proxmox version could not be read.")
            return node

        return await asyncio.gather(
            *[
                collect(resource)
                for resource in resources
                if resource.get("type") == "node"
            ]
        )

    @staticmethod
    def _previous_guest(snapshot, identity):
        return next(
            (
                guest
                for node in snapshot.nodes
                for guest in node.guests
                if guest.id == identity
            ),
            None,
        )

    async def _guest(self, pve, resource, previous):
        kind, vmid = resource["type"], int(resource["vmid"])
        identity = f"{kind}:{vmid}"
        old = self._previous_guest(previous, identity)
        guest = GuestInventory(
            id=identity,
            vmid=vmid,
            kind=kind,
            name=resource.get("name") or f"{kind.upper()} {vmid}",
            status=resource.get("status", "unknown"),
            template=bool(resource.get("template")),
        )
        base = f"/nodes/{quote(str(resource['node']), safe='')}/{kind}/{vmid}"
        try:
            config = await self._pve(pve, f"{base}/config")
            os_type = config.get("ostype", "")
            guest.os_name = (
                f"Linux · {os_type.capitalize()}"
                if kind == "lxc" and os_type
                else (
                    "Windows"
                    if str(os_type).startswith("win")
                    else "Linux" if str(os_type).startswith("l") else None
                )
            )
            for key, value in config.items():
                if re.fullmatch(r"net\d+", key):
                    guest.macs.extend(mac.lower() for mac in _MAC.findall(str(value)))
                    for part in str(value).split(","):
                        if part.startswith(("ip=", "ip6=")):
                            address = _lan_ip(part.split("=", 1)[1])
                            if address:
                                guest.ips.append(address)
        except Exception:
            config = {}
            guest.issues.append("Guest configuration could not be read.")
            if old:
                guest.os_name, guest.os_version, guest.os_source = (
                    old.os_name,
                    old.os_version,
                    old.os_source,
                )
                guest.macs = old.macs.copy()
        if old and guest.os_source == "configured" and old.os_source != "configured":
            guest.os_name, guest.os_version, guest.os_source = (
                old.os_name,
                old.os_version,
                old.os_source,
            )
        if guest.status == "running" and not guest.template:
            if kind == "lxc":
                try:
                    interfaces = await self._pve(pve, f"{base}/interfaces")
                    self._interfaces(guest, interfaces)
                except Exception:
                    guest.issues.append("Current container addresses are unavailable.")
            else:
                agent = str(config.get("agent", "0"))
                if agent.startswith("1") or "enabled=1" in agent:

                    async def agent_query(command):
                        raw = await self._pve(pve, f"{base}/agent/{command}")
                        return raw.get("result", raw) if isinstance(raw, dict) else raw

                    results = await asyncio.gather(
                        agent_query("get-osinfo"),
                        agent_query("network-get-interfaces"),
                        return_exceptions=True,
                    )
                    os_info, interfaces = results
                    if isinstance(os_info, dict):
                        guest.os_name = os_info.get("pretty-name") or os_info.get(
                            "name"
                        )
                        guest.os_version = os_info.get("version-id") or os_info.get(
                            "version"
                        )
                        guest.os_source = "guest_agent"
                    else:
                        guest.issues.append("Guest agent OS details are unavailable.")
                    if isinstance(interfaces, list):
                        self._interfaces(guest, interfaces)
                    else:
                        guest.issues.append("Guest agent addresses are unavailable.")
                else:
                    guest.issues.append(
                        "Enable the QEMU Guest Agent for OS and address details."
                    )
        guest.ips = sorted(set(guest.ips), key=lambda value: (":" in value, value))
        guest.macs = sorted(set(guest.macs))
        return guest

    @staticmethod
    def _interfaces(guest, interfaces):
        configured_macs = set(guest.macs)
        for interface in interfaces:
            name = str(interface.get("name", ""))
            if name == "lo" or name.startswith(("docker", "br-", "veth")):
                continue
            mac = (
                interface.get("hardware-address")
                or interface.get("hwaddr")
                or interface.get("mac")
            )
            if configured_macs and (not mac or mac.lower() not in configured_macs):
                continue
            if mac and _MAC.fullmatch(mac):
                guest.macs.append(mac.lower())
            values = [interface.get("inet"), interface.get("inet6")]
            values.extend(
                item.get("ip-address") for item in interface.get("ip-addresses", [])
            )
            for value in values:
                if address := _lan_ip(value):
                    guest.ips.append(address)

    @staticmethod
    def _match(guests, devices):
        owners = Counter(mac for guest in guests for mac in set(guest.macs))
        for guest in guests:
            matches = []
            ambiguous = False
            for mac in guest.macs:
                candidates = [
                    device
                    for device in devices
                    if (device.get("mac") or "").lower() == mac
                ]
                if len(candidates) > 1 or owners[mac] > 1:
                    ambiguous = True
                    continue
                if candidates:
                    device = candidates[0]
                    identity = device.get("id") or extract_id_from_url(
                        device.get("url")
                    )
                    if identity:
                        matches.append(identity)
                    # Only current, unambiguous eero leases may supply an IP.
                    if device.get("connected") and (
                        address := _lan_ip(device.get("ip"))
                    ):
                        guest.ips.append(address)
            guest.eero_device_ids = sorted(set(matches))
            guest.match_status = (
                "ambiguous" if ambiguous else "matched" if matches else "unmatched"
            )
            guest.ips = sorted(set(guest.ips), key=lambda value: (":" in value, value))

    async def _docker(self, guest, config, context, previous):
        old_guest = self._previous_guest(previous, guest.id)
        old = old_guest.docker if old_guest else None
        if not config.docker_enabled:
            guest.docker = DockerInventory(status="disabled")
            return
        if guest.template:
            guest.docker = DockerInventory(
                status="unknown",
                message="Template; Docker discovery is not applicable.",
            )
            return
        if guest.status != "running" or not guest.ips:
            message = (
                "Guest is stopped."
                if guest.status == "stopped"
                else "No current LAN address is available."
            )
            guest.docker = (
                old.model_copy(deep=True)
                if old and old.collected_at
                else DockerInventory(status="unknown")
            )
            if guest.docker.collected_at:
                guest.docker.status = "stale"
            guest.docker.message = message
            return
        failures = []
        for ip in guest.ips[:2]:
            hostname = f"[{ip}]" if ":" in ip else ip
            try:
                async with httpx.AsyncClient(
                    base_url=f"https://{hostname}:{config.docker_port}",
                    verify=context,
                    timeout=httpx.Timeout(4, connect=1.5),
                    trust_env=False,
                    follow_redirects=False,
                ) as docker:
                    version = await self._get(docker, "/version")
                    api_version = version.get("ApiVersion", "")
                    if not re.fullmatch(r"\d+\.\d+", api_version):
                        raise ValueError("Invalid Docker API version")
                    info, containers = await asyncio.gather(
                        self._get(docker, f"/v{api_version}/info"),
                        self._get(docker, f"/v{api_version}/containers/json?all=true"),
                    )
                    if not isinstance(info, dict) or not isinstance(containers, list):
                        raise ValueError("Invalid Docker response")
                    guest.docker = DockerInventory(
                        status="available",
                        engine_id=info.get("ID"),
                        version=version.get("Version"),
                        collected_at=_now(),
                        containers=sorted(
                            [
                                DockerContainer(
                                    id=item["Id"],
                                    name=(item.get("Names") or [item["Id"][:12]])[
                                        0
                                    ].lstrip("/"),
                                    image=item.get("Image", ""),
                                    state=item.get("State", "unknown"),
                                    health=(
                                        "unhealthy"
                                        if "(unhealthy)" in item.get("Status", "")
                                        else (
                                            "healthy"
                                            if "(healthy)" in item.get("Status", "")
                                            else None
                                        )
                                    ),
                                    project=(item.get("Labels") or {}).get(
                                        "com.docker.compose.project"
                                    ),
                                )
                                for item in containers
                            ],
                            key=lambda item: item.name.casefold(),
                        ),
                    )
                    if guest.os_source != "guest_agent" and info.get("OperatingSystem"):
                        guest.os_name = info["OperatingSystem"]
                        guest.os_version = info.get("OSVersion")
                        guest.os_source = "docker"
                    return
            except Exception as exc:
                failures.append(exc)
        if old and old.collected_at:
            guest.docker = old.model_copy(deep=True)
            guest.docker.status = "stale"
            guest.docker.message = (
                "Docker inventory is unavailable; showing its last reported states."
            )
        else:
            refused = all(
                isinstance(exc, httpx.ConnectError)
                and (
                    "refused" in str(exc).lower()
                    or "connect call failed" in str(exc).lower()
                )
                for exc in failures
            )
            guest.docker = DockerInventory(
                status="not_detected" if refused else "unavailable",
                message=(
                    "Docker API not detected." if refused else _failure(failures[-1])
                ),
            )
