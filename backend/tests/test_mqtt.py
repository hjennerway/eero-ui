"""MQTT discovery, state accuracy, reconnection, persistence and API protection."""

import json
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr, ValidationError

from app.main import app
from app.routes.mqtt import get_mqtt_service
from app.services.mqtt import (
    MQTTService,
    MQTTSettingsUpdate,
    load_settings,
    parse_broker_address,
)
from app.services.nas import NASDisk, NASSnapshot, NASStorage
from app.services.nas_network import NASNetworkSnapshot
from app.services.victoria import Sample


@pytest.fixture
def service(tmp_path):
    return MQTTService(tmp_path / "mqtt.json")


@pytest.fixture
def broker(service, monkeypatch):
    client = MagicMock()
    client.is_connected.return_value = True
    client.publish.return_value.rc = 0
    monkeypatch.setattr("app.services.mqtt.mqtt.Client", lambda *a, **kw: client)
    service.config.enabled = True
    service.config.broker_address = "mqtt://broker:1883"
    service.start()
    service._on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
    client.publish.reset_mock()
    return client


def measurements(network_id="net-1", name="Home"):
    def sample(metric, value, **labels):
        return Sample(metric, value, 1, {"network_id": network_id, **labels})

    return [
        sample("eero_network_clients_count", 3, name=name),
        sample("eero_device_connected", 1, connection_type="wired"),
        sample("eero_device_connected", 1, connection_type="wireless"),
        sample("eero_device_connected", 0, connection_type="wired"),
        sample("eero_device_connected", 1, connection_type="unknown"),
        sample("eero_speed_download_mbps", 931.5),
        sample("eero_speed_upload_mbps", 107.2),
    ]


def published(client):
    return {call.args[0]: call.args[1] for call in client.publish.call_args_list}


def nas_snapshot(**updates):
    return NASSnapshot(
        id="qnap",
        name="My NAS",
        host="192.168.4.2",
        status="online",
        observed_at=datetime.now(UTC).isoformat(),
        volumes=[
            NASStorage(
                id="1",
                name="Data",
                status="Ready",
                health="good",
                capacity_bytes=1000,
                used_bytes=1000,
                free_bytes=0,
                used_percent=100,
            )
        ],
    ).model_copy(update=updates)


def nas_disk(**updates):
    return NASDisk(
        id="1",
        name="Disk 1",
        enclosure_id=0,
        bay=1,
        model="Drive model",
        status="Ready",
        smart_status="GOOD",
        health="good",
        temperature_c=35,
        capacity_bytes=2000,
        power_on_hours=51448,
        power_on_days=2143,
    ).model_copy(update=updates)


def test_nas_network_discovery_direction_expiry_cleanup_and_storage_independence(
    service, broker
):
    snapshot = NASNetworkSnapshot(
        id="qnap",
        name="QNAP TVS-473e",
        observed_at=datetime.now(UTC).isoformat(),
        interfaces=["eth0"],
        receive_mbps=12.5,
        transmit_mbps=0,
    )
    service.publish_nas_network({"home": [snapshot]})
    messages = published(broker)
    configs = [json.loads(v) for k, v in messages.items() if k.endswith("/config")]
    assert len(configs) == 2
    assert {c["default_entity_id"] for c in configs} == {
        "sensor.qnap_tvs_473e_network_receive",
        "sensor.qnap_tvs_473e_network_transmit",
    }
    assert all(
        c["device_class"] == "data_rate"
        and c["unit_of_measurement"] == "Mbit/s"
        and c["expire_after"] == 90
        and c["state_class"] == "measurement"
        for c in configs
    )
    topic = next(k for k in messages if k.endswith("/network/state"))
    assert json.loads(messages[topic])["transmit_mbps"] == 0
    assert "192.168.4.2" not in json.dumps(messages)
    service.publish_nas({"home": [nas_snapshot()]})
    assert json.loads(published(broker)[topic])["receive_mbps"] == 12.5
    service.publish_nas_network(
        {
            "home": [
                snapshot.model_copy(
                    update={
                        "observed_at": (
                            datetime.now(UTC) - timedelta(seconds=91)
                        ).isoformat()
                    }
                )
            ]
        }
    )
    assert json.loads(published(broker)[topic])["receive_mbps"] is None
    assert published(broker)[topic.replace("/state", "/availability")] == "offline"
    service.publish_nas_network({})
    assert published(broker)[topic] == ""
    assert all(published(broker)[k] == "" for k in messages if k.endswith("/config"))


