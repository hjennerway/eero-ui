"""NAS-owned MQTT state and Home Assistant discovery."""

import json
import re
from datetime import UTC, datetime

import paho.mqtt.client as mqtt

from ...services.mqtt_extension import MQTTIntegration
from .nas import REFRESH_SECONDS, NASDisk, NASSnapshot, NASStorage
from .nas_network import EXPIRY_SECONDS as NAS_NETWORK_EXPIRY
from .nas_network import NASNetworkSnapshot

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


class NASMQTT(MQTTIntegration):
    def __init__(self, service):
        super().__init__(service, "nas")
        self._nas_snapshots = {}
        self._nas_network_snapshots = {}
        self._nas_removed_topics = set()

    def discovery_topics(self):
        for identity, snapshot in self._nas_snapshots.items():
            yield from self._nas_discovery_topics(identity, snapshot)
        for identity in self._nas_network_snapshots:
            yield from self._nas_network_discovery_topics(identity)

    def publish(self):
        storage = self._publish_nas_snapshot()
        network = self._publish_nas_network_snapshot()
        return storage or network

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
