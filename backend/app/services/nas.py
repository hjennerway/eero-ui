"""Read-only QNAP NAS-MIB polling of explicitly configured LAN devices.

Numeric OIDs follow QNAP's NAS-MIB (24681), as distributed in
https://github.com/librenms/librenms/blob/master/mibs/qnap/NAS-MIB.
Prefer Storage Manager tables, then the EX and legacy system tables.
SMART summaries are not individual SMART attributes or self-test results.
"""

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
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

Health = Literal["good", "warning", "critical", "unknown"]
Table = dict[tuple[int, ...], Any]
_MISSING = (NoSuchObject, NoSuchInstance, EndOfMibView)
_BASE = "1.3.6.1.4.1.24681"
_STORAGE = f"{_BASE}.1.4.1.1.1"
ROOTS = {
    "disks": f"{_STORAGE}.1.5.2.1",
    "volumes": f"{_STORAGE}.2.3.2.1",
    "pools": f"{_STORAGE}.2.2.2.1",
    "raids": f"{_STORAGE}.2.1.2.1",
    "disks_ex": f"{_BASE}.1.3.11.1",
    "disks_legacy": f"{_BASE}.1.2.11.1",
    "volumes_ex": f"{_BASE}.1.3.17.1",
    "volumes_legacy": f"{_BASE}.1.2.17.1",
}


class NASConfig(BaseModel):
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


class NASDisk(BaseModel):
    id: str  # SNMP table index; distinct from enclosure/bay IDs
    name: str
    enclosure_id: int | None = None
    bay: int | None = None
    model: str | None = None
    status: str | None = None
    smart_status: str | None = None
    health: Health = "unknown"
    temperature_c: float | None = None
    capacity_bytes: int | None = None


class NASStorage(BaseModel):
    id: str
    name: str
    status: str | None = None
    health: Health = "unknown"
    raid_level: str | None = None
    capacity_bytes: int | None = None
    free_bytes: int | None = None
    used_bytes: int | None = None
    used_percent: float | None = None


class NASSnapshot(BaseModel):
    id: str
    name: str
    host: str
    status: Literal["online", "unreachable"] = "unreachable"
    model: str | None = None
    observed_at: str | None = None
    disks: list[NASDisk] = Field(default_factory=list)
    volumes: list[NASStorage] = Field(default_factory=list)
    pools: list[NASStorage] = Field(default_factory=list)
    raids: list[NASStorage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def text_value(value: Any) -> str | None:
    if value is None or isinstance(value, _MISSING):
        return None
    if hasattr(value, "asOctets"):
        value = value.asOctets().decode("utf-8", "replace")
    return str(value).strip("\0 ")[:256] or None


def integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def capacity(value: Any) -> int | None:
    """EX/Storage Manager integers are bytes; legacy strings specify units."""
    text = text_value(value)
    if not text:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTPE]?)(?:i?B)?", text, re.I)
    if not match:
        return None
    amount, unit = match.groups()
    return int(Decimal(amount) * 1024 ** " KMGTPE".index(unit.upper() or " "))


def temperature(value: Any) -> float | None:
    text = text_value(value)
    if not text:
        return None
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(?:°?\s*C(?:elsius)?(?:\s*/.*)?)?", text, re.I
    )
    # Negative/zero values often mean unsupported, not a cold disk.
    return float(match[1]) if match and 0 < float(match[1]) <= 150 else None


def health(value: str | None) -> Health:
    status = (value or "").strip().casefold()
    if status in {"good", "normal", "ready", "ok"}:
        return "good"
    if any(
        term in status
        for term in ("abnormal", "failed", "error", "degraded", "invalid")
    ):
        return "critical"
    if any(term in status for term in ("warning", "rebuild", "recover", "synchroniz")):
        return "warning"
    return "unknown"


def _combined_health(*values: Health) -> Health:
    if "critical" in values:
        return "critical"
    if "warning" in values:
        return "warning"
    # Missing SMART or operational status cannot imply a healthy disk.
    return "good" if all(value == "good" for value in values) else "unknown"


def rows(table: Table) -> dict[int, dict[int, Any]]:
    result: dict[int, dict[int, Any]] = {}
    for key, value in table.items():
        if len(key) == 2 and key[1] > 0:
            result.setdefault(key[1], {})[key[0]] = value
    return dict(sorted(result.items()))


def normalize_disks(table: Table, *, modern: bool) -> list[NASDisk]:
    result = []
    for index, row in rows(table).items():
        if modern:
            bay, enclosure = integer(row.get(2)), integer(row.get(3))
            status = text_value(row.get(4))
            smart = {0: "Good", 1: "Warning", 2: "Abnormal", -1: "Unavailable"}.get(
                integer(row.get(5))
            )
            name = (
                f"Enclosure {enclosure} · Disk {bay}"
                if enclosure is not None and bay is not None
                else f"Disk {index}"
            )
            model, temp, size = row.get(8), row.get(6), row.get(9)
        else:
            bay, enclosure = None, None
            status = {
                0: "Ready",
                -5: "No disk",
                -6: "Invalid",
                -9: "Read/write error",
                -4: "Unknown",
            }.get(integer(row.get(4)))
            smart = text_value(row.get(7))
            name = text_value(row.get(2)) or f"Disk {index}"
            model, temp, size = row.get(5), row.get(3), row.get(6)
        result.append(
            NASDisk(
                id=str(index),
                name=name,
                enclosure_id=enclosure,
                bay=bay,
                model=text_value(model),
                status=status,
                smart_status=smart,
                health=_combined_health(health(status), health(smart)),
                temperature_c=temperature(temp),
                capacity_bytes=capacity(size),
            )
        )
    return result