def test_nas_volumes_publish_retained_json_and_six_sensors_without_host(
    service, broker
):
    service.publish_nas({"home": [nas_snapshot()]})
    messages = published(broker)
    configs = [json.loads(v) for k, v in messages.items() if k.endswith("/config")]
    assert len(configs) == 6
    state = next(json.loads(v) for k, v in messages.items() if k.endswith("/state"))
    assert state["free_bytes"] == 0 and state["used_percent"] == 100
    assert state["observed_at"] and state["health"] == "good"
    assert "192.168.4.2" not in json.dumps(messages)
    assert all(c["expire_after"] == 900 for c in configs)
    assert messages[f"{service.base_topic}/availability"] == "online"
    assert all(
        call.kwargs == {"qos": 1, "retain": True}
        for call in broker.publish.call_args_list
    )
    # Existing volume discovery identities must survive the addition of disk sensors.
    node = "_".join(service._network_key(part) for part in ("home", "qnap", "1"))
    prefix = f"eero_ui_{service.config.instance_id}_nas_{node}"
    assert all(c["unique_id"].startswith(prefix + "_") for c in configs)
    assert f"homeassistant/sensor/{prefix}/health/config" in messages


@pytest.mark.parametrize("hours", [51448, 0, None])
def test_nas_disks_publish_health_and_exact_power_on_hours(service, broker, hours):
    disk = nas_disk(power_on_hours=hours, ssd_life_remaining_percent=0)
    snapshot = nas_snapshot(disks=[disk], volumes=[])
    service.publish_nas({"home": [snapshot]})
    messages = published(broker)
    state_topic = next(k for k in messages if "/disks/" in k)
    assert json.loads(messages[state_topic]) == {
        **disk.model_dump(),
        "observed_at": snapshot.observed_at,
    }
    configs = {
        k.split("/")[-2]: json.loads(v)
        for k, v in messages.items()
        if k.endswith("/config")
    }
    assert set(configs) == {
        "health",
        "smart_status",
        "status",
        "power_on_hours",
        "temperature_c",
        "capacity_bytes",
        "ssd_life_remaining_percent",
    }
    assert configs["power_on_hours"]["unit_of_measurement"] == "h"
    assert configs["power_on_hours"]["device_class"] == "duration"
    assert configs["temperature_c"]["unit_of_measurement"] == "°C"
    assert configs["temperature_c"]["device_class"] == "temperature"
    for key, config in configs.items():
        assert config["state_topic"] == state_topic
        assert config["value_template"] == (
            f"{{{{ value_json.{key} if value_json.{key} is not none else 'None' }}}}"
        )
        assert config["expire_after"] == 900
        assert config["availability_mode"] == "all"
        assert len(config["availability"]) == 2
    assert "192.168.4.2" not in json.dumps(messages)
    assert messages[f"{service.base_topic}/availability"] == "online"
    assert all(
        call.kwargs == {"qos": 1, "retain": True}
        for call in broker.publish.call_args_list
    )


