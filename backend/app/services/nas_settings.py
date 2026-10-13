"""Network-scoped QTS connection settings in the private NAS configuration."""

import json
import os
import tempfile
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from .nas import NASConfig
from .qts import QTSConfig

_lock = threading.RLock()


class NASNotFoundError(Exception):
    pass


class QTSInputError(Exception):
    pass


class QTSConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    username: str = Field(default="", max_length=256)
    # Omitted/null preserves the stored password. Disconnect removes it.
    password: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    port: int = Field(default=443, ge=1, le=65535)
    https: bool = True
    verify_tls: bool = True


class QTSConnectionStatus(BaseModel):
    enabled: bool = False
    username: str = ""
    password_set: bool = False
    port: int = 443
    https: bool = True
    verify_tls: bool = True


class QTSTestResult(BaseModel):
    success: bool
    message: str


def _read(path: str | None, network_id: str, nas_id: str):
    if not path:
        raise NASNotFoundError
    raw = Path(path).read_text(encoding="utf-8")
    if len(raw) > 65536:
        raise ValueError("NAS configuration is too large")
    document = json.loads(raw)
    configs = TypeAdapter(list[NASConfig]).validate_python(document)
    if len(configs) > 16 or len({c.id for c in configs}) != len(configs):
        raise ValueError("Invalid NAS inventory")
    for index, config in enumerate(configs):
        if config.id == nas_id and config.network_id == network_id:
            return document, index, config
    raise NASNotFoundError


def _status(config: QTSConfig | None) -> QTSConnectionStatus:
    if config is None:
        return QTSConnectionStatus()
    return QTSConnectionStatus(
        **config.model_dump(exclude={"password"}),
        enabled=True,
        password_set=bool(config.password.get_secret_value()),
    )


def _updated(config: NASConfig, update: QTSConnectionUpdate) -> NASConfig:
    qts = None
    if update.enabled:
        password = update.password or (config.qts.password if config.qts else None)
        if not update.username.strip() or password is None:
            raise QTSInputError("Enter a QTS username and password.")
        qts = QTSConfig(
            username=update.username.strip(),
            password=password,
            port=update.port,
            https=update.https,
            verify_tls=update.verify_tls,
        )
    return config.model_copy(update={"qts": qts})


def get_connection(
    path: str | None, network_id: str, nas_id: str
) -> QTSConnectionStatus:
    with _lock:
        _, _, config = _read(path, network_id, nas_id)
        return _status(config.qts)


def prepare_connection(
    path: str | None, network_id: str, nas_id: str, update: QTSConnectionUpdate
) -> NASConfig:
    with _lock:
        _, _, config = _read(path, network_id, nas_id)
        if not update.enabled:
            raise QTSInputError("Enable the QTS connection before testing it.")
        return _updated(config, update)


def save_connection(
    path: str | None, network_id: str, nas_id: str, update: QTSConnectionUpdate
) -> QTSConnectionStatus:
    with _lock:
        document, index, config = _read(path, network_id, nas_id)
        qts = _updated(config, update).qts
        if qts is None:
            document[index].pop("qts", None)
        else:
            document[index]["qts"] = {
                **qts.model_dump(mode="json", exclude={"password"}),
                "password": qts.password.get_secret_value(),
            }
        # Preserve other NAS entries, SNMP secrets and unrecognized metadata.
        payload = json.dumps(document, indent=2) + "\n"
        if len(payload) > 65536:
            raise QTSInputError("NAS configuration is too large to save.")
        target = Path(path)
        fd, temporary = tempfile.mkstemp(prefix=".nas-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return _status(qts)
