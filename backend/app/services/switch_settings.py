"""Read and update the dashboard's network-scoped switch configuration."""

import hmac
import json
import os
import tempfile
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from threading import Lock
from typing import Annotated

from pydantic import BaseModel, Field, SecretStr, TypeAdapter, field_validator

_MAX_FILE_SIZE = 65536
_CONFIG_LOCK = Lock()
Community = Annotated[SecretStr, Field(min_length=1, max_length=128)]


class KnownSwitchDevice(BaseModel):
    mac: str = Field(pattern=r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
    ip: IPv4Address
    name: str = Field(min_length=1, max_length=100)

    @field_validator("mac")
    @classmethod
    def unicast_mac(cls, value: str) -> str:
        if value == "00:00:00:00:00:00" or int(value[:2], 16) & 1:
            raise ValueError("Use a unicast device MAC address")
        return value.lower()


class UnmanagedSwitchConfig(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    port: int = Field(ge=1)  # managed switch ifIndex
    eero_ids: list[str] = Field(default_factory=list, max_length=8)
    devices: list[KnownSwitchDevice] = Field(default_factory=list, max_length=32)


class SwitchFields(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    host: IPv4Address
    port: int = Field(default=161, ge=1, le=65535)
    unmanaged_switches: list[UnmanagedSwitchConfig] = Field(
        default_factory=list, max_length=8
    )

    @field_validator("unmanaged_switches")
    @classmethod
    def unique_unmanaged_ports(cls, values: list[UnmanagedSwitchConfig]):
        if len({v.id for v in values}) != len(values) or len(
            {v.port for v in values}
        ) != len(values):
            raise ValueError("Unmanaged switch IDs and upstream ports must be unique")
        return values

    @field_validator("host")
    @classmethod
    def private_lan_only(cls, host: IPv4Address) -> IPv4Address:
        if not any(
            host in IPv4Network(cidr)
            for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ):
            raise ValueError("Use a private LAN IPv4 address")
        return host


class SwitchConfig(SwitchFields):
    network_id: str = Field(min_length=1, max_length=128)
    community: Community


class SwitchUpdate(SwitchFields):
    community: Community | None = None


class SwitchSettings(BaseModel):
    network_id: str
    revision: str
    switches: list[SwitchFields]


class SwitchSettingsUpdate(BaseModel):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    switches: list[SwitchUpdate] = Field(max_length=4)


class SwitchConfigError(ValueError):
    """A configuration rule with a message safe to show in the editor."""


class SwitchConfigConflict(Exception):
    """The editor's copy has been superseded."""


NetworkConfigs = dict[str, list[SwitchConfig]]
_LEGACY_ADAPTER = TypeAdapter(list[SwitchConfig])
_SAVED_ADAPTER = TypeAdapter(NetworkConfigs)


def _read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        raw = handle.read(_MAX_FILE_SIZE + 1)
    if len(raw) > _MAX_FILE_SIZE:
        raise ValueError("Switch configuration is too large")
    return json.loads(raw)


def _read_configs(
    source: str | None, target: Path | None
) -> tuple[NetworkConfigs, NetworkConfigs]:
    legacy: NetworkConfigs = {}
    if source:
        for config in _LEGACY_ADAPTER.validate_python(_read_json(Path(source))):
            legacy.setdefault(config.network_id, []).append(config)
    if target is None:
        return legacy, {}
    try:
        saved = _SAVED_ADAPTER.validate_python(_read_json(target))
    except FileNotFoundError:
        return legacy, {}
    if any(
        config.network_id != network_id
        for network_id, configs in saved.items()
        for config in configs
    ):
        raise ValueError("Stored switch network IDs do not match")
    return legacy, saved


def _validate_configs(configs: list[SwitchConfig]) -> None:
    all_ids = [
        id
        for c in configs
        for id in [c.id, *(f"{c.id}--{u.id}" for u in c.unmanaged_switches)]
    ]
    if len(configs) > 16 or len(set(all_ids)) != len(all_ids):
        raise SwitchConfigError("Switch IDs must be unique, with at most 16 switches")
    assignments = [
        (c.network_id, kind, identity)
        for c in configs
        for u in c.unmanaged_switches
        for kind, identity in [
            *(("eero", e) for e in u.eero_ids),
            *(("mac", d.mac) for d in u.devices),
            *(("ip", str(d.ip)) for d in u.devices),
        ]
    ]
    if len(assignments) != len(set(assignments)):
        raise SwitchConfigError(
            "Known wiring must assign each device to only one switch"
        )


def _effective_configs(
    legacy: NetworkConfigs, saved: NetworkConfigs
) -> list[SwitchConfig]:
    configs = [config for group in (legacy | saved).values() for config in group]
    _validate_configs(configs)
    return configs


def read_switch_configs(source: str | None, target: Path | None) -> list[SwitchConfig]:
    return _effective_configs(*_read_configs(source, target))


def _serialized(configs: list[SwitchConfig]) -> list[dict]:
    return [
        config.model_dump(mode="json", exclude={"community"})
        | {"community": config.community.get_secret_value()}
        for config in configs
    ]


def _revision(configs: list[SwitchConfig], network_id: str, secret: str) -> str:
    data = json.dumps([network_id, _serialized(configs)], sort_keys=True)
    return hmac.digest(secret.encode(), data.encode(), "sha256").hex()


def _settings(
    configs: list[SwitchConfig], network_id: str, secret: str
) -> SwitchSettings:
    return SwitchSettings(
        network_id=network_id,
        revision=_revision(configs, network_id, secret),
        switches=configs,
    )


def get_switch_settings(
    source: str | None, target: Path, network_id: str, secret: str
) -> SwitchSettings:
    legacy, saved = _read_configs(source, target)
    return _settings((legacy | saved).get(network_id, []), network_id, secret)


def _apply_updates(
    updates: list[SwitchUpdate], current: list[SwitchConfig], network_id: str
) -> list[SwitchConfig]:
    by_id = {config.id: config for config in current}
    configs = []
    for update in updates:
        previous = by_id.get(update.id)
        community = update.community
        if community is None:
            if previous is None or (previous.host, previous.port) != (
                update.host,
                update.port,
            ):
                raise SwitchConfigError(
                    "Enter the read-only SNMP community for each new switch or changed address or port."
                )
            community = previous.community
        configs.append(
            SwitchConfig(
                **update.model_dump(exclude={"community"}),
                network_id=network_id,
                community=community,
            )
        )
    return configs


def _write_settings(target: Path, saved: NetworkConfigs) -> None:
    raw = json.dumps(
        {network_id: _serialized(configs) for network_id, configs in saved.items()},
        ensure_ascii=False,
    )
    if len(raw) > _MAX_FILE_SIZE:
        raise SwitchConfigError("Switch configuration is too large.")
    target.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the credentials file with owner-only permissions on POSIX.
    fd, temporary = tempfile.mkstemp(prefix=".switches-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_switch_settings(
    source: str | None,
    target: Path,
    network_id: str,
    secret: str,
    update: SwitchSettingsUpdate,
) -> SwitchSettings:
    with _CONFIG_LOCK:
        legacy, saved = _read_configs(source, target)
        current = (legacy | saved).get(network_id, [])
        if not hmac.compare_digest(
            update.revision, _revision(current, network_id, secret)
        ):
            raise SwitchConfigConflict
        configs = _apply_updates(update.switches, current, network_id)
        saved[network_id] = configs
        _effective_configs(legacy, saved)
        _write_settings(target, saved)
        return _settings(configs, network_id, secret)
