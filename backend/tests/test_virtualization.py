"""Inventory discovery, stale data, credentials, and real polling lifecycle."""

import asyncio
import ssl
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.main import app
from app.services import virtualization as module
from app.services.virtualization import VirtualizationService, _lan_ip, _tls_context
from app.virtualization_models import (
    ConnectionUpdate,
    DockerContainer,
    DockerInventory,
    GuestInventory,
    NodeInventory,
    VirtualizationSnapshot,
)


async def empty_provider():
    if False:
        yield None


def connection(**overrides):
    return ConnectionUpdate(
        **{
            "url": "https://pve.test:8006",
            "token_id": "monitor@pve!inventory",
            "token_secret": "test-secret",
            **overrides,
        }
    )


def guest(**overrides):
    return GuestInventory(
        **{"id": "lxc:101", "vmid": 101, "kind": "lxc", "name": "services", **overrides}
    )


@pytest.fixture
def inventory(tmp_path):
    return VirtualizationService(tmp_path, empty_provider)


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


async def test_background_runs_without_browser_and_waits_ten_minutes(tmp_path):
    clock = [1000.0]
    service = VirtualizationService(tmp_path, empty_provider, clock=lambda: clock[0])
    service.save("net-1", connection())
    service._collect = AsyncMock()
    worker = asyncio.create_task(service.run_forever())
    try:
        await settle()
        assert service._collect.await_count == 1
        clock[0] += 599
        service.wake.set()
        await settle()
        assert service._collect.await_count == 1
        clock[0] += 1
        service.wake.set()
        await settle()
        assert service._collect.await_count == 2
        # A visible-page refresh resets the background deadline.
        clock[0] += 60
        await service.refresh("net-1", force=False)
        assert service._collect.await_count == 3
        clock[0] += 599
        service.wake.set()
        await settle()
        assert service._collect.await_count == 3
        clock[0] += 1
        service.wake.set()
        await settle()
        assert service._collect.await_count == 4
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await service.aclose()


async def test_visible_refresh_at_sixty_seconds_and_manual_bypasses_cooldown(tmp_path):
    clock = [1000.0]
    service = VirtualizationService(tmp_path, empty_provider, clock=lambda: clock[0])
    service.save("net-1", connection())
    service._collect = AsyncMock()
    await service.refresh("net-1", force=False)
    clock[0] += 59
    await service.refresh("net-1", force=False)
    assert service._collect.await_count == 1
    clock[0] += 1
    await service.refresh("net-1", force=False)
    assert service._collect.await_count == 2
    await service.refresh("net-1", force=True)
    assert service._collect.await_count == 3


async def test_overlapping_refreshes_share_work_and_browser_cancellation_does_not_stop_it(
    inventory,
):
    inventory.save("net-1", connection())
    started, release = asyncio.Event(), asyncio.Event()

    async def collect(entry):
        started.set()
        await release.wait()
        entry.snapshot.collected_at = "2026-10-13T12:00:00+00:00"

    inventory._collect = AsyncMock(side_effect=collect)
    first = asyncio.create_task(inventory.refresh("net-1"))
    await started.wait()
    second = asyncio.create_task(inventory.refresh("net-1"))
    await settle()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert not inventory.entries["net-1"].task.cancelled()
    release.set()
    result = await second
    assert result.collected_at
    assert inventory._collect.await_count == 1


def test_saved_secrets_are_redacted_in_summaries_and_restored_privately(inventory):
    inventory.save("net-1", connection())
    summary = inventory.summary("net-1").model_dump_json()
    assert "test-secret" not in summary
    assert "token_secret" not in summary
    assert inventory.summary("net-1").has_token
    inventory.save("net-1", connection(token_secret=None))
    restored = VirtualizationService(inventory.directory, empty_provider)
    restored.load()
    assert (
        restored.entries["net-1"].config.token_secret.get_secret_value()
        == "test-secret"
    )


def test_bad_certificate_does_not_replace_working_connection(inventory):
    inventory.save("net-1", connection())
    with pytest.raises(ValueError, match="PEM"):
        inventory.save("net-1", connection(ca_certificate="not a certificate"))
    assert inventory.entries["net-1"].config.ca_certificate is None
    assert _tls_context(None).verify_mode == ssl.CERT_REQUIRED
    assert _tls_context(None).check_hostname is True


@pytest.mark.parametrize(
    "url",
    [
        "http://pve.test",
        "https://user:pass@pve.test",
        "https://pve.test/api",
        "https://pve.test:0",
    ],
)
def test_connection_rejects_insecure_or_credential_bearing_urls(url):
    with pytest.raises(ValidationError):
        connection(url=url)


@pytest.mark.parametrize(
    "value",
    ["127.0.0.1", "169.254.169.254", "8.8.8.8", "::1", "fe80::1", "bad-address"],
)
def test_discovery_never_targets_public_loopback_or_link_local_addresses(value):
    assert _lan_ip(value) is None


