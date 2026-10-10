"""Read-only SNMP switch inventory. No SET operations or network scanning.

Uses IF-MIB, BRIDGE-MIB, Q-BRIDGE-MIB and LLDP-MIB numeric OIDs so no
MIB compiler/download is needed. Bridge ports, ifIndex and LLDP local port
numbers are different namespaces and are explicitly joined.
"""

import asyncio
import json
import time
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, TypeAdapter, field_validator
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
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject


class SwitchConfig(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    network_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    host: IPv4Address
    port: int = Field(default=161, ge=1, le=65535)
    community: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("host")
    @classmethod
    def private_lan_only(cls, host: IPv4Address) -> IPv4Address:
        if not any(
            host in IPv4Network(cidr)
            for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ):
            raise ValueError("Use a private LAN IPv4 address")
        return host


class SwitchNeighbor(BaseModel):
    chassis_mac: str | None = None
    name: str | None = None
    port: str | None = None


class SwitchPort(BaseModel):
    id: int  # ifIndex, never an assumed physical port number
    name: str
    kind: Literal["physical", "lag"] = "physical"
    status: Literal["up", "down", "unknown"] = "unknown"
    speed_mbps: int | None = None
    macs: list[str] = Field(default_factory=list)
    neighbors: list[SwitchNeighbor] = Field(default_factory=list)


class SwitchSnapshot(BaseModel):
    id: str
    name: str
    host: str
    status: Literal["online", "unreachable"]
    model: str | None = None
    mac: str | None = None
    observed_at: str | None = None
    ports: list[SwitchPort] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# Keep requests small for older GS110TP firmware; cap each table and total time.
ROOTS = {
    "if_name": "1.3.6.1.2.1.31.1.1.1.1",
    "if_alias": "1.3.6.1.2.1.31.1.1.1.18",
    "if_descr": "1.3.6.1.2.1.2.2.1.2",
    "if_type": "1.3.6.1.2.1.2.2.1.3",
    "if_mac": "1.3.6.1.2.1.2.2.1.6",
    "if_status": "1.3.6.1.2.1.2.2.1.8",
    "if_speed": "1.3.6.1.2.1.2.2.1.5",
    "if_high_speed": "1.3.6.1.2.1.31.1.1.1.15",
    "bridge_port": "1.3.6.1.2.1.17.1.4.1.2",
    "fdb_port": "1.3.6.1.2.1.17.4.3.1.2",
    "fdb_status": "1.3.6.1.2.1.17.4.3.1.3",
    "qfdb_port": "1.3.6.1.2.1.17.7.1.2.2.1.2",
    "qfdb_status": "1.3.6.1.2.1.17.7.1.2.2.1.3",
    "local_subtype": "1.0.8802.1.1.2.1.3.7.1.2",
    "local_port": "1.0.8802.1.1.2.1.3.7.1.3",
    "chassis_subtype": "1.0.8802.1.1.2.1.4.1.1.4",
    "chassis": "1.0.8802.1.1.2.1.4.1.1.5",
    "remote_port": "1.0.8802.1.1.2.1.4.1.1.7",
    "remote_port_subtype": "1.0.8802.1.1.2.1.4.1.1.6",
    "remote_name": "1.0.8802.1.1.2.1.4.1.1.9",
}
Table = dict[tuple[int, ...], Any]


def octets(value: Any) -> bytes:
    return value.asOctets() if hasattr(value, "asOctets") else b""


def mac_text(value: Any) -> str | None:
    raw = octets(value)
    if len(raw) != 6 or raw == b"\0" * 6 or raw[0] & 1:
        return None
    return raw.hex(":")


def text_value(value: Any) -> str:
    return octets(value).decode("utf-8", "replace").strip("\0 ")[:256]


def normalize_ports(tables: dict[str, Table]) -> list[SwitchPort]:
    """Join only unambiguous LLDP interface IDs; don't assume port == ifIndex."""

    def table(name: str) -> Table:
        return tables.get(name, {})

    ports: dict[int, SwitchPort] = {}
    for key, kind in table("if_type").items():
        # Physical Ethernet + LAGs. Exclude CPU/internal interfaces and VLANs.
        if len(key) != 1 or int(kind) not in (6, 161):
            continue
        idx = key[0]
        name = text_value(table("if_name").get(key))
        description = text_value(table("if_descr").get(key))
        if "cpu" in description.lower():
            continue
        state = int(table("if_status").get(key, 0))
        high = int(table("if_high_speed").get(key, 0))
        speed = high or int(table("if_speed").get(key, 0)) // 1_000_000
        ports[idx] = SwitchPort(
            id=idx,
            name=name or description or str(idx),
            kind="lag" if int(kind) == 161 else "physical",
            status={1: "up", 2: "down"}.get(state, "unknown"),
            speed_mbps=speed if state == 1 and speed > 0 else None,
        )

    for prefix, length in (("fdb", 6), ("qfdb", 7)):
        for key, bridge in table(f"{prefix}_port").items():
            # Q-BRIDGE's leading index is FDB ID, not necessarily VLAN ID.
            if len(key) != length or int(table(f"{prefix}_status").get(key, 0)) != 3:
                continue
            idx = int(table("bridge_port").get((int(bridge),), 0))
            if idx not in ports or not all(0 <= byte <= 255 for byte in key[-6:]):
                continue
            raw = bytes(key[-6:])
            if raw != b"\0" * 6 and not raw[0] & 1:
                ports[idx].macs.append(raw.hex(":"))

    for key, chassis in table("chassis").items():
        if len(key) != 3:
            continue
        local = (key[1],)
        port_value = table("local_port").get(local)
        subtype = int(table("local_subtype").get(local, 0))
        candidates = []
        for idx, port in ports.items():
            if subtype == 3:
                matches = bool(mac_text(port_value)) and mac_text(
                    port_value
                ) == mac_text(table("if_mac").get((idx,)))
            elif subtype == 1:
                matches = bool(text_value(port_value)) and text_value(
                    port_value
                ) == text_value(table("if_alias").get((idx,)))
            elif subtype in (5, 7):
                matches = text_value(port_value) in (
                    port.name,
                    text_value(table("if_descr").get((idx,))),
                ) and bool(text_value(port_value))
            else:
                matches = False
            if matches:
                candidates.append(idx)
        if len(candidates) != 1:
            continue
        ports[candidates[0]].neighbors.append(
            SwitchNeighbor(
                chassis_mac=(
                    mac_text(chassis)
                    if int(table("chassis_subtype").get(key, 0)) == 4
                    else None
                ),
                name=text_value(table("remote_name").get(key))
                or (
                    text_value(chassis)
                    if int(table("chassis_subtype").get(key, 0)) == 7
                    else None
                ),
                port=(
                    mac_text(table("remote_port").get(key))
                    if int(table("remote_port_subtype").get(key, 0)) == 3
                    else text_value(table("remote_port").get(key)) or None
                ),
            )
        )
    for port in ports.values():
        port.macs = sorted(set(port.macs))
    return sorted(ports.values(), key=lambda port: port.id)


async def poll_switch(config: SwitchConfig) -> SwitchSnapshot:
    snapshot = SwitchSnapshot(
        id=config.id, name=config.name, host=str(config.host), status="unreachable"
    )
    engine = SnmpEngine()
    try:
        async with asyncio.timeout(12):
            target = await UdpTransportTarget.create(
                (str(config.host), config.port), timeout=0.8, retries=0
            )
            auth = CommunityData(config.community.get_secret_value(), mpModel=1)
            context = ContextData()
            error, status, _, values = await get_cmd(
                engine,
                auth,
                target,
                context,
                ObjectType(ObjectIdentity("1.3.6.1.2.1.1.1.0")),
                ObjectType(ObjectIdentity("1.3.6.1.2.1.17.1.1.0")),
                lookupMib=False,
            )
            if error or status:
                raise RuntimeError("Switch did not respond")
            snapshot.status = "online"
            snapshot.model = text_value(values[0][1]) or None
            snapshot.mac = mac_text(values[1][1])
            tables: dict[str, Table] = {}
            for name, root in ROOTS.items():
                rows: Table = {}
                root_parts = tuple(int(n) for n in root.split("."))
                async for error, status, _, bindings in bulk_walk_cmd(
                    engine,
                    auth,
                    target,
                    context,
                    0,
                    15,
                    ObjectType(ObjectIdentity(root)),
                    lexicographicMode=False,
                    lookupMib=False,
                    maxRows=2049,
                ):
                    if error or status:
                        snapshot.warnings.append(f"Could not read {name} table")
                        rows = {}
                        break
                    for oid, value in bindings:
                        parts = tuple(oid)
                        if parts[: len(root_parts)] != root_parts or isinstance(
                            value, (NoSuchObject, NoSuchInstance, EndOfMibView)
                        ):
                            continue
                        rows[parts[len(root_parts) :]] = value
                    if len(rows) > 2048:
                        snapshot.warnings.append(
                            f"{name} table exceeded the size limit"
                        )
                        rows = (
                            {}
                        )  # Incomplete tables must not imply a physical connection.
                        break
                tables[name] = rows
            snapshot.ports = normalize_ports(tables)
            snapshot.observed_at = datetime.now(UTC).isoformat()
    except Exception:
        # Never leak SNMP credentials, library errors, or cached/stale wiring.
        snapshot.status = "unreachable"
        snapshot.ports = []
        snapshot.warnings = [
            "Could not read switch data. Check its address and read-only SNMP access."
        ]
    finally:
        engine.close_dispatcher()
    return snapshot


class SwitchInventory:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.cache: dict[str, tuple[float, SwitchSnapshot]] = {}

    async def read(self, path: str | None, network_id: str) -> list[SwitchSnapshot]:
        if not path:
            return []
        raw = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        if len(raw) > 65536:
            raise ValueError("Switch configuration is too large")
        configs = TypeAdapter(list[SwitchConfig]).validate_python(json.loads(raw))
        if len(configs) > 16 or len({c.id for c in configs}) != len(configs):
            raise ValueError("Switch IDs must be unique, with at most 16 switches")
        selected = [c for c in configs if c.network_id == network_id]
        if len(selected) > 4:
            raise ValueError("At most four switches can be polled per network")
        async with self.lock:
            # Credentials are part of the key only in memory, never returned/logged.
            keys = {
                c.id: c.model_dump_json() + c.community.get_secret_value()
                for c in configs
            }
            self.cache = {k: v for k, v in self.cache.items() if k in keys.values()}

            async def read_one(config: SwitchConfig) -> SwitchSnapshot:
                key = keys[config.id]
                cached = self.cache.get(key)
                if cached and time.monotonic() - cached[0] < 30:
                    return cached[1]
                result = await poll_switch(config)
                self.cache[key] = (time.monotonic(), result)
                return result

            # Limit device concurrency; requests to one switch remain sequential.
            semaphore = asyncio.Semaphore(4)

            async def limited(config: SwitchConfig) -> SwitchSnapshot:
                async with semaphore:
                    return await read_one(config)

            return list(await asyncio.gather(*(limited(c) for c in selected)))


switch_inventory = SwitchInventory()
