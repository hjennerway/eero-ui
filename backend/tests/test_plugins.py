"""Plugin availability, import isolation, auth and lifecycle rollback."""

import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.deps import require_auth
from app.plugins import Plugin, install_plugins, load_plugins, plugin_lifespan
from app.services.mqtt import MQTTService
from app.services.victoria import Sample


@pytest.mark.parametrize(
    "configured, expected", [("", []), ("vpn", ["vpn"]), (" nas, vpn ", ["nas", "vpn"])]
)
def test_plugin_configuration(monkeypatch, configured, expected):
    monkeypatch.setenv("EERO_DASHBOARD_PLUGINS", configured)
    assert get_settings().plugins == expected


def test_unknown_plugin_is_rejected_without_importing_arbitrary_modules():
    with pytest.raises(ValueError, match="Unknown dashboard plugins"):
        load_plugins(["os.path"])


def test_core_only_startup_does_not_import_integrations(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from app.main import app
assert app.state.plugins == ()
assert not any(name.startswith(('app.plugins.nas', 'app.plugins.vpn', 'app.plugins.virtualization')) for name in sys.modules)
from fastapi.testclient import TestClient
with TestClient(app) as client:
    assert client.get('/api/health').status_code == 200
    assert app.state.metrics_collector is not None
    assert app.state.mqtt_service is not None
    assert app.state.mqtt_service.extensions == {}
    assert client.get('/api/nas').status_code == 404
    assert client.get('/api/vpn').status_code == 404
    assert client.get('/api/networks/home/virtualization').status_code == 404
    assert not hasattr(app.state, 'virtualization')
""",
        ],
        env={
            **os.environ,
            "EERO_DASHBOARD_PLUGINS": "",
            "EERO_DASHBOARD_COOKIE_FILE": str(tmp_path / "session.json"),
            "EERO_DASHBOARD_MQTT_FILE": str(tmp_path / "mqtt.json"),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


async def test_discovery_requires_auth_and_only_lists_enabled_plugins():
    app = FastAPI()
    install_plugins(app, load_plugins(["vpn", "vpn"]))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/plugins")).status_code == 401
        app.dependency_overrides[require_auth] = lambda: True
        assert (await client.get("/api/plugins")).json() == [{"id": "vpn"}]
        assert (await client.get("/api/nas")).status_code == 404
        assert (
            await client.get("/api/networks/home/virtualization")
        ).status_code == 404


async def test_plugins_unwind_when_later_startup_fails():
    events = []

    @asynccontextmanager
    async def first(_app):
        events.append("started")
        try:
            yield
        finally:
            events.append("stopped")

    @asynccontextmanager
    async def broken(_app):
        raise RuntimeError("startup failed")
        yield  # pragma: no cover

    app = FastAPI()
    install_plugins(
        app,
        (Plugin("first", APIRouter(), first), Plugin("broken", APIRouter(), broken)),
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        async with plugin_lifespan(app):
            pytest.fail("Startup should fail before yielding")
    assert events == ["started", "stopped"]


def test_wan_mqtt_publishing_works_without_plugins(tmp_path):
    service = MQTTService(tmp_path / "mqtt.json")
    client = MagicMock()
    client.is_connected.return_value = True
    client.publish.return_value.rc = 0
    service._client = client
    timestamp = int(time.time() * 1000)
    service.publish_samples(
        [
            Sample(
                "eero_network_clients_count",
                1,
                timestamp,
                {"network_id": "home", "name": "Home"},
            ),
            Sample(
                "eero_network_wan_download_mbps",
                12.5,
                timestamp,
                {"network_id": "home"},
            ),
        ]
    )
    assert service.extensions == {}
    states = {call.args[0]: call.args[1] for call in client.publish.call_args_list}
    topic = (
        f"{service.base_topic}/{service._network_key('home')}/wan_download_mbps/state"
    )
    assert states[topic] == "12.5"