def test_all_volumes_publish_total_available_and_used_percentage(service, broker):
    volumes = [
        nas_snapshot().volumes[0],
        NASStorage(
            id="2",
            name="Backups",
            capacity_bytes=8000,
            free_bytes=6000,
            used_bytes=2000,
            used_percent=25,
        ),
        NASStorage(id="3", name="Unavailable"),
    ]
    service.publish_nas({"home": [nas_snapshot(volumes=volumes)]})
    messages = published(broker)
    states = {
        state["id"]: state
        for k, v in messages.items()
        if k.endswith("/state")
        for state in [json.loads(v)]
    }
    assert set(states) == {"1", "2", "3"}
    for volume in volumes:
        for key, value in volume.model_dump().items():
            assert states[volume.id][key] == value
    configs = [json.loads(v) for k, v in messages.items() if k.endswith("/config")]
    assert len(configs) == 18
    for config in configs:
        key = config["unique_id"].split("_")[-2:]
        if key[-1] == "bytes":
            assert config["unit_of_measurement"] == "B"
            assert config["device_class"] == "data_size"
        elif key == ["used", "percent"]:
            assert config["unit_of_measurement"] == "%"


def test_nas_identities_do_not_collide_and_reconnect_expires_old_readings(
    service, broker
):
    snapshot = nas_snapshot(disks=[nas_disk(), nas_disk(id="2", name="Disk 2")])
    devices = [snapshot, snapshot.model_copy(update={"id": "other-nas"})]
    service.publish_nas({"home": devices, "office": devices})
    configs = [
        json.loads(v) for k, v in published(broker).items() if k.endswith("/config")
    ]
    assert len({c["unique_id"] for c in configs}) == 80
    assert len({c["state_topic"] for c in configs}) == 12
    assert len({c["device"]["identifiers"][0] for c in configs}) == 4
    old = [
        device.model_copy(
            update={
                "observed_at": (datetime.now(UTC) - timedelta(seconds=901)).isoformat()
            }
        )
        for device in devices
    ]
    service.publish_nas({"home": old, "office": old})
    broker.publish.reset_mock()
    service._on_connect(broker, None, None, SimpleNamespace(is_failure=False), None)
    messages = published(broker)
    assert len([k for k in messages if k.endswith("/config")]) == 80
    assert all(
        v == "offline" for k, v in messages.items() if k.endswith("/availability")
    )
    assert all(
        json.loads(v)["capacity_bytes"] is None
        for k, v in messages.items()
        if k.endswith("/state")
    )
    for topic, value in messages.items():
        if "/disks/" in topic:
            state = json.loads(value)
            assert all(
                state[key] is None
                for key in (
                    "health",
                    "smart_status",
                    "status",
                    "power_on_hours",
                    "power_on_days",
                    "temperature_c",
                    "capacity_bytes",
                    "ssd_life_remaining_percent",
                )
            )


def test_nas_unreachable_preserves_entities_as_unavailable_and_removal_cleans_topics(
    service, broker
):
    snapshot = nas_snapshot(disks=[nas_disk()])
    service.publish_nas({"home": [snapshot]})
    broker.publish.reset_mock()
    service.publish_nas(
        {"home": [nas_snapshot(status="unreachable", volumes=[], observed_at=None)]}
    )
    messages = published(broker)
    assert len([k for k in messages if k.endswith("/state")]) == 2
    assert not any(k.endswith("/config") for k in messages)
    assert all(
        v == "offline" for k, v in messages.items() if k.endswith("/availability")
    )
    assert (
        next(json.loads(v) for k, v in messages.items() if k.endswith("/state"))[
            "capacity_bytes"
        ]
        is None
    )
    service.publish_nas({"home": [snapshot]})
    recovered = published(broker)
    assert all(
        value == "online"
        for key, value in recovered.items()
        if key.endswith("/availability")
    )
    assert (
        next(
            json.loads(value)["power_on_hours"]
            for key, value in recovered.items()
            if "/disks/" in key
        )
        == 51448
    )
    broker.publish.reset_mock()
    service.publish_nas({})
    removed = published(broker)
    assert (
        len([v for k, v in removed.items() if k.endswith("/config") and v == ""]) == 13
    )
    assert len([k for k, v in removed.items() if k.endswith("/state") and v == ""]) == 2


