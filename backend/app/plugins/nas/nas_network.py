"""NAS LAN throughput from IF-MIB Counter64 octets, using the existing SNMP access.

Only physical Ethernet interfaces are selected automatically. Bridges, bonds,
VLANs and container interfaces would double-count traffic. An explicit list of
ifName values in network_interfaces overrides that selection (e.g. one bond).
RFC 2863 requires discarding deltas across counter discontinuities/reboots.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    bulk_walk_cmd,
    get_cmd,
)

from .nas import _MISSING, NASConfig, NASInventory, integer, rows, text_value

INTERVAL_SECONDS = 30
EXPIRY_SECONDS = 90
_LOGGER = logging.getLogger(__name__)
_IF_TABLE = "1.3.6.1.2.1.2.2.1"
_IF_X_TABLE = "1.3.6.1.2.1.31.1.1.1"
_PHYSICAL_NAME = re.compile(r"(?:eth\d+|en[a-zA-Z0-9]+)")


class NASNetworkSnapshot(BaseModel):
    id: str
    name: str
    observed_at: str | None = None
    interfaces: list[str] = Field(default_factory=list)
    receive_mbps: float | None = None
    transmit_mbps: float | None = None


@dataclass(frozen=True)
class InterfaceCounters:
    index: int
    received: int
    transmitted: int
    discontinuity: int | None
    up: bool


@dataclass(frozen=True)
class NetworkCounters:
    uptime: int
    sampled_at: float
    interfaces: dict[str, InterfaceCounters]


def select_interfaces(config: NASConfig, table: dict, extended: dict) -> dict:
    result = {}
    basic_rows = rows(table)
    for index, row in rows(extended).items():
        name = text_value(row.get(1))
        basic = basic_rows.get(index, {})
        if not name:
            continue
        if config.network_interfaces is not None:
            selected = name in config.network_interfaces
        else:
            selected = (
                integer(basic.get(3)) == 6
                and integer(row.get(17)) != 2
                and _PHYSICAL_NAME.fullmatch(name) is not None
            )
        if not selected:
            continue
        received, transmitted = integer(row.get(6)), integer(row.get(10))
        if received is None or transmitted is None or min(received, transmitted) < 0:
            raise ValueError("Incomplete NAS interface counters")
        if integer(basic.get(8)) is None:
            raise ValueError("NAS interface status unavailable")
        result[name] = InterfaceCounters(
            index,
            received,
            transmitted,
            integer(row.get(19)),
            integer(basic.get(8)) == 1,
        )
    return result


def rates(
    previous: NetworkCounters | None, current: NetworkCounters
) -> tuple[float | None, float | None]:
    elapsed = current.sampled_at - previous.sampled_at if previous else 0
    if (
        previous is None
        or not 0 < elapsed < EXPIRY_SECONDS
        or current.uptime < previous.uptime
        or not current.interfaces
        or current.interfaces.keys() != previous.interfaces.keys()
    ):
        return None, None
    received = transmitted = 0
    for name, reading in current.interfaces.items():
        before = previous.interfaces[name]
        if (
            reading.index != before.index
            or reading.discontinuity != before.discontinuity
            or reading.up != before.up
            or reading.received < before.received
            or reading.transmitted < before.transmitted
        ):
            return None, None
        if reading.up:
            received += reading.received - before.received
            transmitted += reading.transmitted - before.transmitted
    return received * 8 / elapsed / 1_000_000, transmitted * 8 / elapsed / 1_000_000


async def read_counters(config: NASConfig) -> NetworkCounters:
    engine = SnmpEngine()
    try:
        async with asyncio.timeout(20):
            target = await UdpTransportTarget.create(
                (str(config.host), config.port), timeout=3, retries=0
            )
            auth = CommunityData(config.community.get_secret_value(), mpModel=1)
            context = ContextData()
            error, status, _, values = await get_cmd(
                engine,
                auth,
                target,
                context,
                ObjectType(ObjectIdentity("1.3.6.1.2.1.1.3.0")),
                lookupMib=False,
            )
            uptime = (
                integer(values[0][1]) if values and not error and not status else None
            )
            if uptime is None:
                raise ValueError("NAS interface counters unavailable")

            async def walk(root: str) -> dict:
                prefix = tuple(int(n) for n in root.split("."))
                table = {}
                async for error, status, _, bindings in bulk_walk_cmd(
                    engine,
                    auth,
                    target,
                    context,
                    0,
                    24,
                    ObjectType(ObjectIdentity(root)),
                    lexicographicMode=False,
                    lookupMib=False,
                    maxRows=4097,
                ):
                    if error or status:
                        raise ValueError("NAS interface counters unavailable")
                    for oid, value in bindings:
                        parts = tuple(oid)
                        if parts[: len(prefix)] == prefix and not isinstance(
                            value, _MISSING
                        ):
                            table[parts[len(prefix) :]] = value
                    if len(table) > 4096:
                        raise ValueError("NAS interface table exceeded size limit")
                return table

            basic = await walk(_IF_TABLE)
            extended = await walk(_IF_X_TABLE)
            interfaces = select_interfaces(config, basic, extended)
            if not interfaces or (
                config.network_interfaces is not None
                and set(interfaces) != set(config.network_interfaces)
            ):
                raise ValueError("No complete NAS interface counters")
            return NetworkCounters(uptime, time.monotonic(), interfaces)
    finally:
        engine.close_dispatcher()


class NASNetworkCollector:
    def __init__(self) -> None:
        self.previous: dict[str, NetworkCounters] = {}
        # Separate from storage inventory: network polling must not invalidate it.
        self.inventory = NASInventory()

    async def collect(self, config: NASConfig) -> NASNetworkSnapshot:
        key = self.inventory._key(config)
        snapshot = NASNetworkSnapshot(id=config.id, name=config.name)
        try:
            current = await read_counters(config)
            snapshot.receive_mbps, snapshot.transmit_mbps = rates(
                self.previous.get(key), current
            )
            self.previous[key] = current
            snapshot.interfaces = sorted(current.interfaces)
            snapshot.observed_at = datetime.now(UTC).isoformat()
        except Exception:
            # Failed polls and changed credentials must establish a fresh baseline.
            self.previous.pop(key, None)
            _LOGGER.warning("NAS network counters unavailable")
        return snapshot

    async def run_forever(self, path: str | None, publish: Callable | None) -> None:
        try:
            while True:
                started = time.monotonic()
                try:
                    configs = await self.inventory._configs(path)
                    keys = {self.inventory._key(c) for c in configs}
                    self.previous = {
                        k: v for k, v in self.previous.items() if k in keys
                    }
                    snapshots = await asyncio.gather(
                        *(self.collect(c) for c in configs)
                    )
                    networks = {}
                    for config, snapshot in zip(configs, snapshots):
                        networks.setdefault(config.network_id, []).append(snapshot)
                    if publish:
                        publish(networks)
                except (OSError, ValueError):
                    self.previous.clear()
                    _LOGGER.warning("NAS network configuration could not be read")
                    if publish:
                        publish({})
                await asyncio.sleep(
                    max(1, INTERVAL_SECONDS - (time.monotonic() - started))
                )
        finally:
            self.previous.clear()
