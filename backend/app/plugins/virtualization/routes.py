"""Authenticated, network-scoped virtualization inventory routes."""

from eero import EeroClient
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...deps import require_auth, validate_request_path_ids
from ...routes.auth import limiter
from .models import (
    ConnectionSummary,
    ConnectionUpdate,
    VirtualizationSnapshot,
)

router = APIRouter(dependencies=[Depends(validate_request_path_ids)])


class RefreshRequest(BaseModel):
    force: bool = True


@router.get("/{network_id}/virtualization", response_model=VirtualizationSnapshot)
async def get_inventory(
    request: Request,
    network_id: str,
    client: EeroClient = Depends(require_auth),
) -> VirtualizationSnapshot:
    return request.app.state.virtualization.snapshot(network_id)


@router.get("/{network_id}/virtualization/connection", response_model=ConnectionSummary)
async def get_connection(
    request: Request,
    network_id: str,
    client: EeroClient = Depends(require_auth),
) -> ConnectionSummary:
    return request.app.state.virtualization.summary(network_id)


@router.put("/{network_id}/virtualization/connection", response_model=ConnectionSummary)
@limiter.shared_limit("6/minute", scope="virtualization_connections")
async def save_connection(
    request: Request,
    network_id: str,
    connection: ConnectionUpdate,
    client: EeroClient = Depends(require_auth),
) -> ConnectionSummary:
    try:
        return request.app.state.virtualization.save(network_id, connection)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except OSError:
        raise HTTPException(
            status_code=503, detail="Could not save the virtualization connection."
        ) from None


@router.post(
    "/{network_id}/virtualization/refresh", response_model=VirtualizationSnapshot
)
@limiter.shared_limit("20/minute", scope="virtualization_refresh")
async def refresh_inventory(
    request: Request,
    network_id: str,
    refresh: RefreshRequest,
    client: EeroClient = Depends(require_auth),
) -> VirtualizationSnapshot:
    try:
        return await request.app.state.virtualization.refresh(
            network_id, force=refresh.force
        )
    except ValueError:
        raise HTTPException(
            status_code=409, detail="Connect your Proxmox cluster first."
        ) from None
