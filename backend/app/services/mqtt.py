"""Persist MQTT settings and publish collected metrics with HA discovery."""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import paho.mqtt.client as mqtt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .nas import REFRESH_SECONDS, NASDisk, NASSnapshot, NASStorage
from .nas_network import EXPIRY_SECONDS as NAS_NETWORK_EXPIRY
from .nas_network import NASNetworkSnapshot
from .victoria import Sample
from .vpn import VPNControlError, VPNReadError, VPNSnapshot

_SENSORS = {
    "wired_clients": ("Wired clients", "mdi:ethernet", None),
    "wireless_clients": ("Wireless clients", "mdi:wifi", None),
    "download_mbps": ("Latest speed test download", "mdi:download", "Mbit/s"),
    "upload_mbps": ("Latest speed test upload", "mdi:upload", "Mbit/s"),
}
_VOLUME_SENSORS = {
    "capacity_bytes": ("Total capacity", "B", "data_size"),
    "used_bytes": ("Used space", "B", "data_size"),
    "free_bytes": ("Free space", "B", "data_size"),
    "used_percent": ("Used capacity", "%", None),
    "status": ("Status", None, None),
    "health": ("Health", None, None),
}
_DISK_SENSORS = {
    "status": ("Status", None, None),
    "smart_status": ("SMART health", None, None),
    "health": ("Health", None, None),
    "temperature_c": ("Temperature", "°C", "temperature"),
    "capacity_bytes": ("Total capacity", "B", "data_size"),
    "power_on_hours": ("Power-on hours", "h", "duration"),
    "ssd_life_remaining_percent": ("SSD life remaining", "%", None),
}
_NAS_SENSORS = {"volumes": _VOLUME_SENSORS, "disks": _DISK_SENSORS}
_NAS_EXPIRY = REFRESH_SECONDS * 3
_VPN_SENSORS = {
    "status": ("Connection status", None),
    "connected_clients": ("Active clients", None),
    "download_mbps": ("Download throughput", "Mbit/s"),
    "upload_mbps": ("Upload throughput", "Mbit/s"),
}
_VPN_EXPIRY = 90
_LOGGER = logging.getLogger(__name__)


