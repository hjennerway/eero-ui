"""Authenticated, network-scoped access to configured LAN switches."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..deps import get_network_id, require_auth, validate_request_path_ids
from ..services.switch_settings import (
    SwitchConfigConflict,
    SwitchConfigError,
    SwitchSettings,
    SwitchSettingsUpdate,
    get_switch_settings,
    save_switch_settings,
)
from ..services.switches import SwitchSnapshot, switch_inventory

router = APIRouter(
    dependencies=[Depends(require_auth), Depends(validate_request_path_ids)]
)
_READ_ERROR = (
    "Switch configuration could not be read. Check the dashboard server configuration."
)


def _settings_path() -> Path:
    return Path(settings.cookie_file).with_name("topology-switch-settings.json")


@router.get("", response_model=list[SwitchSnapshot])
async def list_switches(
    network_id: str = Depends(get_network_id),
) -> list[SwitchSnapshot]:
    try:
        return await switch_inventory.read(
            settings.switches_file, network_id, settings_path=_settings_path()
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=_READ_ERROR) from exc


@router.get("/settings", response_model=SwitchSettings)
def get_settings(network_id: str = Depends(get_network_id)) -> SwitchSettings:
    try:
        return get_switch_settings(
            settings.switches_file,
            _settings_path(),
            network_id,
            settings.session_secret,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=_READ_ERROR) from exc


@router.put("/settings", response_model=SwitchSettings)
def update_settings(
    update: SwitchSettingsUpdate,
    network_id: str = Depends(get_network_id),
) -> SwitchSettings:
    try:
        return save_switch_settings(
            settings.switches_file,
            _settings_path(),
            network_id,
            settings.session_secret,
            update,
        )
    except SwitchConfigConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Switch settings changed. Reopen the editor to load the latest settings.",
        ) from exc
    except SwitchConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="Switch settings could not be saved."
        ) from exc
