"""Authenticated, network-scoped access to configured LAN switches."""

from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..deps import get_network_id, require_auth, validate_request_path_ids
from ..services.switches import SwitchSnapshot, switch_inventory

router = APIRouter(
    dependencies=[Depends(require_auth), Depends(validate_request_path_ids)]
)


@router.get("", response_model=list[SwitchSnapshot])
async def list_switches(
    network_id: str = Depends(get_network_id),
) -> list[SwitchSnapshot]:
    try:
        return await switch_inventory.read(settings.switches_file, network_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Switch configuration could not be read. Check the dashboard server configuration.",
        ) from exc
