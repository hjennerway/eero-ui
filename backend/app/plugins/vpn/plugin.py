"""VPN plugin registration and worker lifecycle."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from ...config import settings
from ...services.victoria import victoria_client
from .. import Plugin, stop_task
from .mqtt import VPNMQTT
from .routes import router
from .vpn import VPNService


@asynccontextmanager
async def lifespan(app):
    mqtt = app.state.mqtt_service
    publisher = VPNMQTT(mqtt) if mqtt else None
    if mqtt:
        mqtt.add_extension("vpn", publisher)
    app.state.vpn = VPNService(
        (
            Path(settings.vpn_file)
            if settings.vpn_file
            else Path(settings.cookie_file).parent / "vpn.json"
        ),
        victoria_client,
        publish_snapshot=publisher.publish_vpn if publisher else None,
    )
    if publisher:
        publisher.set_vpn_controller(app.state.vpn.control)
    task = asyncio.create_task(app.state.vpn.run_forever())
    try:
        yield
    finally:
        await stop_task(task)
        if publisher:
            await publisher.aclose()
        app.state.vpn = None


plugin = Plugin(id="vpn", router=router, lifespan=lifespan)