def test_mac_matching_rejects_clones_and_uses_only_current_leases():
    mac = "aa:bb:cc:dd:ee:01"
    first, clone = guest(macs=[mac]), guest(
        id="qemu:102", vmid=102, kind="qemu", macs=[mac]
    )
    devices = [
        {"id": "dev-1", "mac": mac.upper(), "ip": "192.168.4.101", "connected": True}
    ]
    VirtualizationService._match([first, clone], devices)
    assert first.match_status == clone.match_status == "ambiguous"
    assert first.eero_device_ids == first.ips == []
    VirtualizationService._match([first], [{**devices[0], "connected": False}])
    assert first.eero_device_ids == ["dev-1"]
    assert first.ips == []
    VirtualizationService._match([first], devices)
    assert first.ips == ["192.168.4.101"]


def test_guest_interfaces_exclude_docker_bridges_and_unrelated_virtual_nics():
    current = guest(macs=["aa:bb:cc:dd:ee:01"])
    VirtualizationService._interfaces(
        current,
        [
            {
                "name": "docker0",
                "hardware-address": "00:00:00:00:00:01",
                "ip-addresses": [{"ip-address": "172.17.0.1"}],
            },
            {
                "name": "br-compose",
                "hwaddr": "00:00:00:00:00:02",
                "inet": "172.18.0.1/16",
            },
            {"name": "tun0", "hwaddr": "00:00:00:00:00:03", "inet": "10.8.0.1/24"},
            {"name": "eth0", "hwaddr": "AA:BB:CC:DD:EE:01", "inet": "192.168.4.101/24"},
        ],
    )
    assert current.ips == ["192.168.4.101"]
    assert set(current.macs) == {"aa:bb:cc:dd:ee:01"}


def test_corrupt_snapshot_does_not_drop_saved_connection(inventory):
    inventory.save("net-1", connection())
    inventory._path("net-1", "snapshot").write_text("not JSON", encoding="utf-8")
    restored = VirtualizationService(inventory.directory, empty_provider)
    restored.load()
    assert restored.summary("net-1").configured
    assert restored.snapshot("net-1").nodes == []


async def test_discovery_migration_and_partial_docker_failure(inventory, monkeypatch):
    context = ssl.create_default_context()
    monkeypatch.setattr(module, "_tls_context", lambda *args: context)
    inventory.save(
        "net-1",
        connection(
            docker_enabled=True,
            docker_ca_certificate="ca",
            docker_client_certificate="cert",
            docker_client_key="key",
        ),
    )
    inventory._devices = AsyncMock(
        return_value=(
            [
                {
                    "id": "dev-1",
                    "mac": "aa:bb:cc:dd:ee:01",
                    "connected": True,
                    "ip": "192.168.4.101",
                }
            ],
            True,
        )
    )
    moved, docker_down = [False], [False]
    requests = []

    def handle(request):
        requests.append(request)
        path = request.url.path
        if request.url.host != "pve.test":
            assert "Authorization" not in request.headers
            if docker_down[0] and request.url.host == "192.168.4.101":
                raise httpx.ConnectError("connection failed", request=request)
            if path == "/version":
                return httpx.Response(
                    200, json={"ApiVersion": "1.45", "Version": "26.0.1"}
                )
            if path.endswith("/info"):
                return httpx.Response(
                    200,
                    json={
                        "ID": request.url.host,
                        "OperatingSystem": "Debian GNU/Linux 12",
                        "OSVersion": "12",
                    },
                )
            assert request.url.params["all"] == "true"
            return httpx.Response(
                200,
                json=[
                    {
                        "Id": "running-1",
                        "Names": ["/web"],
                        "State": "running",
                        "Status": "Up (healthy)",
                    },
                    {"Id": "stopped-1", "Names": ["/old-service"], "State": "exited"},
                ],
            )
        if path == "/api2/json/cluster/resources":
            data = [
                {"type": "node", "node": "pve-1", "status": "online"},
                {"type": "node", "node": "pve-2", "status": "online"},
                {
                    "type": "lxc",
                    "node": "pve-2" if moved[0] else "pve-1",
                    "vmid": 101,
                    "name": "services",
                    "status": "running",
                },
                {
                    "type": "qemu",
                    "node": "pve-1",
                    "vmid": 102,
                    "name": "windows",
                    "status": "running",
                },
            ]
        elif path.endswith("/version"):
            data = {"version": "9.1.1"}
        elif path.endswith("/config"):
            data = (
                {"ostype": "debian", "net0": "hwaddr=AA:BB:CC:DD:EE:01,ip=dhcp"}
                if "/lxc/" in path
                else {"ostype": "win11", "agent": 1, "net0": "virtio=AA:BB:CC:DD:EE:02"}
            )
        elif path.endswith("/interfaces"):
            data = [
                {
                    "name": "eth0",
                    "hwaddr": "aa:bb:cc:dd:ee:01",
                    "inet": "192.168.4.101/24",
                }
            ]
        elif path.endswith("/get-osinfo"):
            data = {
                "result": {"pretty-name": "Microsoft Windows 11", "version-id": "11"}
            }
        else:
            data = {
                "result": [
                    {
                        "hardware-address": "aa:bb:cc:dd:ee:02",
                        "ip-addresses": [{"ip-address": "192.168.4.102"}],
                    }
                ]
            }
        return httpx.Response(200, json={"data": data})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    first = await inventory.refresh("net-1")
    linux, windows = first.nodes[0].guests
    assert linux.os_name == "Debian GNU/Linux 12"
    assert linux.os_source == "docker"
    assert linux.eero_device_ids == ["dev-1"]
    assert windows.os_name == "Microsoft Windows 11"
    assert windows.os_source == "guest_agent"
    assert {container.state for container in linux.docker.containers} == {
        "running",
        "exited",
    }
    assert first.nodes[0].version == "9.1.1"
    moved[0], docker_down[0] = True, True
    second = await inventory.refresh("net-1")
    assert [item.id for item in second.nodes[0].guests] == ["qemu:102"]
    migrated = second.nodes[1].guests[0]
    assert migrated.id == "lxc:101"
    assert migrated.docker.status == "stale"
    assert {container.state for container in migrated.docker.containers} == {
        "running",
        "exited",
    }
    assert second.nodes[0].guests[0].docker.status == "available"
    assert all(request.method == "GET" for request in requests)


