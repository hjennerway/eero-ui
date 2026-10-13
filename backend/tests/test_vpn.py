"""Tunnel metrics, failure semantics, SSH secrecy and authenticated routes."""

import asyncio
import json
import shlex
import shutil
import subprocess
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
    assert snapshot.connected_client_ips == ["192.168.4.50", "192.168.4.51"]
    snapshot, _ = vpn.parse_reading(vpn.VPNConfig(), reading(probe="1"))
    assert snapshot.status == "unknown" and snapshot.reachable
    snapshot, _ = vpn.parse_reading(vpn.VPNConfig(), reading(conntrack="UNAVAILABLE"))
    assert snapshot.connected_clients is None
    assert snapshot.connected_client_ips is None
    snapshot, _ = vpn.parse_reading(vpn.VPNConfig(), reading(present=False))
    assert snapshot.status == "disconnected" and snapshot.connected_clients == 0
    assert snapshot.connected_client_ips == []
    assert snapshot.download_mbps == snapshot.upload_mbps == 0


def test_client_ips_are_sorted_numerically_and_normalized():
    data = reading(
        conntrack="\n".join(
            f"ipv4 2 udp 17 60 src={address} dst=1.1.1.1 src=1.1.1.1 dst=10.8.0.2"
            for address in [
                "192.168.4.100",
                "192.168.4.9",
                "192.168.3.200",
                "192.168.4.20",
                "192.168.4.9",
                "invalid-address",
            ]
        )
    )
    addresses = json.loads(data["ADDRESSES"])
    addresses[1]["addr_info"].append({"local": "fd00:8::2", "scope": "global"})
    data["ADDRESSES"] = json.dumps(addresses)
    data["CONNTRACK"] += "\n" + "\n".join(
        f"ipv6 10 udp 17 60 src={address} dst=2606:4700:4700::1111 src=2606:4700:4700::1111 dst=fd00:8::2"
        for address in ["fd00:4::10", "fd00:4::2", "fd00:4:0:0:0:0:0:2"]
    )
    snapshot, _ = vpn.parse_reading(vpn.VPNConfig(), data)
    assert snapshot.connected_client_ips == [
        "192.168.3.200",
        "192.168.4.9",
        "192.168.4.20",
        "192.168.4.100",
        "fd00:4::2",
        "fd00:4::10",
    ]
    assert snapshot.connected_clients == 6


def test_empty_conntrack_has_no_client_ips():
    snapshot, _ = vpn.parse_reading(vpn.VPNConfig(), reading(conntrack=""))
    assert snapshot.connected_clients == 0
    assert snapshot.connected_client_ips == []


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
    assert failed.connected_client_ips is None
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
        ("POST", "/api/vpn/tunnel"),
        ("POST", "/api/vpn/tunnels"),
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
        connected_client_ips=["192.168.4.50"],
        download_mbps=12,
        upload_mbps=4,
        collected_at=datetime.now(UTC) - timedelta(seconds=91),
        stale=False,
    )
    response = (await auth_client.get("/api/vpn")).json()
    assert response["status"] == "unknown" and response["stale"]
    assert response["connected_clients"] is response["download_mbps"] is None
    assert response["connected_client_ips"] is None
    assert monitor.snapshot.connected_client_ips == ["192.168.4.50"]


async def test_snapshot_returns_live_client_ips(auth_client, monitor, monkeypatch):
    monkeypatch.setattr(
        vpn, "read_openwrt", lambda config: (reading(), "SHA256:fixture")
    )
    await monitor.collect_once()
    response = await auth_client.get("/api/vpn")
    assert response.status_code == 200
    assert response.json()["connected_client_ips"] == ["192.168.4.50", "192.168.4.51"]


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


@pytest.mark.parametrize("mode", ["openvpn", "network"])
@pytest.mark.parametrize("action", ["connect", "disconnect"])
async def test_tunnel_control_uses_saved_target_and_refreshes_real_status(
    auth_client, monitor, monkeypatch, mode, action
):
    config = vpn.VPNConfig(
        password="private-password",
        host_key="SHA256:fixture",
        control_mode=mode,
        control_target="nordvpn",
    )
    vpn.save_config(monitor.path, config)
    monitor._baseline = ("boot-a", 7, 100, 1, 1)
    remote = MagicMock()
    monkeypatch.setattr(vpn, "control_openwrt", remote)
    monkeypatch.setattr(
        vpn, "read_openwrt", lambda config: (reading(present=False), "SHA256:fixture")
    )
    response = await auth_client.post("/api/vpn/tunnel", json={"action": action})
    assert response.status_code == 200
    # Successful command delivery does not mean a handshake has completed.
    assert response.json()["status"] == "disconnected"
    assert response.json()["control_available"]
    saved, requested = remote.call_args.args
    assert saved == config and requested == action
    assert "private-password" not in response.text
    assert monitor._baseline is None
    assert vpn.read_config(monitor.path) == config


