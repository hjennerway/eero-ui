"""Discover trusted, bundled plugins without importing disabled integrations."""

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI

from ..deps import require_auth


@dataclass(frozen=True)
class Plugin:
    id: str
    router: APIRouter
    lifespan: Callable
    api_prefix: str | None = None


def load_plugins(names: list[str]) -> tuple[Plugin, ...]:
    """Only load installed packages, never an arbitrary module from configuration."""
    installed = {
        path.name
        for path in Path(__file__).parent.iterdir()
        if path.is_dir() and (path / "plugin.py").is_file()
    }
    unknown = set(names) - installed
    if unknown:
        raise ValueError(f"Unknown dashboard plugins: {', '.join(sorted(unknown))}")
    plugins = tuple(
        import_module(f"{__name__}.{name}.plugin").plugin
        for name in dict.fromkeys(names)
    )
    if any(plugin.id != name for plugin, name in zip(plugins, dict.fromkeys(names))):
        raise ValueError("Plugin package and manifest IDs must match")
    return plugins


def install_plugins(app: FastAPI, plugins: tuple[Plugin, ...]) -> None:
    app.state.plugins = plugins
    app.state.plugin_endpoints = {
        route.endpoint
        for plugin in plugins
        for route in plugin.router.routes
        if hasattr(route, "endpoint")
    }
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.get("")
    async def list_plugins():
        return [{"id": plugin.id} for plugin in plugins]

    app.include_router(router, prefix="/api/plugins", tags=["Plugins"])
    for plugin in plugins:
        app.include_router(
            plugin.router,
            prefix=plugin.api_prefix or f"/api/{plugin.id}",
            tags=[plugin.id.upper()],
        )


@asynccontextmanager
async def plugin_lifespan(app: FastAPI):
    # ExitStack unwinds already-started plugins if a later startup fails.
    async with AsyncExitStack() as stack:
        for plugin in app.state.plugins:
            await stack.enter_async_context(plugin.lifespan(app))
        yield


async def stop_task(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