def normalize_storage(table: Table, kind: str) -> list[NASStorage]:
    result = []
    for index, row in rows(table).items():
        if kind == "system_volume":
            name = text_value(row.get(2)) or f"Volume {index}"
            total, free = capacity(row.get(4)), capacity(row.get(5))
            status = text_value(row.get(6))
        else:
            identity = integer(row.get(2))
            name = (
                text_value(row.get(8)) if kind == "volume" else None
            ) or f"{kind.title()} {identity if identity is not None else index}"
            total, free = capacity(row.get(3)), capacity(row.get(4))
            status = (
                {0: "Ready", -1: "Warning", -2: "Not ready", -3: "Error"}.get(
                    integer(row.get(5))
                )
                if kind == "pool"
                else text_value(row.get(5))
            )
        # Reject inconsistent readings instead of clamping them into plausible usage.
        valid_usage = total is not None and free is not None and 0 <= free <= total
        used = total - free if valid_usage else None
        result.append(
            NASStorage(
                id=str(index),
                name=name,
                status=status,
                health=health(status),
                raid_level=text_value(row.get(7)) if kind == "raid" else None,
                capacity_bytes=total,
                free_bytes=free if valid_usage else None,
                used_bytes=used,
                used_percent=(
                    round(used / total * 100, 1) if used is not None and total else None
                ),
            )
        )
    return result


async def poll_nas(config: NASConfig) -> NASSnapshot:
    snapshot = NASSnapshot(id=config.id, name=config.name, host=str(config.host))
    engine = SnmpEngine()
    try:
        async with asyncio.timeout(15):
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
                ObjectType(ObjectIdentity(f"{_BASE}.1.3.12.0")),
                ObjectType(ObjectIdentity(f"{_BASE}.1.2.12.0")),
                lookupMib=False,
            )
            if error or status:
                raise RuntimeError("NAS did not respond")
            snapshot.model = text_value(values[1][1]) or text_value(values[2][1])

            async def walk(name: str) -> Table:
                root = ROOTS[name]
                prefix = tuple(int(n) for n in root.split("."))
                table: Table = {}
                async for error, status, _, bindings in bulk_walk_cmd(
                    engine,
                    auth,
                    target,
                    context,
                    0,
                    12,
                    ObjectType(ObjectIdentity(root)),
                    lexicographicMode=False,
                    lookupMib=False,
                    maxRows=2049,
                ):
                    if error or status:
                        snapshot.warnings.append(
                            f"Could not read the {name.replace('_', ' ')} table."
                        )
                        return {}
                    for oid, value in bindings:
                        parts = tuple(oid)
                        if parts[: len(prefix)] == prefix and not isinstance(
                            value, _MISSING
                        ):
                            table[parts[len(prefix) :]] = value
                    if len(table) > 2048:
                        snapshot.warnings.append(
                            f"The {name} table exceeded the size limit."
                        )
                        return {}
                return table

            disks = await walk("disks")
            modern = bool(disks)
            if not disks:
                disks = await walk("disks_ex") or await walk("disks_legacy")
            snapshot.disks = normalize_disks(disks, modern=modern)
            volumes = await walk("volumes")
            if volumes:
                snapshot.volumes = normalize_storage(volumes, "volume")
            else:
                volumes = await walk("volumes_ex") or await walk("volumes_legacy")
                snapshot.volumes = normalize_storage(volumes, "system_volume")
            snapshot.pools = normalize_storage(await walk("pools"), "pool")
            snapshot.raids = normalize_storage(await walk("raids"), "raid")
            if not snapshot.disks:
                snapshot.warnings.append(
                    "No disk health data was reported. Check that this firmware exposes QNAP NAS-MIB (24681)."
                )
            if not snapshot.volumes:
                snapshot.warnings.append("No volume usage data was reported.")
            snapshot.status = "online"
            snapshot.observed_at = datetime.now(UTC).isoformat()
    except Exception:
        # Do not leak credentials/library errors or present partial data as current.
        snapshot = NASSnapshot(
            id=config.id,
            name=config.name,
            host=str(config.host),
            warnings=[
                "Could not read NAS data. Check its address, UDP port and read-only SNMP community."
            ],
        )
    finally:
        engine.close_dispatcher()
    return snapshot


class NASInventory:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.cache: dict[str, tuple[float, NASSnapshot]] = {}

    async def read(self, path: str | None, network_id: str) -> list[NASSnapshot]:
        if not path:
            return []
        raw = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        if len(raw) > 65536:
            raise ValueError("NAS configuration is too large")
        configs = TypeAdapter(list[NASConfig]).validate_python(json.loads(raw))
        if len(configs) > 16 or len({c.id for c in configs}) != len(configs):
            raise ValueError("Use unique IDs and at most sixteen NAS devices")
        selected = [c for c in configs if c.network_id == network_id]
        if len(selected) > 4:
            raise ValueError("At most four NAS devices can be polled per network")
        async with self.lock:
            keys = {
                c.id: c.model_dump_json() + c.community.get_secret_value()
                for c in configs
            }
            self.cache = {k: v for k, v in self.cache.items() if k in keys.values()}

            async def read_one(config: NASConfig) -> NASSnapshot:
                key = keys[config.id]
                cached = self.cache.get(key)
                if cached and time.monotonic() - cached[0] < 30:
                    return cached[1]
                result = await poll_nas(config)
                self.cache[key] = (time.monotonic(), result)
                return result

            return list(await asyncio.gather(*(read_one(c) for c in selected)))


nas_inventory = NASInventory()