@pytest.mark.parametrize(
    "config",
    [
        vpn.VPNConfig(
            enabled=False, control_target="nordvpn", host_key="SHA256:fixture"
        ),
        vpn.VPNConfig(control_target="nordvpn"),
        vpn.VPNConfig(host_key="SHA256:fixture"),
    ],
)
async def test_unconfigured_controls_never_open_ssh(
    auth_client, monitor, monkeypatch, config
):
    vpn.save_config(monitor.path, config)
    remote = MagicMock()
    monkeypatch.setattr(vpn, "control_openwrt", remote)
    response = await auth_client.post("/api/vpn/tunnel", json={"action": "disconnect"})
    assert response.status_code == 409
    remote.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "restart"},
        {"action": "connect; reboot"},
        {"action": "connect", "target": "other-tunnel"},
    ],
)
async def test_control_payload_cannot_override_saved_tunnel(
    auth_client, monitor, monkeypatch, payload
):
    remote = MagicMock()
    monkeypatch.setattr(vpn, "control_openwrt", remote)
    assert (await auth_client.post("/api/vpn/tunnel", json=payload)).status_code == 422
    remote.assert_not_called()


async def test_control_requires_csrf_header(auth_client, monitor, monkeypatch):
    remote = MagicMock()
    monkeypatch.setattr(vpn, "control_openwrt", remote)
    response = await auth_client.post(
        "/api/vpn/tunnel", json={"action": "connect"}, headers={"X-Requested-With": ""}
    )
    assert response.status_code == 403
    remote.assert_not_called()


async def test_control_failure_invalidates_old_measurements(
    auth_client, monitor, monkeypatch
):
    vpn.save_config(
        monitor.path, vpn.VPNConfig(control_target="nordvpn", host_key="SHA256:fixture")
    )
    monitor.snapshot, monitor._baseline = vpn.parse_reading(vpn.VPNConfig(), reading())
    publisher = MagicMock()
    monitor._publish_snapshot = publisher

    def failed(config, action):
        raise vpn.VPNReadError("Cannot reach OpenWrt.")

    monkeypatch.setattr(vpn, "control_openwrt", failed)
    response = await auth_client.post("/api/vpn/tunnel", json={"action": "disconnect"})
    assert response.status_code == 422 and "Cannot reach OpenWrt" in response.text
    assert monitor.snapshot.stale and monitor.snapshot.status == "unknown"
    assert monitor.snapshot.connected_clients is monitor.snapshot.download_mbps is None
    assert monitor._baseline is None
    publisher.assert_called_once()


