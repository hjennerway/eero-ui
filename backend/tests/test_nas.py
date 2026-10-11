"""QNAP health semantics, unavailable readings and credential boundaries."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from pysnmp.proto.rfc1902 import OctetString
from pysnmp.proto.rfc1905 import NoSuchObject

from app.config import settings
from app.services import nas
from app.services.nas import NASConfig, NASInventory, NASSnapshot


def config(**updates):
    return (
        dict(
            id="qnap",
            network_id="network-123",
            name="QNAP TVS-473e",
            host="192.168.4.2",
            community="private-community",
        )
        | updates
    )


def table(*records):
    return {
        (column, index): value
        for index, record in enumerate(records, 1)
        for column, value in record.items()
    }


def test_qnap_zero_is_good_not_missing_and_unknown_is_never_healthy():
    readings = nas.normalize_disks(
        table(
            {
                2: 1,
                3: 0,
                4: OctetString("Good"),
                5: 0,
                6: 37,
                8: OctetString("WD Red"),
                9: 4_000_000_000_000,
            },
            {2: 2, 3: 0, 4: OctetString("Good"), 5: 1, 6: -1},
            {2: 3, 3: 0, 4: OctetString("Abnormal"), 5: 2},
            {2: 4, 3: 0, 4: OctetString("Good"), 5: -1},
            {2: 5, 3: 0, 5: 0},
            {2: 6, 3: 0, 4: OctetString("Good")},
        ),
        modern=True,
    )
    assert [disk.health for disk in readings] == [
        "good",
        "warning",
        "critical",
        "unknown",
        "unknown",
        "unknown",
    ]
    assert readings[0].enclosure_id == 0
    assert readings[0].bay == 1
    assert readings[0].capacity_bytes == 4_000_000_000_000
    assert readings[0].temperature_c == 37
    assert readings[1].temperature_c is None
    assert readings[3].smart_status == "Unavailable"


def test_legacy_ready_empty_slots_and_unreadable_smart():
    readings = nas.normalize_disks(
        table(
            {
                2: OctetString("HDD1"),
                3: OctetString("35 C/95 F"),
                4: 0,
                6: OctetString("1.5 TB"),
                7: OctetString("GOOD"),
            },
            {4: -5, 3: OctetString("--"), 7: OctetString("N/A")},
            {4: -9, 7: OctetString("GOOD")},
            {4: 0},
        ),
        modern=False,
    )
    assert [disk.health for disk in readings] == [
        "good",
        "unknown",
        "critical",
        "unknown",
    ]
    assert readings[0].status == "Ready"
    assert readings[0].temperature_c == 35
    assert readings[0].capacity_bytes == int(1.5 * 1024**4)
    assert readings[1].status == "No disk"
    assert readings[1].temperature_c is None


def test_usage_handles_full_empty_missing_and_inconsistent_readings():
    readings = nas.normalize_storage(
        table(
            {2: 1, 3: 1000, 4: 0, 5: OctetString("Ready"), 8: OctetString("Data")},
            {2: 2, 3: 1000, 4: 1000, 5: OctetString("Ready")},
            {2: 3, 3: 0, 4: 0},
            {2: 4, 3: 1000},
            {2: 5, 3: 1000, 4: 2000},
        ),
        "volume",
    )
    assert readings[0].name == "Data" and readings[0].used_percent == 100
    assert readings[0].free_bytes == 0
    assert readings[1].used_percent == 0
    assert [r.used_percent for r in readings[2:]] == [None, None, None]
    assert readings[-1].free_bytes is None
    pools = nas.normalize_storage(table({2: 0, 3: 100, 4: 50, 5: -1}), "pool")
    assert pools[0].name == "Pool 0" and pools[0].health == "warning"
    raids = nas.normalize_storage(
        table({2: 1, 5: OctetString("Degraded"), 7: OctetString("RAID 5")}), "raid"
    )
    assert raids[0].health == "critical" and raids[0].raid_level == "RAID 5"


@pytest.mark.parametrize("modern", [True, False])
def test_qts_kib_volume_counters_require_confirmation_from_labelled_size(modern):
    total_column, free_column = (3, 4) if modern else (4, 5)
    numeric = table(
        {total_column: 1_638_850_228, free_column: 708_585_056},
        {total_column: 1024**4, free_column: 0},
        {total_column: 1024, free_column: 512},
    )
    legacy = table({4: "1.53 TB"}, {4: "1.00 TB"}, {4: "2.00 TB"})
    corrected = nas.verified_volume_units(numeric, legacy, modern=modern)
    readings = nas.normalize_storage(corrected, "volume" if modern else "system_volume")
    assert readings[0].capacity_bytes == 1_638_850_228 * 1024
    assert readings[0].free_bytes == 708_585_056 * 1024
    assert readings[0].used_percent == 56.8
    assert readings[1].capacity_bytes == 1024**4
    assert readings[1].free_bytes == 0
    assert readings[2].capacity_bytes == 1024
    assert numeric[(total_column, 1)] == 1_638_850_228
    assert nas.verified_volume_units(numeric, {}, modern=modern) == numeric


@pytest.mark.parametrize("value", [None, "N/A", "--", "-1", "not known"])
def test_missing_numbers_are_not_zero(value):
    assert nas.capacity(value) is None
    assert nas.temperature(value) is None


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "224.0.0.1", "localhost", "::1"]
)
def test_only_explicit_lan_addresses(host):
    with pytest.raises(ValidationError):
        NASConfig(**config(host=host))


async def test_poll_falls_back_to_ex_tables_and_closes_engine(monkeypatch):
    engines = []

    class Engine:
        closed = False

        def __init__(self):
            engines.append(self)

        def close_dispatcher(self):
            self.closed = True

    monkeypatch.setattr(nas, "SnmpEngine", Engine)
    monkeypatch.setattr(nas, "ObjectIdentity", lambda root: root)
    monkeypatch.setattr(nas, "ObjectType", lambda identity: identity)
    monkeypatch.setattr(
        nas.UdpTransportTarget, "create", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        nas,
        "get_cmd",
        AsyncMock(
            return_value=(
                None,
                0,
                0,
                [
                    (None, OctetString("QNAP")),
                    (None, NoSuchObject()),
                    (None, OctetString("TVS-473e")),
                ],
            )
        ),
    )
    seen = []

    async def walk(*args, **kwargs):
        root = args[6]
        seen.append(root)
        source = {
            nas.ROOTS["disks_ex"]: table(
                {2: OctetString("HDD1"), 3: 33, 4: 0, 6: 1000, 7: OctetString("GOOD")}
            ),
            nas.ROOTS["volumes_ex"]: table(
                {2: OctetString("Data"), 4: 1000, 5: 500, 6: OctetString("Ready")}
            ),
        }.get(root, {})
        bindings = [
            (tuple(map(int, root.split("."))) + key, value)
            for key, value in source.items()
        ]
        yield None, 0, 0, bindings

    monkeypatch.setattr(nas, "bulk_walk_cmd", walk)
    result = await nas.poll_nas(NASConfig(**config()))
    assert result.status == "online" and result.observed_at
    assert result.model == "TVS-473e"
    assert result.disks[0].health == "good"
    assert result.volumes[0].used_percent == 50
    assert nas.ROOTS["disks_legacy"] not in seen
    assert engines[0].closed
    assert "private-community" not in result.model_dump_json()


async def test_poll_error_is_sanitized_and_has_no_current_readings(monkeypatch):
    monkeypatch.setattr(
        nas.UdpTransportTarget,
        "create",
        AsyncMock(side_effect=RuntimeError("private-community")),
    )
    result = await nas.poll_nas(NASConfig(**config()))
    assert result.status == "unreachable" and result.observed_at is None
    assert result.disks == [] and result.volumes == []
    assert "private-community" not in result.model_dump_json()


async def test_inventory_is_scoped_and_replaces_old_health_on_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "nas.json"
    path.write_text(json.dumps([config(), config(id="other", network_id="other")]))
    now = [0]
    monkeypatch.setattr(nas.time, "monotonic", lambda: now[0])
    poll = AsyncMock(
        side_effect=[
            NASSnapshot(
                id="qnap",
                name="QNAP",
                host="192.168.4.2",
                status="online",
                disks=[nas.NASDisk(id="1", name="Disk 1", health="good")],
            ),
            NASSnapshot(id="qnap", name="QNAP", host="192.168.4.2"),
            NASSnapshot(id="qnap", name="QNAP", host="192.168.4.2"),
        ]
    )
    monkeypatch.setattr(nas, "poll_nas", poll)
    inventory = NASInventory()
    assert (await inventory.read(str(path), "network-123"))[0].disks
    assert (await inventory.read(str(path), "network-123"))[0].disks
    assert poll.await_count == 1
    assert await inventory.read(str(path), "missing") == []
    now[0] = 299
    assert (await inventory.read(str(path), "network-123"))[0].disks
    assert poll.await_count == 1
    now[0] = 301
    stale = (await inventory.read(str(path), "network-123"))[0]
    assert stale.stale and stale.disks
    await asyncio.gather(*inventory.tasks.values())
    assert (await inventory.read(str(path), "network-123"))[0].disks == []
    path.write_text(json.dumps([config(community="new-community")]))
    await inventory.read(str(path), "network-123")
    assert poll.await_count == 3
    assert all(str(call.args[0].host) == "192.168.4.2" for call in poll.await_args_list)


async def test_api_requires_auth(async_client):
    assert (await async_client.get("/api/nas")).status_code == 401


async def test_api_configuration_errors_and_empty_state(
    auth_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "nas_file", None)
    response = await auth_client.get("/api/nas")
    assert response.status_code == 200 and response.json() == []
    path = tmp_path / "nas.json"
    path.write_text('{"community": "private-community"}')
    monkeypatch.setattr(settings, "nas_file", str(path))
    response = await auth_client.get("/api/nas")
    assert response.status_code == 503 and "private-community" not in response.text


async def test_api_only_polls_requested_network(auth_client, tmp_path, monkeypatch):
    path = tmp_path / "nas.json"
    path.write_text(
        json.dumps([config(), config(id="other", network_id="network-456")])
    )
    monkeypatch.setattr(settings, "nas_file", str(path))

    async def poll(config):
        return NASSnapshot(id=config.id, name=config.name, host=str(config.host))

    mock = AsyncMock(side_effect=poll)
    monkeypatch.setattr(nas, "poll_nas", mock)
    response = await auth_client.get("/api/nas?network_id=network-456")
    assert response.status_code == 200
    assert [n["id"] for n in response.json()] == ["other"]
    assert "community" not in response.text
    assert mock.await_count == 1


async def test_refresh_deduplicates_requests_and_close_cancels_pending_poll(
    tmp_path, monkeypatch
):
    path = tmp_path / "nas.json"
    path.write_text(json.dumps([config()]))
    started, finish = asyncio.Event(), asyncio.Event()

    async def poll(config):
        started.set()
        await finish.wait()
        return NASSnapshot(id=config.id, name=config.name, host=str(config.host))

    mock = AsyncMock(side_effect=poll)
    monkeypatch.setattr(nas, "poll_nas", mock)
    inventory = NASInventory()
    first = asyncio.create_task(inventory.refresh_all(str(path)))
    await started.wait()
    second = asyncio.create_task(inventory.read(str(path), "network-123", refresh=True))
    await asyncio.sleep(0.05)
    assert mock.await_count == 1
    finish.set()
    await asyncio.gather(first, second)
    finish.clear()
    started.clear()
    pending = asyncio.create_task(
        inventory.read(str(path), "network-123", refresh=True)
    )
    await started.wait()
    await inventory.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not inventory.tasks and not inventory.cache


async def test_background_polling_publishes_all_networks_without_page_visit(
    tmp_path, monkeypatch
):
    path = tmp_path / "nas.json"
    path.write_text(json.dumps([config(), config(id="other", network_id="other")]))
    monkeypatch.setattr(
        nas,
        "poll_nas",
        AsyncMock(
            side_effect=lambda c: NASSnapshot(id=c.id, name=c.name, host=str(c.host))
        ),
    )
    published = []
    ready = asyncio.Event()

    def publish(networks):
        published.append(networks)
        ready.set()

    inventory = NASInventory()
    task = asyncio.create_task(inventory.run_forever(str(path), publish))
    await asyncio.wait_for(ready.wait(), 2)
    assert set(published[0]) == {"network-123", "other"}
    assert len(inventory.cache) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await inventory.close()


async def test_manual_api_refresh_bypasses_five_minute_cache(
    auth_client, tmp_path, monkeypatch
):
    path = tmp_path / "nas.json"
    path.write_text(json.dumps([config()]))
    monkeypatch.setattr(settings, "nas_file", str(path))
    inventory = NASInventory()
    monkeypatch.setattr("app.routes.nas.nas_inventory", inventory)
    poll = AsyncMock(
        side_effect=lambda c: NASSnapshot(id=c.id, name=c.name, host=str(c.host))
    )
    monkeypatch.setattr(nas, "poll_nas", poll)
    for url in ("/api/nas", "/api/nas", "/api/nas?refresh=true"):
        assert (await auth_client.get(url)).status_code == 200
    assert poll.await_count == 2