@pytest.mark.parametrize(("kind", "count"), [("disks", 7), ("volumes", 6)])
def test_removed_disk_or_volume_cleans_only_its_topics(service, broker, kind, count):
    snapshot = nas_snapshot(disks=[nas_disk()])
    service.publish_nas({"home": [snapshot]})
    broker.publish.reset_mock()
    service.publish_nas({"home": [snapshot.model_copy(update={kind: []})]})
    messages = published(broker)
    removed = [k for k, v in messages.items() if v == ""]
    assert len([k for k in removed if k.endswith("/config")]) == count
    assert len([k for k in removed if f"/{kind}/" in k]) == 1
    assert messages[f"{service.base_topic}/availability"] == "online"


def test_disks_and_volumes_publish_without_discovery(service, broker):
    service.config.home_assistant_discovery = False
    service.publish_nas({"home": [nas_snapshot(disks=[nas_disk()])]})
    messages = published(broker)
    assert not any(k.endswith("/config") for k in messages)
    assert len([k for k in messages if k.endswith("/state")]) == 2


def test_nas_home_assistant_restart_reannounces_stable_discovery(service, broker):
    snapshot = nas_snapshot(disks=[nas_disk()])
    service.publish_nas({"home": [snapshot]})
    configs = {k: v for k, v in published(broker).items() if k.endswith("/config")}
    broker.publish.reset_mock()
    service.publish_nas({"home": [snapshot]})
    assert not any(k.endswith("/config") for k in published(broker))
    service._on_message(
        broker, None, SimpleNamespace(topic="homeassistant/status", payload=b"online")
    )
    assert {
        k: v for k, v in published(broker).items() if k.endswith("/config")
    } == configs


def test_disabled_mqtt_caches_nas_without_publishing(service):
    service.publish_nas({"home": [nas_snapshot(disks=[nas_disk()])]})
    assert service._client is None and service._nas_snapshots


async def test_turning_discovery_off_removes_nas_configs(service, broker):
    service.publish_nas({"home": [nas_snapshot(disks=[nas_disk()])]})
    broker.publish.reset_mock()
    await service.update(
        MQTTSettingsUpdate(broker_address="broker", home_assistant_discovery=False)
    )
    assert (
        len(
            [
                v
                for k, v in published(broker).items()
                if k.endswith("/config") and v == ""
            ]
        )
        == 13
    )


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("broker", ("broker", 1883, False)),
        ("192.168.1.2:1884", ("192.168.1.2", 1884, False)),
        ("mqtts://broker", ("broker", 8883, True)),
        ("mqtt://[::1]:1883", ("::1", 1883, False)),
    ],
)
def test_broker_addresses(address, expected):
    assert parse_broker_address(address) == expected


@pytest.mark.parametrize(
    "address",
    [
        "http://broker",
        "mqtt://user:secret@broker",
        "broker:0",
        "broker:65536",
        "broker/path",
        "mqtt://",
        "bad host",
    ],
)
def test_invalid_addresses(address):
    with pytest.raises(ValueError):
        parse_broker_address(address)


def test_discovery_and_states_count_only_connected_known_clients(service, broker):
    service.publish_samples(measurements())
    messages = published(broker)
    configs = [
        json.loads(value)
        for topic, value in messages.items()
        if topic.endswith("/config")
    ]
    assert len(configs) == 4
    states = {
        topic.split("/")[-2]: value
        for topic, value in messages.items()
        if topic.endswith("/state")
    }
    assert states == {
        "wired_clients": "1",
        "wireless_clients": "1",
        "download_mbps": "931.5",
        "upload_mbps": "107.2",
    }
    assert messages[f"{service.base_topic}/availability"] == "online"
    assert len({c["unique_id"] for c in configs}) == 4
    assert len({c["device"]["identifiers"][0] for c in configs}) == 1
    for config in configs:
        assert config["expire_after"] == 180
        assert config["state_class"] == "measurement"
        if "speed test" in config["name"]:
            assert config["unit_of_measurement"] == "Mbit/s"
            assert config["device_class"] == "data_rate"
    assert all(
        call.kwargs == {"qos": 1, "retain": True}
        for call in broker.publish.call_args_list
    )


