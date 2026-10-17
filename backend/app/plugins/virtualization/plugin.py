"""Virtualization inventory registration and worker lifecycle."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from ...config import settings
from ...deps import get_eero_client
from .. import Plugin, stop_task
from .routes import router
from .virtualization import VirtualizationService


@asynccontextmanager
async def lifespan(app):
    inventory = VirtualizationService(
        Path(settings.cookie_file).parent / "virtualization", get_eero_client
    )
    async with AsyncExitStack() as stack:
        stack.push_async_callback(inventory.aclose)
        inventory.load()
        app.state.virtualization = inventory
        stack.push_async_callback(
            stop_task, asyncio.create_task(inventory.run_forever())
        )
        try:
            yield
        finally:
            app.state.virtualization = None


plugin = Plugin(
    id="virtualization", router=router, lifespan=lifespan, api_prefix="/api/networks"
)
