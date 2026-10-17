"""Stable MQTT hooks for optional integrations; the broker remains a core service."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mqtt import MQTTService


class MQTTIntegration:
    """Adapters run under the broker's lock and share its discovery cache."""

    def __init__(self, service: "MQTTService", name: str):
        self.service = service
        self.name = name

    @property
    def topic(self):
        return f"{self.base_topic}/{self.name}"

    @property
    def config(self):
        return self.service.config

    @property
    def base_topic(self):
        return self.service.base_topic

    @property
    def _lock(self):
        return self.service._lock

    @property
    def _client(self):
        return self.service._client

    @property
    def _announced(self):
        return self.service._announced

    def _publish_snapshot(self):
        self.service._publish_snapshot()

    def _network_key(self, identity):
        return self.service._network_key(identity)

    def discovery_topics(self):
        return ()

    def on_connect(self, client):
        pass

    def on_message(self, client, message) -> bool:
        return False

    def publish(self) -> bool:
        return False

    async def aclose(self):
        pass
