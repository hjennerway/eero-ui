"""LAN scope, scan failure/freshness and authenticated discovery boundaries."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Network
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import settings
from app.services import arp_scanner
from app.services.discovery import DiscoverySnapshot, read_discovery


def test_isolated_scanner_imports_without_any_dashboard_session_secret():
    environment = {
        k: v for k, v in os.environ.items() if k != "EERO_DASHBOARD_SESSION_SECRET"
    }
    result = subprocess.run(
        [sys.executable, "-c", "import app.services.arp_scanner"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def snapshot(**overrides):
    now = datetime.now(UTC) - timedelta(seconds=10)
    return DiscoverySnapshot.model_validate(
        {
            "network_id": "network-123",
            "subnet": "192.168.1.0/24",
            "last_scan": now,
            "last_success": now,
            "next_scan": now + timedelta(hours=1),
            "devices": [
                {
                    "ip": "192.168.1.165",
                    "mac": "02:00:00:00:00:01",
                    "last_seen": now,
                    "connected": True,
                }
            ],
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "subnet", ["8.8.8.0/24", "127.0.0.0/24", "169.254.0.0/24", "192.168.0.0/16"]
)
def test_scanner_refuses_non_lan_or_unbounded_ranges(subnet):
    with pytest.raises(ValidationError):
        snapshot(subnet=subnet)


def test_parser_ignores_outside_range_bad_macs_and_duplicates():
    output = """192.168.1.165 02:00:00:00:00:01
192.168.1.165 02:00:00:00:00:01 (DUP: 2)
192.168.2.1 02:00:00:00:00:02
192.168.1.0 02:00:00:00:00:03
192.168.1.255 ff:ff:ff:ff:ff:ff
192.168.1.166 garbage
Interface: eth0
"""
    found = arp_scanner.parse_scan(
        output, IPv4Network("192.168.1.0/24"), datetime.now(UTC)
    )
    assert len(found) == 1
    assert str(found[0].ip) == "192.168.1.165" and found[0].connected


def test_scan_uses_direct_host_interface_and_only_configured_subnet(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, json.dumps([{"dev": "enp3s0"}]) if args[0] == "ip" else ""
        )

    monkeypatch.setattr(arp_scanner.subprocess, "run", run)
    arp_scanner.scan_lan(IPv4Network("192.168.1.0/24"))
    assert calls[1][1] == "--interface=enp3s0"
    assert calls[1][-1] == "192.168.1.0/24"
    assert "--localnet" not in calls[1]
    monkeypatch.setattr(
        arp_scanner.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, '[{"dev":"eth0","gateway":"172.17.0.1"}]'
        ),
    )
    with pytest.raises(ValueError, match="directly attached"):
        arp_scanner.scan_lan(IPv4Network("192.168.1.0/24"))


def test_failed_scan_preserves_last_seen_but_never_reports_cached_hosts_online(
    monkeypatch,
):
    previous = snapshot()

    def fail(_):
        raise subprocess.TimeoutExpired("arp-scan", 45)

    monkeypatch.setattr(arp_scanner, "scan_lan", fail)
    result = arp_scanner.scan_once(previous, previous.network_id, previous.subnet, 3600)
    assert result.status == "error"
    assert result.last_success == previous.last_success
    assert result.devices[0].last_seen == previous.devices[0].last_seen
    assert not result.devices[0].connected
    assert (result.next_scan - result.last_scan).total_seconds() == 3600


def test_success_replaces_presence_and_expires_old_hosts(monkeypatch):
    previous = snapshot()
    previous.devices[0].last_seen -= timedelta(days=8)
    monkeypatch.setattr(
        arp_scanner, "scan_lan", lambda _: "192.168.1.166 02:00:00:00:00:02"
    )
    result = arp_scanner.scan_once(previous, previous.network_id, previous.subnet, 3600)
    assert [str(d.ip) for d in result.devices] == ["192.168.1.166"]
    assert result.devices[0].connected


def test_scan_never_reuses_another_networks_history(monkeypatch):
    previous = snapshot()
    monkeypatch.setattr(arp_scanner, "scan_lan", lambda _: "")
    result = arp_scanner.scan_once(previous, "network-456", previous.subnet, 3600)
    assert result.devices == [] and result.network_id == "network-456"


def test_reader_scopes_and_marks_overdue_scans_unconfirmed(tmp_path):
    path = tmp_path / "arp.json"
    old = snapshot()
    old.last_scan -= timedelta(hours=2)
    old.last_success = old.last_scan
    old.devices[0].last_seen = old.last_scan
    arp_scanner.write_snapshot(path, old)
    assert read_discovery(str(path), "other").devices == []
    result = read_discovery(str(path), "network-123")
    assert result.status == "stale" and result.warning
    assert not result.devices[0].connected


async def test_route_disabled_and_network_scoped(auth_client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "discovery_file", None)
    assert (await auth_client.get("/api/discovery")).json()["status"] == "disabled"
    path = tmp_path / "arp.json"
    arp_scanner.write_snapshot(path, snapshot())
    monkeypatch.setattr(settings, "discovery_file", str(path))
    assert (
        len(
            (await auth_client.get("/api/discovery?network_id=network-123")).json()[
                "devices"
            ]
        )
        == 1
    )
    assert (await auth_client.get("/api/discovery?network_id=network-456")).json()[
        "devices"
    ] == []


async def test_route_sanitizes_invalid_file(auth_client, monkeypatch, tmp_path):
    path = tmp_path / "arp.json"
    path.write_text('{"private": "do-not-expose"}')
    monkeypatch.setattr(settings, "discovery_file", str(path))
    response = await auth_client.get("/api/discovery")
    assert response.status_code == 503 and "do-not-expose" not in response.text


async def test_route_requires_auth(async_client):
    assert (await async_client.get("/api/discovery")).status_code == 401
