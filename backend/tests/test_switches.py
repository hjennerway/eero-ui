"""Switch evidence, network isolation and credential boundary regressions."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from pysnmp.proto.rfc1902 import OctetString

from app.config import settings
from app.services import switches
from app.services.switches import (
    SwitchConfig,
    SwitchInventory,
    SwitchSnapshot,
    normalize_ports,
)


def config():
    return dict(
        id="office",
        name="Office switch",
        network_id="network-123",
        host="192.168.1.10",
        community="test-secret",
    )


def test_joins_bridge_and_lldp_ids_without_assuming_ifindex():
    mac = (2, 0, 0, 0, 0, 1)
    neighbor = (123, 7, 1)
    tables = {
        "if_type": {(101,): 6, (102,): 6, (999,): 6},
        "if_name": {(101,): OctetString("g1"), (102,): OctetString("g2")},
        "if_descr": {(999,): OctetString("CPU interface")},
        "if_status": {(101,): 1, (102,): 2},
        "if_high_speed": {(101,): 1000, (102,): 1000},
        "bridge_port": {(5,): 101},
        "qfdb_port": {(42, *mac): 5, (43, *mac): 5, (44, *mac): 0},
        "qfdb_status": {(42, *mac): 3, (43, *mac): 3, (44, *mac): 3},
        "local_subtype": {(7,): 5},
        "local_port": {(7,): OctetString("g1")},
        "chassis_subtype": {neighbor: 4},
        "chassis": {neighbor: OctetString(bytes(mac))},
        "remote_name": {neighbor: OctetString("Office eero")},
        "remote_port_subtype": {neighbor: 3},
        "remote_port": {neighbor: OctetString(bytes(mac))},
    }
    ports = normalize_ports(tables)
    assert [p.id for p in ports] == [101, 102]
    assert ports[0].macs == ["02:00:00:00:00:01"]
    assert ports[0].speed_mbps == 1000
    assert ports[0].neighbors[0].chassis_mac == "02:00:00:00:00:01"
    assert ports[0].neighbors[0].port == "02:00:00:00:00:01"
    assert ports[1].speed_mbps is None
    tables["local_port"][(7,)] = OctetString("unknown")
    assert normalize_ports(tables)[0].neighbors == []
    tables["local_subtype"][(7,)] = 1
    tables["local_port"][(7,)] = OctetString("uplink")
    tables["if_alias"] = {(101,): OctetString("uplink")}
    assert normalize_ports(tables)[0].neighbors[0].name == "Office eero"
    tables["qfdb_status"] = {}
    assert normalize_ports(tables)[0].macs == []


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "224.0.0.1", "localhost", "::1"]
)
def test_only_explicit_private_ipv4_targets(host):
    with pytest.raises(ValidationError):
        SwitchConfig(**(config() | {"host": host}))


async def test_inventory_scopes_caches_and_redacts(tmp_path, monkeypatch):
    path = tmp_path / "switches.json"
    path.write_text(
        json.dumps([config(), config() | {"id": "other", "network_id": "other"}])
    )
    poll = AsyncMock(
        return_value=SwitchSnapshot(
            id="office", name="Office", host="192.168.1.10", status="online"
        )
    )
    monkeypatch.setattr(switches, "poll_switch", poll)
    inventory = SwitchInventory()
    first = await inventory.read(str(path), "network-123")
    assert len(first) == 1
    assert "test-secret" not in first[0].model_dump_json()
    assert await inventory.read(str(path), "network-123") == first
    assert poll.await_count == 1
    assert await inventory.read(str(path), "missing") == []
    path.write_text(json.dumps([config() | {"community": "new-secret"}]))
    await inventory.read(str(path), "network-123")
    assert poll.await_count == 2


async def test_failure_never_returns_stale_links_or_credentials(monkeypatch):
    monkeypatch.setattr(
        switches, "get_cmd", AsyncMock(side_effect=RuntimeError("test-secret"))
    )
    result = await switches.poll_switch(SwitchConfig(**config()))
    assert result.status == "unreachable"
    assert result.ports == []
    assert result.observed_at is None
    assert "test-secret" not in result.model_dump_json()


async def test_empty_inventory_is_optional(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "switches_file", None)
    response = await auth_client.get("/api/switches")
    assert response.status_code == 200
    assert response.json() == []


async def test_bad_config_does_not_expose_values(auth_client, tmp_path, monkeypatch):
    path = tmp_path / "switches.json"
    path.write_text('{"community": "private-secret"}')
    monkeypatch.setattr(settings, "switches_file", str(path))
    response = await auth_client.get("/api/switches")
    assert response.status_code == 503
    assert "private-secret" not in response.text


async def test_requires_auth(async_client):
    response = await async_client.get("/api/switches")
    assert response.status_code == 401


async def test_refresh_replaces_online_data_when_switch_stops_responding(
    tmp_path, monkeypatch
):
    path = tmp_path / "switches.json"
    path.write_text(json.dumps([config()]))
    now = [0]
    monkeypatch.setattr(switches.time, "monotonic", lambda: now[0])
    poll = AsyncMock(
        side_effect=[
            SwitchSnapshot(
                id="office",
                name="Office",
                host="192.168.1.10",
                status="online",
                ports=[switches.SwitchPort(id=1, name="g1", status="up")],
            ),
            SwitchSnapshot(
                id="office", name="Office", host="192.168.1.10", status="unreachable"
            ),
        ]
    )
    monkeypatch.setattr(switches, "poll_switch", poll)
    inventory = SwitchInventory()
    assert (await inventory.read(str(path), "network-123"))[0].ports
    now[0] = 31
    refreshed = (await inventory.read(str(path), "network-123"))[0]
    assert refreshed.status == "unreachable"
    assert refreshed.ports == []


async def test_route_uses_only_selected_network(auth_client, tmp_path, monkeypatch):
    path = tmp_path / "switches.json"
    path.write_text(
        json.dumps([config(), config() | {"id": "second", "network_id": "network-456"}])
    )

    async def poll(config):
        return SwitchSnapshot(
            id=config.id, name=config.name, host=str(config.host), status="online"
        )

    monkeypatch.setattr(settings, "switches_file", str(path))
    monkeypatch.setattr(switches, "poll_switch", AsyncMock(side_effect=poll))
    response = await auth_client.get("/api/switches?network_id=network-456")
    assert response.status_code == 200
    assert [s["id"] for s in response.json()] == ["second"]
    assert "community" not in response.text


async def test_known_unmanaged_wiring_is_not_polled_or_given_fake_telemetry(
    tmp_path, monkeypatch
):
    path = tmp_path / "switches.json"
    known = dict(
        id="desk",
        name="Desk switch",
        port=101,
        eero_ids=["eero-office"],
        devices=[dict(mac="02:00:00:00:00:01", ip="192.168.1.165", name="Camera")],
    )
    path.write_text(json.dumps([config() | {"unmanaged_switches": [known]}]))
    poll = AsyncMock(
        return_value=SwitchSnapshot(
            id="office", name="Office", host="192.168.1.10", status="unreachable"
        )
    )
    monkeypatch.setattr(switches, "poll_switch", poll)
    inventory = SwitchInventory()
    snapshots = await inventory.read(str(path), "network-123")
    assert poll.await_count == 1
    unmanaged = snapshots[1]
    assert unmanaged.kind == "unmanaged"
    assert unmanaged.status == "unknown"
    assert unmanaged.host == "" and unmanaged.ports == []
    assert unmanaged.observed_at is None and unmanaged.mac is None
    assert unmanaged.upstream_port_id == 101
    assert unmanaged.eero_ids == ["eero-office"]
    assert str(unmanaged.known_devices[0].ip) == "192.168.1.165"
    assert "test-secret" not in unmanaged.model_dump_json()
    path.write_text(
        json.dumps([config() | {"unmanaged_switches": [known | {"name": "Renamed"}]}])
    )
    assert (await inventory.read(str(path), "network-123"))[1].name == "Renamed"
    assert poll.await_count == 2


async def test_rejects_conflicting_known_wiring(tmp_path, monkeypatch):
    path = tmp_path / "switches.json"
    known = dict(id="desk", name="Desk", port=1, eero_ids=["same-eero"])
    path.write_text(
        json.dumps(
            [
                config()
                | {"unmanaged_switches": [known, known | {"id": "other", "port": 2}]}
            ]
        )
    )
    poll = AsyncMock()
    monkeypatch.setattr(switches, "poll_switch", poll)
    with pytest.raises(ValueError, match="only one switch"):
        await SwitchInventory().read(str(path), "network-123")
    poll.assert_not_awaited()
    with pytest.raises(ValidationError):
        SwitchConfig(
            **(config() | {"unmanaged_switches": [known, known | {"id": "other"}]})
        )