def test_empty_network_is_zero_but_missing_data_is_unknown(service, broker):
    service.publish_samples(
        [
            Sample(
                "eero_network_clients_count",
                0,
                1,
                {"network_id": "net-1", "name": "Home"},
            )
        ]
    )
    states = published(broker)
    assert any(
        topic.endswith("/wired_clients/state") and value == "0"
        for topic, value in states.items()
    )
    assert any(
        topic.endswith("/download_mbps/state") and value == "None"
        for topic, value in states.items()
    )
    broker.publish.reset_mock()
    service.publish_samples([])
    assert published(broker)[f"{service.base_topic}/availability"] == "offline"
    assert all(
        value == "None"
        for topic, value in published(broker).items()
        if topic.endswith("/state")
    )


def test_multiple_networks_do_not_collide_and_names_can_change(service, broker):
    service.publish_samples(measurements() + measurements("net-2", "Office"))
    configs = [
        json.loads(value)
        for topic, value in published(broker).items()
        if topic.endswith("/config")
    ]
    assert len({c["unique_id"] for c in configs}) == 8
    assert {c["device"]["name"] for c in configs} == {"Home", "Office"}
    broker.publish.reset_mock()
    service.publish_samples(measurements(name="Renamed home"))
    configs = [
        json.loads(value)
        for topic, value in published(broker).items()
        if topic.endswith("/config")
    ]
    assert len(configs) == 4
    assert all(c["device"]["name"] == "Renamed home" for c in configs)


def test_home_assistant_birth_and_broker_reconnect_rediscover(service, broker):
    service.publish_samples(measurements())
    unique_ids = {
        json.loads(value)["unique_id"]
        for topic, value in published(broker).items()
        if topic.endswith("/config")
    }
    for announce in (
        lambda: service._on_message(
            broker,
            None,
            SimpleNamespace(topic="homeassistant/status", payload=b"online"),
        ),
        lambda: service._on_connect(
            broker, None, None, SimpleNamespace(is_failure=False), None
        ),
    ):
        broker.publish.reset_mock()
        announce()
        configs = [
            json.loads(value)
            for topic, value in published(broker).items()
            if topic.endswith("/config")
        ]
        assert {c["unique_id"] for c in configs} == unique_ids
    broker.subscribe.assert_called_with("homeassistant/status", qos=1)


def test_expired_cache_not_replayed_as_current_data(service, broker):
    service.publish_samples(measurements())
    service._snapshot_at = time.monotonic() - 181
    broker.publish.reset_mock()
    service._on_connect(broker, None, None, SimpleNamespace(is_failure=False), None)
    assert published(broker)[f"{service.base_topic}/availability"] == "offline"
    assert all(
        value == "None"
        for topic, value in published(broker).items()
        if topic.endswith("/state")
    )


async def test_credentials_persist_preserve_clear_and_never_appear_in_status(service):
    update = MQTTSettingsUpdate(
        broker_address="broker",
        username="mqtt-user",
        password=SecretStr("private-password"),
    )
    status = await service.update(update)
    assert status.password_set
    assert "private-password" not in status.model_dump_json()
    assert "password" not in status.model_dump()
    reopened = MQTTService(service.path)
    assert reopened.config.instance_id == service.config.instance_id
    assert reopened.config.password.get_secret_value() == "private-password"
    await reopened.update(MQTTSettingsUpdate(broker_address="new-broker"))
    assert load_settings(service.path).password.get_secret_value() == "private-password"
    await reopened.update(MQTTSettingsUpdate(password=SecretStr("")))
    assert not reopened.status().password_set
    assert load_settings(service.path).password.get_secret_value() == ""
    if os.name == "posix":
        assert service.path.stat().st_mode & 0o777 == 0o600


