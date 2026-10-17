"""VPN MQTT state, Home Assistant discovery and bounded command dispatch."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.plugins.vpn.mqtt import VPNMQTT
from app.plugins.vpn.vpn import VPNControlError, VPNSnapshot
from app.services.mqtt import MQTTService, MQTTSettingsUpdate


def snapshot(**updates):
    return VPNSnapshot(
        status="connected",
        reachable=True,
        stale=False,
        collected_at=datetime.now(UTC),
        control_available=True,
        connected_clients=2,
        download_mbps=12.5,
        upload_mbps=0,
    ).model_copy(update=updates)


@pytest.fixture
async def connected(tmp_path, monkeypatch):
    service = MQTTService(tmp_path / "mqtt.json")
    service.add_extension("vpn", VPNMQTT(service))
    controller = AsyncMock(return_value=snapshot())
    service.extensions["vpn"].set_vpn_controller(controller)
    client = MagicMock()
    client.is_connected.return_value = True
    client.publish.return_value.rc = 0
    monkeypatch.setattr("app.services.mqtt.mqtt.Client", lambda *args, **kwargs: client)
    service.config.enabled = True
    service.config.broker_address = "broker"
    service.start()
    service._on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
    client.publish.reset_mock()
    yield service, client, controller
    await service.close()


def messages(client):
    return {call.args[0]: call.args[1] for call in client.publish.call_args_list}


def command(service, client, payload=b"connect", retain=False):
    service._on_message(
        client,
        None,
        SimpleNamespace(
            topic=f"{service.base_topic}/vpn/command",
            payload=payload,
            retain=retain,
        ),
    )


async def test_vpn_sensors_buttons_and_zero_throughput_are_published(connected):
    service, client, _ = connected
    service.extensions["vpn"].publish_vpn(snapshot())
    published = messages(client)
    topic = f"{service.base_topic}/vpn"
    state = json.loads(published[f"{topic}/state"])
    assert state["status"] == "connected" and state["upload_mbps"] == 0
    assert state["download_mbps"] == 12.5
    assert "192.168" not in json.dumps(published)
    configs = {
        key.split("/")[-2]: json.loads(value)
        for key, value in published.items()
        if key.endswith("/config")
    }
    assert set(configs) == {
        "status",
        "connected_clients",
        "download_mbps",
        "upload_mbps",
        "connect",
        "disconnect",
    }
    for key in ("download_mbps", "upload_mbps"):
        assert configs[key]["unit_of_measurement"] == "Mbit/s"
        assert configs[key]["device_class"] == "data_rate"
        assert configs[key]["expire_after"] == 90
    for key in ("connect", "disconnect"):
        assert configs[key]["payload_press"] == key
        assert configs[key]["command_topic"] == f"{topic}/command"
        assert configs[key]["retain"] is False and configs[key]["qos"] == 0
    assert published[f"{topic}/availability"] == "online"
    assert published[f"{topic}/control_availability"] == "online"
    assert service.status().integration_topics["vpn"] == topic
    client.subscribe.assert_any_call(f"{topic}/command", qos=0)


async def test_stale_readings_are_cleared_on_reconnect(connected):
    service, client, controller = connected
    service.extensions["vpn"].publish_vpn(
        snapshot(collected_at=datetime.now(UTC) - timedelta(seconds=91))
    )
    service._on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
    published = messages(client)
    topic = f"{service.base_topic}/vpn"
    state = json.loads(published[f"{topic}/state"])
    assert state["status"] == "unknown"
    assert state["download_mbps"] is state["upload_mbps"] is None
    assert published[f"{topic}/availability"] == "offline"
    assert published[f"{topic}/control_availability"] == "offline"
    controller.assert_not_awaited()


async def test_unreachable_status_is_reported_without_inventing_traffic(connected):
    service, client, _ = connected
    service.extensions["vpn"].publish_vpn(
        snapshot(
            status="unreachable",
            reachable=False,
            stale=True,
            control_available=False,
            download_mbps=None,
            upload_mbps=None,
        )
    )
    topic = f"{service.base_topic}/vpn"
    assert json.loads(messages(client)[f"{topic}/state"])["status"] == "unreachable"
    assert messages(client)[f"{topic}/control_availability"] == "offline"


@pytest.mark.parametrize("action", ["connect", "disconnect"])
async def test_commands_dispatch_and_publish_non_retained_results(connected, action):
    service, client, controller = connected
    command(service, client, payload=action.encode())
    await asyncio.wait_for(
        asyncio.wrap_future(service.extensions["vpn"]._vpn_pending), 1
    )
    controller.assert_awaited_once_with(action)
    result = json.loads(messages(client)[f"{service.base_topic}/vpn/result"])
    assert result["action"] == action and result["success"]
    result_call = next(
        call
        for call in client.publish.call_args_list
        if call.args[0].endswith("/result")
    )
    assert result_call.kwargs == {"qos": 0, "retain": False}


@pytest.mark.parametrize(
    "payload,retain",
    [
        (b"connect", True),
        (b"disconnect", True),
        (b"restart", False),
        (b"connect; reboot", False),
        (b"connect" * 1000, False),
    ],
)
async def test_retained_and_invalid_commands_never_dispatch(connected, payload, retain):
    service, client, controller = connected
    command(service, client, payload, retain)
    await asyncio.sleep(0)
    controller.assert_not_awaited()
    assert service.extensions["vpn"]._vpn_pending is None


async def test_busy_commands_are_not_queued_and_errors_are_reported(connected):
    service, client, controller = connected
    started, release = asyncio.Event(), asyncio.Event()

    async def action(_action):
        started.set()
        await release.wait()
        raise VPNControlError("Set the tunnel name in Connection first.")

    controller.side_effect = action
    command(service, client)
    await started.wait()
    command(service, client, b"disconnect")
    result = json.loads(messages(client)[f"{service.base_topic}/vpn/result"])
    assert not result["success"] and "in progress" in result["message"]
    release.set()
    await asyncio.wait_for(
        asyncio.wrap_future(service.extensions["vpn"]._vpn_pending), 1
    )
    assert controller.await_count == 1
    assert not service.extensions["vpn"]._vpn_busy
    result = json.loads(messages(client)[f"{service.base_topic}/vpn/result"])
    assert not result["success"] and "tunnel name" in result["message"]


async def test_discovery_removal_clears_vpn_buttons_and_sensors(connected):
    service, client, _ = connected
    service.extensions["vpn"].publish_vpn(snapshot())
    topics = service.extensions["vpn"]._vpn_discovery_topics()
    client.publish.reset_mock()
    await service.update(
        MQTTSettingsUpdate(broker_address="broker", home_assistant_discovery=False)
    )
    assert all(messages(client)[topic] == "" for topic in topics)


async def test_discovery_disabled_still_publishes_state_and_accepts_commands(connected):
    service, client, controller = connected
    service.config.home_assistant_discovery = False
    service.extensions["vpn"].publish_vpn(snapshot())
    assert not any(topic.endswith("/config") for topic in messages(client))
    command(service, client, b"disconnect")
    await asyncio.wait_for(
        asyncio.wrap_future(service.extensions["vpn"]._vpn_pending), 1
    )
    controller.assert_awaited_once_with("disconnect")


async def test_shutdown_cancels_pending_control(connected):
    service, client, controller = connected
    started = asyncio.Event()

    async def waiting(_action):
        started.set()
        await asyncio.Event().wait()

    controller.side_effect = waiting
    command(service, client)
    await started.wait()
    await service.close()
    assert service.extensions["vpn"]._vpn_pending.cancelled()