async def test_concurrent_controls_are_rejected(monitor, monkeypatch):
    vpn.save_config(
        monitor.path, vpn.VPNConfig(control_target="nordvpn", host_key="SHA256:fixture")
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def collecting():
        started.set()
        await release.wait()
        return monitor.snapshot

    monkeypatch.setattr(vpn, "control_openwrt", MagicMock())
    monitor.collect_once = collecting
    pending = asyncio.create_task(monitor.control("connect"))
    await started.wait()
    with pytest.raises(vpn.VPNControlError, match="already in progress"):
        await monitor.control("disconnect")
    release.set()
    await pending


def test_control_host_key_is_checked_before_credentials_or_command(monkeypatch):
    transport = MagicMock()
    transport.__enter__.return_value = transport
    transport.get_remote_server_key.return_value.asbytes.return_value = b"changed-key"
    monkeypatch.setattr(vpn.paramiko, "Transport", lambda sock: transport)
    monkeypatch.setattr(vpn.socket, "create_connection", MagicMock())
    with pytest.raises(vpn.VPNReadError, match="host key changed"):
        vpn.control_openwrt(
            vpn.VPNConfig(
                control_target="nordvpn", host_key="SHA256:expected", password="private"
            ),
            "disconnect",
        )
    transport.auth_password.assert_not_called()
    transport.open_session.assert_not_called()


@pytest.mark.parametrize(
    "target", ["", "-a", "../vpn", "vpn; reboot", "vpn$(reboot)", "vpn\nreboot"]
)
def test_command_refuses_unsafe_or_empty_tunnel_names(target):
    # Also enforce validation at the command boundary, even for an unvalidated model.
    config = vpn.VPNConfig.model_construct(control_target=target)
    with pytest.raises(vpn.VPNControlError):
        vpn.control_command(config, "connect")


def test_commands_are_scoped_and_network_mode_refuses_non_vpn_protocols():
    config = vpn.VPNConfig(control_target="nordvpn")
    assert "/etc/init.d/openvpn start nordvpn" in vpn.control_command(config, "connect")
    assert "/etc/init.d/openvpn stop nordvpn" in vpn.control_command(
        config, "disconnect"
    )
    config.control_mode = "network"
    command = vpn.control_command(config, "disconnect")
    assert "wireguard|openvpn" in command
    assert "ubus call network.interface.nordvpn down '{}'" in command
    assert "restart" not in command and "uci set" not in command


async def test_collections_publish_disabled_and_live_readings(monitor, monkeypatch):
    publisher = MagicMock()
    monitor._publish_snapshot = publisher
    vpn.save_config(monitor.path, vpn.VPNConfig(enabled=False))
    await monitor.collect_once()
    assert publisher.call_args.args[0].status == "disabled"
    vpn.save_config(monitor.path, vpn.VPNConfig(control_target="nordvpn"))
    monkeypatch.setattr(
        vpn, "read_openwrt", lambda config: (reading(), "SHA256:fixture")
    )
    await monitor.collect_once()
    assert publisher.call_args.args[0].status == "connected"
    assert publisher.call_args.args[0].control_available


def test_discovery_returns_sorted_unique_safe_tunnel_names(monkeypatch):
    remote = MagicMock(
        return_value=(
            "openvpn\tnordvpn\nnetwork\twg_vpn\nopenvpn\tnordvpn\n"
            "openvpn\tbackup\nnetwork\twg-down\nopenvpn\t../unsafe\n"
            "openvpn\tbad; reboot\nother\tlan\nnetwork\t" + "a" * 65 + "\n",
            "SHA256:fixture",
        )
    )
    monkeypatch.setattr(vpn, "_run_openwrt", remote)
    config = vpn.VPNConfig(password="private-password", host_key="SHA256:fixture")
    assert [item.model_dump() for item in vpn.discover_openwrt(config)] == [
        {"control_mode": "network", "name": "wg-down"},
        {"control_mode": "network", "name": "wg_vpn"},
        {"control_mode": "openvpn", "name": "backup"},
        {"control_mode": "openvpn", "name": "nordvpn"},
    ]
    remote.assert_called_once_with(config, vpn.discovery_command())


@pytest.mark.parametrize("openvpn_installed", [True, False])
def test_discovery_command_lists_configured_vpns_without_reading_files(
    tmp_path, monkeypatch, openvpn_installed
):
    shell = shutil.which("sh")
    if not shell:
        pytest.skip("A POSIX shell is needed to exercise the OpenWrt command")
    etc = tmp_path / "etc"
    library = tmp_path / "lib"
    (etc / "init.d").mkdir(parents=True)
    (etc / "openvpn").mkdir()
    library.mkdir()
    (etc / "openwrt_release").touch()
    if openvpn_installed:
        service = etc / "init.d" / "openvpn"
        service.write_text("#!/bin/sh\nexit 1\n")
        service.chmod(0o700)
    for name in ("nordvpn", "file-vpn", "bad name"):
        (etc / "openvpn" / f"{name}.conf").write_text("private-file-contents")
    (library / "functions.sh").write_text("""config_load() { fixture_package="$1"; }
config_foreach() {
  case "$fixture_package:$2" in
    openvpn:openvpn) "$1" nordvpn; "$1" stopped_vpn ;;
    network:interface) "$1" lan; "$1" wg_vpn; "$1" vpn_net; "$1" wan ;;
  esac
}
config_get() {
  case "$2:$3" in
    wg_vpn:proto) eval "$1=wireguard" ;;
    vpn_net:proto) eval "$1=openvpn" ;;
    wan:proto) eval "$1=dhcp" ;;
    *) eval "$1=static" ;;
  esac
}
""")
    command = vpn.discovery_command().replace("/etc", shlex.quote(etc.as_posix()))
    command = command.replace("/lib", shlex.quote(library.as_posix()))
    result = subprocess.run(
        [shell, "-s"],
        input=command,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert "private-file-contents" not in result.stdout
    monkeypatch.setattr(
        vpn, "_run_openwrt", lambda *_: (result.stdout, "SHA256:fixture")
    )
    expected = [
        {"control_mode": "network", "name": "vpn_net"},
        {"control_mode": "network", "name": "wg_vpn"},
    ]
    if openvpn_installed:
        expected += [
            {"control_mode": "openvpn", "name": "file-vpn"},
            {"control_mode": "openvpn", "name": "nordvpn"},
            {"control_mode": "openvpn", "name": "stopped_vpn"},
        ]
    assert [
        item.model_dump() for item in vpn.discover_openwrt(vpn.VPNConfig())
    ] == expected


@pytest.mark.parametrize("new_host", [False, True])
async def test_discovery_uses_form_credentials_without_saving(
    auth_client, monitor, monkeypatch, new_host
):
    vpn.save_config(
        monitor.path,
        vpn.VPNConfig(password="saved-private-password", host_key="SHA256:fixture"),
    )
    original = monitor.path.read_bytes()
    remote = MagicMock(
        return_value=[vpn.VPNTunnel(name="nordvpn", control_mode="openvpn")]
    )
    monkeypatch.setattr(vpn, "discover_openwrt", remote)
    response = await auth_client.post(
        "/api/vpn/tunnels",
        json={
            "enabled": False,
            "host": "192.168.4.11" if new_host else "192.168.4.10",
            "password": "new-private-password" if new_host else None,
        },
    )
    assert response.status_code == 200
    assert response.json() == [{"control_mode": "openvpn", "name": "nordvpn"}]
    config = remote.call_args.args[0]
    assert config.password.get_secret_value() == (
        "new-private-password" if new_host else "saved-private-password"
    )
    assert config.host_key == (None if new_host else "SHA256:fixture")
    assert "private-password" not in response.text
    assert monitor.path.read_bytes() == original
    monitor.victoria.write.assert_not_awaited()


async def test_discovery_does_not_reuse_credentials_for_another_device(
    auth_client, monitor, monkeypatch
):
    vpn.save_config(monitor.path, vpn.VPNConfig(password="private-password"))
    remote = MagicMock(return_value=[])
    monkeypatch.setattr(vpn, "discover_openwrt", remote)
    response = await auth_client.post("/api/vpn/tunnels", json={"host": "192.168.4.11"})
    assert response.status_code == 200 and response.json() == []
    assert remote.call_args.args[0].password is None


async def test_discovery_failures_and_invalid_credentials_do_not_save_or_leak(
    auth_client, monitor, monkeypatch
):
    remote = MagicMock(side_effect=vpn.VPNReadError("SSH login failed."))
    monkeypatch.setattr(vpn, "discover_openwrt", remote)
    response = await auth_client.post(
        "/api/vpn/tunnels", json={"password": "private-password"}
    )
    assert response.status_code == 422 and "SSH login failed" in response.text
    response = await auth_client.post(
        "/api/vpn/tunnels", json={"host": "8.8.8.8", "password": "private-password"}
    )
    assert response.status_code == 422 and "private-password" not in response.text
    remote.assert_called_once()
    assert not monitor.path.exists()


async def test_discovery_requires_csrf_header(auth_client, monitor, monkeypatch):
    remote = MagicMock()
    monkeypatch.setattr(vpn, "discover_openwrt", remote)
    response = await auth_client.post(
        "/api/vpn/tunnels", json={}, headers={"X-Requested-With": ""}
    )
    assert response.status_code == 403
    remote.assert_not_called()


def test_discovery_host_key_is_checked_before_authentication(monkeypatch):
    transport = MagicMock()
    transport.__enter__.return_value = transport
    transport.get_remote_server_key.return_value.asbytes.return_value = b"changed-key"
    monkeypatch.setattr(vpn.paramiko, "Transport", lambda sock: transport)
    monkeypatch.setattr(vpn.socket, "create_connection", MagicMock())
    with pytest.raises(vpn.VPNReadError, match="host key changed"):
        vpn.discover_openwrt(
            vpn.VPNConfig(host_key="SHA256:expected", password="private")
        )
    transport.auth_password.assert_not_called()
    transport.open_session.assert_not_called()
