"""Tunnel metrics, failure semantics, SSH secrecy and authenticated routes."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.main import app
from app.services import vpn
from app.services.victoria import VictoriaWriteError


def reading(
    rx=1_000_000,
    tx=500_000,
    uptime=100,
    boot="boot-a",
    probe="0",
    present=True,
    conntrack=None,
):
    tunnel = {
        "ifname": "tun0",
        "ifindex": 7,
        "flags": ["UP"],
        "stats64": {"rx": {"bytes": rx}, "tx": {"bytes": tx}},
    }
    return {
        "BOOT": boot,
        "UPTIME": str(uptime),
        "LINKS": json.dumps([tunnel] if present else []),
        "ADDRESSES": json.dumps(
            [
                {
                    "ifname": "br-lan",
                    "addr_info": [{"local": "192.168.4.10", "scope": "global"}],
                },
                {
                    "ifname": "tun0",
                    "addr_info": [{"local": "10.8.0.2", "scope": "global"}],
                },
            ]
        ),
        "INTERFACES": '{"interface": [{"device": "tun0", "up": true}]}',
        "PROBE": probe,
        "CONNTRACK": (
            conntrack
            if conntrack is not None
            else "\n".join(
                [
                    "ipv4 2 tcp 6 300 ESTABLISHED src=192.168.4.50 dst=8.8.8.8 src=8.8.8.8 dst=10.8.0.2",
                    "ipv4 2 udp 17 60 src=192.168.4.50 dst=1.1.1.1 src=1.1.1.1 dst=10.8.0.2",
                    "ipv4 2 udp 17 60 src=192.168.4.51 dst=1.1.1.1 src=1.1.1.1 dst=10.8.0.2",
                    "ipv4 2 tcp 6 300 ESTABLISHED src=192.168.4.10 dst=8.8.8.8 src=8.8.8.8 dst=10.8.0.2",
                    "ipv4 2 tcp 6 300 ESTABLISHED src=192.168.4.52 dst=192.168.4.10 src=192.168.4.10 dst=192.168.4.52",
                    "ipv4 2 tcp 6 60 TIME_WAIT src=192.168.4.53 dst=8.8.8.8 src=8.8.8.8 dst=10.8.0.2",
                    "ipv4 2 udp 17 60 [UNREPLIED] src=192.168.4.54 dst=8.8.8.8 src=8.8.8.8 dst=10.8.0.2",
                ]
            )
        ),
    }


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    service = vpn.VPNService(
        tmp_path / "vpn-test.json",
        SimpleNamespace(
            write=AsyncMock(),
            query_range=AsyncMock(return_value={"data": {"result": []}}),
        ),
    )
    monkeypatch.setattr(app.state, "vpn", service, raising=False)
    return service


def test_clients_only_include_unique_replied_tunnel_flows():
    snapshot, _ = vpn.parse_reading(vpn.VPNConfig(), reading())
    assert snapshot.status == "connected" and snapshot.connected_clients == 2
    snapshot, _ = vpn.parse_reading(vpn.VPNConfig(), reading(probe="1"))
    assert snapshot.status == "unknown" and snapshot.reachable
    snapshot, _ = vpn.parse_reading(vpn.VPNConfig(), reading(conntrack="UNAVAILABLE"))
    assert snapshot.connected_clients is None
    snapshot, _ = vpn.parse_reading(vpn.VPNConfig(), reading(present=False))
    assert snapshot.status == "disconnected" and snapshot.connected_clients == 0
    assert snapshot.download_mbps == snapshot.upload_mbps == 0


def test_changed_host_key_is_rejected_before_password_authentication(monkeypatch):
    transport = MagicMock()
    transport.__enter__.return_value = transport
    transport.get_remote_server_key.return_value.asbytes.return_value = b"changed-key"
    monkeypatch.setattr(vpn.paramiko, "Transport", lambda sock: transport)
    monkeypatch.setattr(vpn.socket, "create_connection", MagicMock())
    config = vpn.VPNConfig(host_key="SHA256:expected", password="private-password")
    with pytest.raises(vpn.VPNReadError, match="host key changed"):
        vpn.read_openwrt(config)
    transport.auth_password.assert_not_called()


async def test_disabled_monitor_never_connects_or_writes(monitor, monkeypatch):
    vpn.save_config(monitor.path, vpn.VPNConfig(enabled=False))
    remote = MagicMock()
    monkeypatch.setattr(vpn, "read_openwrt", remote)
    result = await monitor.collect_once()
    assert result.status == "disabled"
    remote.assert_not_called()
    monitor.victoria.write.assert_not_awaited()


async def test_counter_deltas_reboots_resets_and_recovery(monitor, monkeypatch):
    readings = iter(
        [
            reading(),
            reading(rx=4_000_000, tx=2_000_000, uptime=130),
            reading(rx=10, tx=10, uptime=160),
            reading(rx=2_000_000, tx=1_000_000, uptime=30, boot="boot-b"),
            vpn.VPNReadError("Cannot reach OpenWrt."),
            reading(uptime=190),
            reading(rx=4_000_000, tx=2_000_000, uptime=220),
        ]
    )

    def poll(config):
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value, "SHA256:fixture"

    monkeypatch.setattr(vpn, "read_openwrt", poll)
    assert (await monitor.collect_once()).download_mbps is None
    sample = await monitor.collect_once()
    assert sample.download_mbps == pytest.approx(0.8)
    assert sample.upload_mbps == pytest.approx(0.4)
    assert (await monitor.collect_once()).download_mbps is None
    assert (await monitor.collect_once()).download_mbps is None
    failed = await monitor.collect_once()
    assert failed.status == "unreachable" and failed.download_mbps is None
    assert failed.connected_clients is None and failed.stale
    assert (await monitor.collect_once()).download_mbps is None
    assert (await monitor.collect_once()).download_mbps == pytest.approx(0.8)
    samples = monitor.victoria.write.call_args_list[4].args[0]
    assert {sample.name for sample in samples} == {
        "vpn_reachable",
        "vpn_connection_status",
    }
    assert json.loads(monitor.path.read_text())["host_key"] == "SHA256:fixture"


async def test_store_failure_does_not_discard_live_reading(monitor, monkeypatch):
    monkeypatch.setattr(
        vpn, "read_openwrt", lambda config: (reading(), "SHA256:fixture")
    )
    monitor.victoria.write.side_effect = VictoriaWriteError("store offline")
    sample = await monitor.collect_once()
    assert sample.status == "connected" and sample.history_error


async def test_settings_test_before_save_and_keep_password_private(
    auth_client, monitor, monkeypatch
):
    monkeypatch.setattr(
        vpn, "read_openwrt", lambda config: (reading(), "SHA256:fixture")
    )
    response = await auth_client.put(
        "/api/vpn/connection", json={"password": "vpn-private-password"}
    )
    assert response.status_code == 200 and response.json()["password_set"]
    assert "vpn-private-password" not in response.text
    assert json.loads(monitor.path.read_text())["password"] == "vpn-private-password"
    await auth_client.put("/api/vpn/connection", json={"password": None})
    assert json.loads(monitor.path.read_text())["password"] == "vpn-private-password"
    original = monitor.path.read_bytes()

    def failed(config):
        raise vpn.VPNReadError("SSH login failed.")

    monkeypatch.setattr(vpn, "read_openwrt", failed)
    assert (
        await auth_client.put("/api/vpn/connection", json={"password": "bad"})
    ).status_code == 422
    assert monitor.path.read_bytes() == original
    response = await auth_client.put("/api/vpn/connection", json={"enabled": False})
    assert response.status_code == 200 and not response.json()["password_set"]
    assert json.loads(monitor.path.read_text())["password"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"host": "127.0.0.1"},
        {"host": "8.8.8.8"},
        {"interface": "tun0; reboot"},
        {"port": 0},
    ],
)
async def test_invalid_settings_do_not_echo_credentials(auth_client, monitor, payload):
    response = await auth_client.put(
        "/api/vpn/connection", json={**payload, "password": "vpn-private-password"}
    )
    assert response.status_code == 422
    assert "vpn-private-password" not in response.text
    assert not monitor.path.exists()


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "/api/vpn"),
        ("GET", "/api/vpn/history"),
        ("GET", "/api/vpn/connection"),
        ("PUT", "/api/vpn/connection"),
    ],
)
async def test_all_routes_require_login(async_client, monitor, method, url):
    response = await async_client.request(
        method, url, **({"json": {}} if method == "PUT" else {})
    )
    assert response.status_code == 401


async def test_stale_snapshot_hides_old_counts_and_rates(auth_client, monitor):
    monitor.snapshot = vpn.VPNSnapshot(
        status="connected",
        reachable=True,
        connected_clients=7,
        download_mbps=12,
        upload_mbps=4,
        collected_at=datetime.now(UTC) - timedelta(seconds=91),
        stale=False,
    )
    response = (await auth_client.get("/api/vpn")).json()
    assert response["status"] == "unknown" and response["stale"]
    assert response["connected_clients"] is response["download_mbps"] is None


async def test_history_is_scoped_and_preserves_unknown_status(auth_client, monitor):
    monitor.victoria.query_range.return_value = {
        "data": {"result": [{"values": [[100, "-1"], [130, "NaN"], [160, "0"]]}]}
    }
    response = await auth_client.get("/api/vpn/history?range=1h")
    assert response.status_code == 200
    assert response.json()["connection_status"] == [
        {"x": 100000, "y": -1},
        {"x": 160000, "y": 0},
    ]
    assert len(monitor.victoria.query_range.call_args_list) == 4
    for call in monitor.victoria.query_range.call_args_list:
        assert '{host="192.168.4.10",interface="tun0"}[45s]' in call.args[0]
        assert call.args[3] == "30s"


async def test_background_collector_uses_30_second_cadence(monitor, monkeypatch):
    monitor.collect_once = AsyncMock()

    async def sleep(delay):
        assert 29 < delay <= 30
        raise asyncio.CancelledError

    monkeypatch.setattr(vpn.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await monitor.run_forever()
    monitor.collect_once.assert_awaited_once()
