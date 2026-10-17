"""VPN-owned MQTT sensors, buttons and command handling."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from typing import Literal

import paho.mqtt.client as mqtt

from ...services.mqtt_extension import MQTTIntegration
from .vpn import VPNControlError, VPNReadError, VPNSnapshot

_LOGGER = logging.getLogger(__name__)
_VPN_SENSORS = {
    "status": ("Connection status", None),
    "connected_clients": ("Active clients", None),
    "download_mbps": ("Download throughput", "Mbit/s"),
    "upload_mbps": ("Upload throughput", "Mbit/s"),
}
_VPN_EXPIRY = 90


class VPNMQTT(MQTTIntegration):
    def __init__(self, service):
        super().__init__(service, "vpn")
        self._vpn_snapshot = None
        self._vpn_control = None
        self._vpn_loop = None
        self._vpn_pending: Future | None = None
        self._vpn_busy = False

    def discovery_topics(self):
        return self._vpn_discovery_topics() if self._vpn_snapshot is not None else []

    def on_connect(self, client):
        if self._vpn_control:
            client.subscribe(f"{self.base_topic}/vpn/command", qos=0)

    def on_message(self, client, message):
        if message.topic == f"{self.base_topic}/vpn/command":
            # A retained command must never run when the publisher reconnects.
            if getattr(message, "retain", False) or message.payload not in (
                b"connect",
                b"disconnect",
            ):
                return True
            if (
                not self._vpn_control
                or not self._vpn_loop
                or self._vpn_loop.is_closed()
            ):
                return True
            action: Literal["connect", "disconnect"] = (
                "connect" if message.payload == b"connect" else "disconnect"
            )
            if self._vpn_busy:
                self._vpn_result(
                    client, action, False, "A tunnel action is already in progress."
                )
                return True
            self._vpn_busy = True
            self._publish_snapshot()
            self._vpn_pending = asyncio.run_coroutine_threadsafe(
                self._run_vpn_command(client, action), self._vpn_loop
            )
            return True
        return False

    def publish(self):
        return self._publish_vpn_snapshot()

    async def aclose(self):
        if self._vpn_pending:
            self._vpn_pending.cancel()
            try:
                await asyncio.wrap_future(self._vpn_pending)
            except asyncio.CancelledError:
                pass

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
