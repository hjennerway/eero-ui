"""LAN direction, physical interface selection, discontinuities and failed polls."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.plugins.nas import nas_network as net
from app.plugins.nas.nas import NASConfig


def config(**updates):
    return NASConfig(
        **(
            dict(
                id="qnap",
                name="QNAP TVS-473e",
                network_id="home",
                host="192.168.4.2",
                community="private-community",
            )
            | updates
        )
    )


def sample(received=2**40, transmitted=2**39, **updates):
    return net.NetworkCounters(
        **(
            dict(
                uptime=10000,
                sampled_at=100,
                interfaces={
                    "eth0": net.InterfaceCounters(2, received, transmitted, 0, True)
                },
            )
            | updates
        )
    )


def test_64_bit_octets_convert_to_mbit_per_second_in_nas_direction():
    before = sample()
    after = sample(
        2**40 + 30 * 125000000, 2**39 + 30 * 12500000, sampled_at=130, uptime=13000
    )
    assert net.rates(before, after) == (1000, 100)
    assert net.rates(before, replace(before, sampled_at=130)) == (0, 0)
    assert net.rates(None, after) == (None, None)


@pytest.mark.parametrize(
    "change",
    [
        {"uptime": 1},
        {"sampled_at": 100},
        {"sampled_at": 200},
        {"received": 0},
        {"transmitted": 0},
        {"discontinuity": 99},
        {"index": 5},
        {"up": False},
        {"rename": True},
    ],
)
def test_resets_gaps_interface_changes_and_restarts_do_not_create_spikes(change):
    before = sample()
    after = replace(before, sampled_at=130, uptime=13000)
    fields = {k: v for k, v in change.items() if k in {"uptime", "sampled_at"}}
    interface = replace(
        after.interfaces["eth0"],
        **{
            k: v
            for k, v in change.items()
            if k not in {"uptime", "sampled_at", "rename"}
        }
    )
    after = replace(
        after,
        **fields,
        interfaces={"eth1" if change.get("rename") else "eth0": interface}
    )
    assert net.rates(before, after) == (None, None)


def test_only_physical_ethernet_is_automatic_and_explicit_selection_is_respected():
    names = ["eth0", "eth1", "bond0", "br0", "veth42", "eth0.42", "lo", "enp3s0"]
    basic, extended = {}, {}
    for index, name in enumerate(names, 1):
        basic[(3, index)], basic[(8, index)] = 6, 1
        for column, value in {1: name, 6: 1000, 10: 2000, 19: 0}.items():
            extended[(column, index)] = value
    assert set(net.select_interfaces(config(), basic, extended)) == {
        "eth0",
        "eth1",
        "enp3s0",
    }
    assert set(
        net.select_interfaces(config(network_interfaces=["bond0"]), basic, extended)
    ) == {"bond0"}
    # Explicitly virtual connectors and missing 64-bit counters are not real zeroes.
    extended[(17, 1)] = 2
    del extended[(6, 2)]
    with pytest.raises(ValueError, match="Incomplete"):
        net.select_interfaces(config(), basic, extended)


async def test_failed_poll_clears_baseline_and_does_not_expose_credentials(monkeypatch):
    collector = net.NASNetworkCollector()
    monkeypatch.setattr(
        net,
        "read_counters",
        AsyncMock(
            side_effect=[
                sample(),
                sample(2**40 + 3750000, sampled_at=130),
                RuntimeError("private-community"),
                sample(sampled_at=160),
            ]
        ),
    )
    assert (await collector.collect(config())).receive_mbps is None
    assert (await collector.collect(config())).receive_mbps == 1
    failed = await collector.collect(config())
    assert failed.observed_at is None and failed.receive_mbps is None
    assert "private-community" not in failed.model_dump_json()
    assert (await collector.collect(config())).receive_mbps is None


async def test_snmp_reads_use_counter64_and_always_close_engine(monkeypatch):
    engine = type(
        "Engine", (), {"close_dispatcher": lambda self: setattr(self, "closed", True)}
    )()
    monkeypatch.setattr(net, "SnmpEngine", lambda: engine)
    monkeypatch.setattr(net, "ObjectIdentity", lambda oid: oid)
    monkeypatch.setattr(net, "ObjectType", lambda oid: oid)
    monkeypatch.setattr(
        net.UdpTransportTarget, "create", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        net, "get_cmd", AsyncMock(return_value=(None, 0, 0, [(None, 10000)]))
    )

    async def walk(*args, **kwargs):
        root = args[6]
        columns = (
            {3: 6, 8: 1}
            if root == net._IF_TABLE
            else {1: "eth0", 6: 2**40, 10: 2**39, 17: 1, 19: 0}
        )
        prefix = tuple(map(int, root.split(".")))
        yield None, 0, 0, [(prefix + (col, 2), value) for col, value in columns.items()]

    monkeypatch.setattr(net, "bulk_walk_cmd", walk)
    result = await net.read_counters(config())
    assert result.interfaces["eth0"].received == 2**40 and engine.closed
    monkeypatch.setattr(net, "get_cmd", AsyncMock(side_effect=RuntimeError()))
    engine.closed = False
    with pytest.raises(RuntimeError):
        await net.read_counters(config())
    assert engine.closed
