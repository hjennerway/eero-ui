"""NAS plugin registration and worker lifecycle."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

from ...config import settings
from .. import Plugin, stop_task
from .mqtt import NASMQTT
from .nas import nas_inventory
from .nas_network import NASNetworkCollector
from .routes import router


@asynccontextmanager
async def lifespan(app):
    mqtt = app.state.mqtt_service
    publisher = NASMQTT(mqtt) if mqtt else None
    if mqtt:
        mqtt.add_extension("nas", publisher)
    async with AsyncExitStack() as stack:
        # Callbacks run in reverse: stop both workers, then release SNMP clients.
        stack.push_async_callback(nas_inventory.close)
        network = NASNetworkCollector()
        for worker in (
            nas_inventory.run_forever(
                settings.nas_file, publisher.publish_nas if publisher else None
            ),
            network.run_forever(
                settings.nas_file, publisher.publish_nas_network if publisher else None
            ),
        ):
            stack.push_async_callback(stop_task, asyncio.create_task(worker))
        yield


plugin = Plugin(id="nas", router=router, lifespan=lifespan)
