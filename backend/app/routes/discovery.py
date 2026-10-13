"""Read-only, authenticated local discovery results. Requests never trigger scans."""

from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..deps import get_network_id, require_auth, validate_request_path_ids
from ..services.discovery import DiscoveryResponse, read_discovery

router = APIRouter(
    dependencies=[Depends(require_auth), Depends(validate_request_path_ids)]
)


@router.get("", response_model=DiscoveryResponse)
async def get_discovery(network_id: str = Depends(get_network_id)) -> DiscoveryResponse:
    try:
        return read_discovery(settings.discovery_file, network_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="LAN discovery data could not be read."
        ) from exc