async def test_turning_discovery_off_removes_retained_configs(service, broker):
    service.publish_samples(measurements())
    broker.publish.reset_mock()
    await service.update(
        MQTTSettingsUpdate(broker_address="broker", home_assistant_discovery=False)
    )
    removed = [
        value for topic, value in published(broker).items() if topic.endswith("/config")
    ]
    assert removed == [""] * 4
    assert published(broker)[f"{service.base_topic}/availability"] == "offline"
    broker.disconnect.assert_called_once()
    broker.loop_stop.assert_called_once()


def test_start_sets_auth_tls_will_and_errors_are_safe(service, monkeypatch):
    broker = MagicMock()
    monkeypatch.setattr("app.services.mqtt.mqtt.Client", lambda *a, **kw: broker)
    service.config.enabled = True
    service.config.broker_address = "mqtts://broker"
    service.config.username = "mqtt-user"
    service.config.password = SecretStr("private-password")
    service.start()
    broker.tls_set.assert_called_once()
    broker.username_pw_set.assert_called_with("mqtt-user", "private-password")
    broker.connect_async.assert_called_with("broker", 8883, keepalive=60)
    broker.will_set.assert_called_with(
        f"{service.base_topic}/availability", "offline", qos=1, retain=True
    )
    service._on_connect(broker, None, None, SimpleNamespace(is_failure=True), None)
    assert service.status().connection_status == "error"
    assert "private-password" not in service.status().model_dump_json()
    service._on_connect_fail(broker, None)
    assert "Retrying" in service.status().message


def test_discovery_prefix_rejects_wildcards():
    with pytest.raises(ValidationError):
        MQTTSettingsUpdate(discovery_prefix="homeassistant/#")


@pytest.fixture
def api_service(service):
    app.dependency_overrides[get_mqtt_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_mqtt_service, None)


async def test_mqtt_api_requires_auth(async_client, api_service):
    assert (await async_client.get("/api/mqtt")).status_code == 401
    assert (await async_client.put("/api/mqtt", json={})).status_code == 401


async def test_mqtt_api_saves_secret_without_returning_it(auth_client, api_service):
    response = await auth_client.put(
        "/api/mqtt", json={"broker_address": "broker", "password": "private-password"}
    )
    assert response.status_code == 200
    assert response.json()["password_set"]
    assert "private-password" not in response.text
    assert "password" not in response.json()
    response = await auth_client.get("/api/mqtt")
    assert response.status_code == 200
    assert "private-password" not in response.text


async def test_mqtt_api_rejects_empty_enabled_broker_and_csrf(auth_client, api_service):
    response = await auth_client.put(
        "/api/mqtt", json={"enabled": True, "password": "private-password"}
    )
    assert response.status_code == 422
    assert "private-password" not in response.text
    response = await auth_client.put(
        "/api/mqtt", json={}, headers={"X-Requested-With": ""}
    )
    assert response.status_code == 403


async def test_validation_errors_do_not_echo_credentials(auth_client, api_service):
    for payload in (
        {"password": {"secret": "private-password"}},
        {"password": "private-password" * 400},
        {"broker_address": "mqtt://user:private-password@broker"},
    ):
        response = await auth_client.put("/api/mqtt", json=payload)
        assert response.status_code == 422
        assert "private-password" not in response.text


async def test_save_failure_keeps_previous_config(
    auth_client, api_service, monkeypatch
):
    def fail_save(*args):
        raise OSError("private-password")

    monkeypatch.setattr("app.services.mqtt.save_settings", fail_save)
    response = await auth_client.put("/api/mqtt", json={"broker_address": "broker"})
    assert response.status_code == 503
    assert "private-password" not in response.text
    assert api_service.config.broker_address == ""