def parse_broker_address(address: str) -> tuple[str, int, bool]:
    """Accept a host[:port] or mqtt(s) URL, without embedded credentials."""
    try:
        parsed = urlsplit(address if "://" in address else f"mqtt://{address}")
        tls = parsed.scheme == "mqtts"
        if (
            parsed.scheme not in ("mqtt", "mqtts")
            or not parsed.hostname
            or any(c.isspace() for c in parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        port = parsed.port if parsed.port is not None else (8883 if tls else 1883)
        if not 1 <= port <= 65535:
            raise ValueError
        return parsed.hostname, port, tls
    except ValueError as exc:
        raise ValueError(
            "Use a broker hostname, host:port, or mqtt(s)://host:port."
        ) from exc


class MQTTSettingsUpdate(BaseModel):
    """A null password preserves the saved secret; an empty one clears it."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    broker_address: str = Field(default="", max_length=512)
    username: str = Field(default="", max_length=256)
    password: SecretStr | None = Field(default=None, max_length=4096)
    home_assistant_discovery: bool = True
    discovery_prefix: str = Field(default="homeassistant", max_length=128)

    @field_validator("broker_address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        value = value.strip()
        if value:
            parse_broker_address(value)
        return value

    @field_validator("discovery_prefix")
    @classmethod
    def valid_prefix(cls, value: str) -> str:
        value = value.strip().strip("/")
        if (
            not value
            or any(c in value for c in "+#\x00")
            or any(c.isspace() for c in value)
        ):
            raise ValueError(
                "Enter a discovery prefix without spaces or MQTT wildcards."
            )
        return value


class MQTTSettings(MQTTSettingsUpdate):
    password: SecretStr = SecretStr("")
    instance_id: str = Field(
        default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$"
    )


class MQTTStatus(BaseModel):
    enabled: bool
    broker_address: str
    username: str
    password_set: bool
    home_assistant_discovery: bool
    discovery_prefix: str
    connection_status: Literal["disabled", "connecting", "connected", "error"]
    message: str | None
    vpn_topic: str


def load_settings(path: Path) -> MQTTSettings:
    if not path.exists():
        return MQTTSettings()
    config = MQTTSettings.model_validate_json(path.read_text(encoding="utf-8"))
    if config.enabled and not config.broker_address:
        raise ValueError("Enabled MQTT requires a broker address.")
    return config


def save_settings(path: Path, config: MQTTSettings) -> None:
    """Write atomically with owner-only permissions on POSIX systems."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json")
    payload["password"] = config.password.get_secret_value()
    fd, temporary = tempfile.mkstemp(prefix=".mqtt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class MQTTService:
    """Paho owns its network thread; callbacks and collection share a lock.

    Publishes aggregate network metrics and configured NAS disk/volume readings.
    Client addresses and credentials are never included in MQTT payloads.
    """

    def __init__(self, path: Path, interval_seconds: int = 60) -> None:
        self.path = path
        self.config = load_settings(path)
        self._expiry = max(60, interval_seconds * 3)
        self._lock = threading.RLock()
        self._update_lock = asyncio.Lock()
        self._client: mqtt.Client | None = None
        self._status = "disabled"
        self._message: str | None = None
        self._snapshots: dict[str, dict] = {}
        self._snapshot_at = 0.0
        self._announced: dict[str, str] = {}
        self._nas_snapshots: dict[tuple[str, str], NASSnapshot] = {}
        self._nas_network_snapshots: dict[tuple[str, str], NASNetworkSnapshot] = {}
        self._nas_removed_topics: set[str] = set()
        self._vpn_snapshot: VPNSnapshot | None = None
        self._vpn_control: (
            Callable[[Literal["connect", "disconnect"]], Awaitable[VPNSnapshot]] | None
        ) = None
        self._vpn_loop: asyncio.AbstractEventLoop | None = None
        self._vpn_pending: Future | None = None
        self._vpn_busy = False

    @property
    def base_topic(self) -> str:
        return f"eero_ui/{self.config.instance_id}"

    def status(self) -> MQTTStatus:
        with self._lock:
            return MQTTStatus(
                **self.config.model_dump(exclude={"password", "instance_id"}),
                password_set=bool(self.config.password.get_secret_value()),
                connection_status=self._status,
                message=self._message,
                vpn_topic=f"{self.base_topic}/vpn",
            )

    def set_vpn_controller(
        self,
        controller: Callable[
            [Literal["connect", "disconnect"]], Awaitable[VPNSnapshot]
        ],
    ) -> None:
        with self._lock:
            self._vpn_control = controller
            self._vpn_loop = asyncio.get_running_loop()
            if self._client and self._client.is_connected():
                self._client.subscribe(f"{self.base_topic}/vpn/command", qos=0)

    def publish_vpn(self, snapshot: VPNSnapshot) -> None:
        with self._lock:
            self._vpn_snapshot = snapshot.model_copy(deep=True)
            if self._client and self._client.is_connected():
                self._publish_snapshot()

    def start(self) -> None:
        with self._lock:
            if not self.config.enabled or self._client is not None:
                return
            host, port, tls = parse_broker_address(self.config.broker_address)
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"eero-ui-{self.config.instance_id}",
            )
            self._client = client
            self._status, self._message = "connecting", None
            client.on_connect = self._on_connect
            client.on_connect_fail = self._on_connect_fail
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            client.reconnect_delay_set(min_delay=1, max_delay=60)
            client.max_queued_messages_set(256)
            client.will_set(
                f"{self.base_topic}/availability", "offline", qos=1, retain=True
            )
            if self.config.username:
                client.username_pw_set(
                    self.config.username, self.config.password.get_secret_value()
                )
            if tls:
                client.tls_set()
            client.connect_async(host, port, keepalive=60)
            client.loop_start()

    async def update(self, update: MQTTSettingsUpdate) -> MQTTStatus:
        async with self._update_lock:
            payload = update.model_dump(exclude={"password"})
            config = MQTTSettings(
                **payload,
                password=(
                    update.password
                    if update.password is not None
                    else self.config.password
                ),
                instance_id=self.config.instance_id,
            )
            if config.enabled and not config.broker_address:
                raise ValueError("Enter a broker address before enabling MQTT.")
            await asyncio.to_thread(save_settings, self.path, config)
            # Remove retained discovery if disabled or its prefix changes.
            remove_discovery = self.config.home_assistant_discovery and (
                not config.home_assistant_discovery
                or config.discovery_prefix != self.config.discovery_prefix
            )
            await asyncio.to_thread(self._stop, remove_discovery)
            with self._lock:
                self.config = config
            await asyncio.to_thread(self.start)
            return self.status()

    async def close(self) -> None:
        async with self._update_lock:
            await asyncio.to_thread(self._stop)
            if self._vpn_pending:
                self._vpn_pending.cancel()
                try:
                    await asyncio.wrap_future(self._vpn_pending)
                except asyncio.CancelledError:
                    pass

    def _stop(self, remove_discovery: bool = False) -> None:
        with self._lock:
            client = self._client
            if client is None:
                return
            messages = []
            if client.is_connected():
                if remove_discovery:
                    for network_id in self._snapshots:
                        for key in _SENSORS:
                            messages.append(
                                client.publish(
                                    self._discovery_topic(network_id, key),
                                    "",
                                    qos=1,
                                    retain=True,
                                )
                            )
                    for identity, snapshot in self._nas_snapshots.items():
                        for topic in self._nas_discovery_topics(identity, snapshot):
                            messages.append(
                                client.publish(topic, "", qos=1, retain=True)
                            )
                    for identity in self._nas_network_snapshots:
                        for topic in self._nas_network_discovery_topics(identity):
                            messages.append(
                                client.publish(topic, "", qos=1, retain=True)
                            )
                    if self._vpn_snapshot is not None:
                        for topic in self._vpn_discovery_topics():
                            messages.append(
                                client.publish(topic, "", qos=1, retain=True)
                            )
                messages.append(
                    client.publish(
                        f"{self.base_topic}/availability", "offline", qos=1, retain=True
                    )
                )
            self._client = None
            self._status, self._message = "disabled", None
            self._announced.clear()
        # Waiting inside a callback or while holding the lock would deadlock.
        deadline = time.monotonic() + 3
        for message in messages:
            try:
                message.wait_for_publish(timeout=max(0.01, deadline - time.monotonic()))
            except (RuntimeError, ValueError):
                break
        client.disconnect()
        client.loop_stop()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        with self._lock:
            if client is not self._client:
                return
            if reason_code.is_failure:
                self._status, self._message = (
                    "error",
                    "Broker rejected the connection. Check your credentials and broker permissions.",
                )
                return
            self._status, self._message = "connected", None
            self._announced.clear()
            if self._vpn_control:
                client.subscribe(f"{self.base_topic}/vpn/command", qos=0)
            if self.config.home_assistant_discovery:
                client.subscribe(f"{self.config.discovery_prefix}/status", qos=1)
            self._publish_snapshot()

    def _on_connect_fail(self, client, _userdata) -> None:
        with self._lock:
            if client is self._client:
                self._status, self._message = (
                    "error",
                    "Cannot reach the broker. Check its address, port and TLS settings. Retrying automatically.",
                )

    def _on_disconnect(
        self, client, _userdata, _flags, _reason_code, _properties
    ) -> None:
        with self._lock:
            if client is self._client:
                if self._status == "error":
                    return
                self._status, self._message = (
                    "connecting",
                    "Connection lost. Retrying automatically.",
                )

    def _on_message(self, client, _userdata, message) -> None:
        with self._lock:
            if client is not self._client:
                return
            if message.topic == f"{self.base_topic}/vpn/command":
                # A retained command must never run when the publisher reconnects.
                if getattr(message, "retain", False) or message.payload not in (
                    b"connect",
                    b"disconnect",
                ):
                    return
                if (
                    not self._vpn_control
                    or not self._vpn_loop
                    or self._vpn_loop.is_closed()
                ):
                    return
                action: Literal["connect", "disconnect"] = (
                    "connect" if message.payload == b"connect" else "disconnect"
                )
                if self._vpn_busy:
                    self._vpn_result(
                        client, action, False, "A tunnel action is already in progress."
                    )
                    return
                self._vpn_busy = True
                self._publish_snapshot()
                self._vpn_pending = asyncio.run_coroutine_threadsafe(
                    self._run_vpn_command(client, action), self._vpn_loop
                )
                return
            if (
                client is self._client
                and message.topic == f"{self.config.discovery_prefix}/status"
                and message.payload == b"online"
            ):
                self._announced.clear()
                self._publish_snapshot()

    def _vpn_result(self, client, action: str, success: bool, message: str) -> None:
        if client is self._client:
            client.publish(
                f"{self.base_topic}/vpn/result",
                json.dumps({"action": action, "success": success, "message": message}),
                qos=0,
                retain=False,
            )

    async def _run_vpn_command(
        self, client, action: Literal["connect", "disconnect"]
    ) -> None:
        try:
            if self._vpn_control:
                snapshot = await self._vpn_control(action)
                self.publish_vpn(snapshot)
                with self._lock:
                    self._vpn_result(
                        client,
                        action,
                        True,
                        "Tunnel action requested. Check the connection status for its outcome.",
                    )
        except (VPNControlError, VPNReadError) as exc:
            with self._lock:
                self._vpn_result(client, action, False, str(exc))
        except Exception:
            _LOGGER.warning("MQTT VPN control failed")
            with self._lock:
                self._vpn_result(
                    client,
                    action,
                    False,
                    "Could not control the tunnel. Check VPN Connection settings.",
                )
        finally:
            with self._lock:
                self._vpn_busy = False
                if client is self._client:
                    self._publish_snapshot()

    def publish_samples(self, samples: list[Sample]) -> None:
        """Use the collector's normalized samples, including empty device lists."""
        snapshots: dict[str, dict] = {}
        for sample in samples:
            network_id = sample.labels.get("network_id")
            if not network_id:
                continue
            snapshot = snapshots.setdefault(
                network_id,
                {
                    "name": self._snapshots.get(network_id, {}).get(
                        "name", "Eero network"
                    ),
                    "values": {},
                },
            )
            values = snapshot["values"]
            if sample.name == "eero_network_clients_count":
                snapshot["name"] = sample.labels.get("name") or "Eero network"
                # Count only connected devices with a known connection type.
                for connection in ("wired", "wireless"):
                    values[f"{connection}_clients"] = sum(
                        1
                        for device in samples
                        if device.name == "eero_device_connected"
                        and device.labels.get("network_id") == network_id
                        and device.labels.get("connection_type") == connection
                        and device.value == 1
                    )
            elif sample.name in ("eero_speed_download_mbps", "eero_speed_upload_mbps"):
                if math.isfinite(sample.value) and sample.value >= 0:
                    key = (
                        "download_mbps"
                        if sample.name == "eero_speed_download_mbps"
                        else "upload_mbps"
                    )
                    values[key] = sample.value
        with self._lock:
            for network_id, previous in self._snapshots.items():
                snapshots.setdefault(
                    network_id, {"name": previous["name"], "values": {}}
                )
            self._snapshots = snapshots
            self._snapshot_at = time.monotonic()
            if self._client and self._client.is_connected():
                self._publish_snapshot()

    def _network_key(self, network_id: str) -> str:
        return hashlib.sha256(network_id.encode()).hexdigest()[:16]

    def _nas_topic(self, identity: tuple[str, str]) -> str:
        network_id, nas_id = identity
        return f"{self.base_topic}/{self._network_key(network_id)}/nas/{self._network_key(nas_id)}"

    def _nas_entity_key(
        self, identity: tuple[str, str], entity_id: str, kind: str
    ) -> str:
        node = "_".join(self._network_key(part) for part in (*identity, entity_id))
        # Preserve existing volume IDs; disk and volume table indices can overlap.
        return node if kind == "volumes" else f"{node}_{kind}"

    def _nas_discovery_topic(
        self, identity: tuple[str, str], entity_id: str, key: str, kind: str = "volumes"
    ) -> str:
        node = self._nas_entity_key(identity, entity_id, kind)
        return f"{self.config.discovery_prefix}/sensor/eero_ui_{self.config.instance_id}_nas_{node}/{key}/config"

    def _nas_discovery_topics(
        self, identity: tuple[str, str], snapshot: NASSnapshot
    ) -> list[str]:
        return [
            self._nas_discovery_topic(identity, entity.id, key, kind)
            for kind, sensors in _NAS_SENSORS.items()
            for entity in getattr(snapshot, kind)
            for key in sensors
        ]

    def publish_nas(self, networks: dict[str, list[NASSnapshot]]) -> None:
        """Consume the complete background inventory, without NAS credentials/addresses."""
        with self._lock:
            snapshots = {
                (network_id, nas.id): nas
                for network_id, devices in networks.items()
                for nas in devices
            }
            for identity, previous in self._nas_snapshots.items():
                current = snapshots.get(identity)
                # A failed poll has no inventory. Keep identities to announce unknown
                # states and offline availability until the next successful poll.
                if current and current.status != "online":
                    snapshots[identity] = current.model_copy(
                        update={kind: getattr(previous, kind) for kind in _NAS_SENSORS}
                    )
                    continue
                for kind, sensors in _NAS_SENSORS.items():
                    ids = {e.id for e in getattr(current, kind)} if current else set()
                    for entity in getattr(previous, kind):
                        if entity.id not in ids:
                            self._nas_removed_topics.add(
                                f"{self._nas_topic(identity)}/{kind}/{self._network_key(entity.id)}/state"
                            )
                            self._nas_removed_topics.update(
                                self._nas_discovery_topic(
                                    identity, entity.id, key, kind
                                )
                                for key in sensors
                            )
                if current is None:
                    self._nas_removed_topics.add(
                        f"{self._nas_topic(identity)}/availability"
                    )
            self._nas_snapshots = snapshots
            if self.config.enabled and self._client and self._client.is_connected():
                self._publish_snapshot()

    @staticmethod
    def _nas_fresh(snapshot: NASSnapshot) -> bool:
        if snapshot.status != "online" or not snapshot.observed_at or snapshot.stale:
            return False
        try:
            age = (
                datetime.now(UTC) - datetime.fromisoformat(snapshot.observed_at)
            ).total_seconds()
            return 0 <= age < _NAS_EXPIRY
        except (ValueError, TypeError):
            return False

    def _publish_nas_snapshot(self) -> bool:
        client = self._client
        if client is None:
            return False
        for topic in list(self._nas_removed_topics):
            if (
                client.publish(topic, "", qos=1, retain=True).rc
                == mqtt.MQTT_ERR_SUCCESS
            ):
                self._nas_removed_topics.discard(topic)
                self._announced.pop(topic, None)
        available = False
        for identity, snapshot in self._nas_snapshots.items():
            topic = self._nas_topic(identity)
            fresh = self._nas_fresh(snapshot)
            available |= fresh
            client.publish(
                f"{topic}/availability",
                "online" if fresh else "offline",
                qos=1,
                retain=True,
            )
            for kind in _NAS_SENSORS:
                for entity in getattr(snapshot, kind):
                    self._publish_nas_entity(identity, snapshot, entity, kind, fresh)
        return available

    def _publish_nas_entity(
        self,
        identity: tuple[str, str],
        snapshot: NASSnapshot,
        entity: NASDisk | NASStorage,
        kind: str,
        fresh: bool,
    ) -> None:
        client = self._client
        if client is None:
            return
        topic = self._nas_topic(identity)
        state_topic = f"{topic}/{kind}/{self._network_key(entity.id)}/state"
        sensors = _NAS_SENSORS[kind]
        values = entity.model_dump()
        if not fresh:
            values.update({key: None for key in sensors})
            if kind == "disks":
                values["power_on_days"] = None
        values["observed_at"] = snapshot.observed_at
        client.publish(state_topic, json.dumps(values), qos=1, retain=True)
        if not self.config.home_assistant_discovery:
            return
        unique = self._nas_entity_key(identity, entity.id, kind)
        for key, (name, unit, device_class) in sensors.items():
            payload = {
                "name": f"{entity.name} {name}",
                "unique_id": f"eero_ui_{self.config.instance_id}_nas_{unique}_{key}",
                "state_topic": state_topic,
                "value_template": "{{ value_json."
                + key
                + " if value_json."
                + key
                + " is not none else 'None' }}",
                "availability": [
                    {"topic": f"{self.base_topic}/availability"},
                    {"topic": f"{topic}/availability"},
                ],
                "availability_mode": "all",
                "expire_after": _NAS_EXPIRY,
                "icon": "mdi:harddisk",
                "device": {
                    "identifiers": [
                        f"eero_ui_{self.config.instance_id}_nas_{self._network_key(identity[0])}_{self._network_key(identity[1])}"
                    ],
                    "name": snapshot.name,
                    "manufacturer": "QNAP",
                    "model": snapshot.model or "NAS",
                },
            }
            if unit:
                payload.update(unit_of_measurement=unit, state_class="measurement")
            if device_class:
                payload["device_class"] = device_class
            discovery_topic = self._nas_discovery_topic(identity, entity.id, key, kind)
            encoded = json.dumps(payload)
            if self._announced.get(discovery_topic) != encoded:
                if (
                    client.publish(discovery_topic, encoded, qos=1, retain=True).rc
                    == mqtt.MQTT_ERR_SUCCESS
                ):
                    self._announced[discovery_topic] = encoded

    def _discovery_topic(self, network_id: str, key: str) -> str:
        return f"{self.config.discovery_prefix}/sensor/eero_ui_{self.config.instance_id}_{self._network_key(network_id)}/{key}/config"

    def _nas_network_discovery_topics(self, identity: tuple[str, str]) -> list[str]:
        return [
            self._nas_discovery_topic(identity, "network", key, "network")
            for key in ("receive_mbps", "transmit_mbps")
        ]

    def publish_nas_network(
        self, networks: dict[str, list[NASNetworkSnapshot]]
    ) -> None:
        with self._lock:
            snapshots = {
                (network, nas.id): nas
                for network, devices in networks.items()
                for nas in devices
            }
            for identity in self._nas_network_snapshots.keys() - snapshots.keys():
                self._nas_removed_topics.update(
                    self._nas_network_discovery_topics(identity)
                )
                topic = f"{self._nas_topic(identity)}/network"
                self._nas_removed_topics.update(
                    (f"{topic}/state", f"{topic}/availability")
                )
            self._nas_network_snapshots = snapshots
            if self.config.enabled and self._client and self._client.is_connected():
                self._publish_snapshot()

    def _publish_nas_network_snapshot(self) -> bool:
        client = self._client
        if client is None:
            return False
        available = False
        for identity, snapshot in self._nas_network_snapshots.items():
            fresh = False
            try:
                age = (
                    datetime.now(UTC) - datetime.fromisoformat(snapshot.observed_at)
                ).total_seconds()
                fresh = 0 <= age < NAS_NETWORK_EXPIRY
            except (ValueError, TypeError):
                pass
            available |= fresh
            topic = f"{self._nas_topic(identity)}/network"
            client.publish(
                f"{topic}/availability",
                "online" if fresh else "offline",
                qos=1,
                retain=True,
            )
            values = snapshot.model_dump(exclude={"id", "name"})
            if not fresh:
                values.update(receive_mbps=None, transmit_mbps=None)
            client.publish(f"{topic}/state", json.dumps(values), qos=1, retain=True)
            if not self.config.home_assistant_discovery:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", snapshot.name.lower()).strip("_")
            for key, direction, label in (
                ("receive_mbps", "receive", "Network receive"),
                ("transmit_mbps", "transmit", "Network transmit"),
            ):
                unique = self._nas_entity_key(identity, "network", "network")
                payload = {
                    "name": label,
                    "unique_id": f"eero_ui_{self.config.instance_id}_nas_{unique}_{key}",
                    "default_entity_id": f"sensor.{slug}_network_{direction}",
                    "state_topic": f"{topic}/state",
                    "value_template": "{{ value_json."
                    + key
                    + " if value_json."
                    + key
                    + " is not none else 'None' }}",
                    "unit_of_measurement": "Mbit/s",
                    "device_class": "data_rate",
                    "state_class": "measurement",
                    "suggested_display_precision": 2,
                    "icon": (
                        "mdi:download-network"
                        if direction == "receive"
                        else "mdi:upload-network"
                    ),
                    "availability": [
                        {"topic": f"{self.base_topic}/availability"},
                        {"topic": f"{topic}/availability"},
                    ],
                    "availability_mode": "all",
                    "expire_after": NAS_NETWORK_EXPIRY,
                    "device": {
                        "identifiers": [
                            f"eero_ui_{self.config.instance_id}_nas_{self._network_key(identity[0])}_{self._network_key(identity[1])}"
                        ],
                        "name": snapshot.name,
                        "manufacturer": "QNAP",
                    },
                }
                discovery_topic = self._nas_discovery_topic(
                    identity, "network", key, "network"
                )
                encoded = json.dumps(payload)
                if self._announced.get(discovery_topic) != encoded:
                    if (
                        client.publish(discovery_topic, encoded, qos=1, retain=True).rc
                        == mqtt.MQTT_ERR_SUCCESS
                    ):
                        self._announced[discovery_topic] = encoded
        return available

    def _vpn_discovery_topics(self) -> list[str]:
        node = f"eero_ui_{self.config.instance_id}_vpn"
        return [
            f"{self.config.discovery_prefix}/{kind}/{node}/{key}/config"
            for kind, keys in (
                ("sensor", _VPN_SENSORS),
                ("button", ("connect", "disconnect")),
            )
            for key in keys
        ]

    def _publish_vpn_snapshot(self) -> bool:
        client, snapshot = self._client, self._vpn_snapshot
        if client is None or snapshot is None:
            return False
        topic = f"{self.base_topic}/vpn"
        age = (
            (datetime.now(UTC) - snapshot.collected_at).total_seconds()
            if snapshot.collected_at
            else _VPN_EXPIRY
        )
        fresh = (
            (not snapshot.stale or snapshot.status == "unreachable")
            and 0 <= age < _VPN_EXPIRY
            and snapshot.status != "disabled"
        )
        control_available = (
            fresh
            and snapshot.reachable
            and snapshot.control_available
            and bool(self._vpn_control)
            and not self._vpn_busy
        )
        values = {key: getattr(snapshot, key) for key in _VPN_SENSORS}
        if not fresh or snapshot.stale:
            values.update(
                status=(
                    snapshot.status
                    if fresh or snapshot.status == "disabled"
                    else "unknown"
                ),
                connected_clients=None,
                download_mbps=None,
                upload_mbps=None,
            )
        values["collected_at"] = (
            snapshot.collected_at.isoformat() if snapshot.collected_at else None
        )
        client.publish(f"{topic}/state", json.dumps(values), qos=1, retain=True)
        for suffix, available in (
            ("availability", fresh),
            ("control_availability", control_available),
        ):
            client.publish(
                f"{topic}/{suffix}",
                "online" if available else "offline",
                qos=1,
                retain=True,
            )
        if not self.config.home_assistant_discovery:
            return fresh
        node = f"eero_ui_{self.config.instance_id}_vpn"
        for discovery_topic in self._vpn_discovery_topics():
            key = discovery_topic.split("/")[-2]
            button = key in ("connect", "disconnect")
            payload = {
                "name": key.capitalize() if button else _VPN_SENSORS[key][0],
                "unique_id": f"{node}_{key}",
                "availability": [
                    {"topic": f"{self.base_topic}/availability"},
                    {
                        "topic": f"{topic}/{'control_availability' if button else 'availability'}"
                    },
                ],
                "availability_mode": "all",
                "device": {
                    "identifiers": [node],
                    "name": "VPN tunnel",
                    "manufacturer": "OpenWrt",
                    "model": "VPN",
                },
                "icon": "mdi:vpn",
            }
            if button:
                payload.update(
                    command_topic=f"{topic}/command",
                    payload_press=key,
                    qos=0,
                    retain=False,
                )
            else:
                payload.update(
                    state_topic=f"{topic}/state",
                    expire_after=_VPN_EXPIRY,
                    value_template="{{ value_json."
                    + key
                    + " if value_json."
                    + key
                    + " is not none else 'None' }}",
                )
                unit = _VPN_SENSORS[key][1]
                if key != "status":
                    payload["state_class"] = "measurement"
                else:
                    payload.update(
                        device_class="enum",
                        options=[
                            "connected",
                            "disconnected",
                            "unknown",
                            "unreachable",
                            "disabled",
                        ],
                    )
                if unit:
                    payload.update(unit_of_measurement=unit, device_class="data_rate")
            encoded = json.dumps(payload)
            if self._announced.get(discovery_topic) != encoded:
                if (
                    client.publish(discovery_topic, encoded, qos=1, retain=True).rc
                    == mqtt.MQTT_ERR_SUCCESS
                ):
                    self._announced[discovery_topic] = encoded
        return fresh

    def _publish_snapshot(self) -> None:
        client = self._client
        if client is None:
            return
        fresh = time.monotonic() - self._snapshot_at < self._expiry
        for network_id, snapshot in self._snapshots.items():
            network_key = self._network_key(network_id)
            topic = f"{self.base_topic}/{network_key}"
            for key, (name, icon, unit) in _SENSORS.items():
                if self.config.home_assistant_discovery:
                    payload = {
                        "name": name,
                        "unique_id": f"eero_ui_{self.config.instance_id}_{network_key}_{key}",
                        "state_topic": f"{topic}/{key}/state",
                        "availability_topic": f"{self.base_topic}/availability",
                        "expire_after": self._expiry,
                        "state_class": "measurement",
                        "icon": icon,
                        "device": {
                            "identifiers": [
                                f"eero_ui_{self.config.instance_id}_{network_key}"
                            ],
                            "name": snapshot["name"],
                            "manufacturer": "eero",
                            "model": "Eero network",
                        },
                    }
                    if unit:
                        payload.update(
                            unit_of_measurement=unit,
                            device_class="data_rate",
                            suggested_display_precision=1,
                        )
                    discovery_topic = self._discovery_topic(network_id, key)
                    encoded = json.dumps(payload)
                    if self._announced.get(discovery_topic) != encoded:
                        info = client.publish(
                            discovery_topic, encoded, qos=1, retain=True
                        )
                        if info.rc == mqtt.MQTT_ERR_SUCCESS:
                            self._announced[discovery_topic] = encoded
                value = snapshot["values"].get(key) if fresh else None
                # HA's numeric sensors interpret 'None' as unknown, never as 0.
                client.publish(
                    f"{topic}/{key}/state",
                    "None" if value is None else str(value),
                    qos=1,
                    retain=True,
                )
        available = fresh and any(
            snapshot["values"] for snapshot in self._snapshots.values()
        )
        available = self._publish_nas_snapshot() or available
        available = self._publish_nas_network_snapshot() or available
        available = self._publish_vpn_snapshot() or available
        client.publish(
            f"{self.base_topic}/availability",
            "online" if available else "offline",
            qos=1,
            retain=True,
        )
