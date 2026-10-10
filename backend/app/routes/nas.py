"""Authenticated, network-scoped access to read-only QNAP health data."""

from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..deps import get_network_id, require_auth
from ..services.nas import NASSnapshot, nas_inventory

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("", response_model=list[NASSnapshot])
async def list_nas(network_id: str = Depends(get_network_id)) -> list[NASSnapshot]:
    try:
        return await nas_inventory.read(settings.nas_file, network_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="NAS configuration could not be read. Check the dashboard server configuration.",
        ) from exc