async def test_cluster_failure_keeps_inventory_and_marks_it_stale(
    inventory, monkeypatch
):
    inventory.save("net-1", connection())
    inventory.entries["net-1"].snapshot = VirtualizationSnapshot(
        configured=True,
        collected_at="2026-10-13T12:00:00+00:00",
        nodes=[
            NodeInventory(
                id="pve-1",
                name="pve-1",
                guests=[
                    guest(
                        docker=DockerInventory(
                            status="available",
                            collected_at="2026-10-13T12:00:00+00:00",
                            containers=[
                                DockerContainer(id="1", name="web", state="running")
                            ],
                        )
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(
        inventory,
        "_pve",
        AsyncMock(side_effect=httpx.ConnectError("do not expose secret details")),
    )
    result = await inventory.refresh("net-1")
    assert result.stale
    assert "secret details" not in result.error
    assert result.nodes[0].guests[0].docker.status == "stale"
    assert result.nodes[0].guests[0].docker.containers[0].state == "running"
    restored = VirtualizationService(inventory.directory, empty_provider)
    restored.load()
    assert restored.snapshot("net-1").nodes
    assert restored.snapshot("net-1").stale


async def test_stopped_guest_preserves_last_reported_docker_states(inventory):
    inventory.save("net-1", connection())
    current = guest(status="stopped")
    previous = VirtualizationSnapshot(
        nodes=[
            NodeInventory(
                id="pve-1",
                name="pve-1",
                guests=[
                    guest(
                        docker=DockerInventory(
                            status="available",
                            collected_at="2026-10-13T12:00:00+00:00",
                            containers=[
                                DockerContainer(id="1", name="web", state="running")
                            ],
                        )
                    )
                ],
            )
        ]
    )
    config = inventory.entries["net-1"].config.model_copy(
        update={"docker_enabled": True}
    )
    await inventory._docker(current, config, None, previous)
    assert current.docker.status == "stale"
    assert current.docker.containers[0].state == "running"


async def test_routes_require_auth(inventory, monkeypatch, async_client):
    monkeypatch.setattr(app.state, "virtualization", inventory, raising=False)
    inventory.save("net-1", connection())
    response = await async_client.get("/api/networks/net-1/virtualization/connection")
    assert response.status_code == 401


async def test_routes_redact_credentials(inventory, monkeypatch, auth_client):
    monkeypatch.setattr(app.state, "virtualization", inventory, raising=False)
    inventory.save("net-1", connection())
    response = await auth_client.get("/api/networks/net-1/virtualization/connection")
    assert response.status_code == 200
    assert "test-secret" not in response.text
    assert "token_secret" not in response.json()


async def test_routes_validate_ids_and_forward_manual_vs_automatic_refresh(
    inventory, monkeypatch, auth_client
):
    monkeypatch.setattr(app.state, "virtualization", inventory, raising=False)
    inventory.refresh = AsyncMock(return_value=VirtualizationSnapshot())
    response = await auth_client.post(
        "/api/networks/net..1/virtualization/refresh", json={"force": True}
    )
    assert response.status_code == 400
    inventory.refresh.assert_not_called()
    for force in (False, True):
        response = await auth_client.post(
            "/api/networks/net-1/virtualization/refresh", json={"force": force}
        )
        assert response.status_code == 200
        inventory.refresh.assert_awaited_with("net-1", force=force)
