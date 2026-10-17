"""Authenticated NAS health reads and local QTS connection settings."""

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from ...config import settings
from ...deps import get_network_id, require_auth
from .nas import NASSnapshot, nas_inventory, poll_nas
from .nas_settings import (
    NASNotFoundError,
    QTSConnectionStatus,
    QTSConnectionUpdate,
    QTSInputError,
    QTSTestResult,
    get_connection,
    prepare_connection,
    save_connection,
)

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("", response_model=list[NASSnapshot])
async def list_nas(
    network_id: str = Depends(get_network_id), refresh: bool = False
) -> list[NASSnapshot]:
    try:
        return await nas_inventory.read(settings.nas_file, network_id, refresh=refresh)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="NAS configuration could not be read. Check the dashboard server configuration.",
        ) from exc


async def _connection_action(action: Callable, *args):
    try:
        return await asyncio.to_thread(action, settings.nas_file, *args)
    except NASNotFoundError as exc:
        raise HTTPException(404, "NAS not found on this network.") from exc
    except QTSInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            503,
            "Could not access NAS settings. The private NAS configuration must be readable "
            "and writable by the dashboard server to save a QTS connection.",
        ) from exc


@router.get("/{nas_id}/qts", response_model=QTSConnectionStatus)
async def get_qts(nas_id: str, network_id: str = Depends(get_network_id)):
    return await _connection_action(get_connection, network_id, nas_id)


@router.put("/{nas_id}/qts", response_model=QTSConnectionStatus)
async def update_qts(
    nas_id: str, update: QTSConnectionUpdate, network_id: str = Depends(get_network_id)
):
    return await _connection_action(save_connection, network_id, nas_id, update)


@router.post("/{nas_id}/qts/test", response_model=QTSTestResult)
async def test_qts(
    nas_id: str, update: QTSConnectionUpdate, network_id: str = Depends(get_network_id)
):
    config = await _connection_action(prepare_connection, network_id, nas_id, update)
    snapshot = await poll_nas(config)
    count = sum(d.power_on_hours is not None for d in snapshot.disks)
    return QTSTestResult(
        success=count > 0,
        message=(
            f"Connected. Power-on time is available for {count} drive{'s' if count != 1 else ''}."
            if count
            else " ".join(snapshot.warnings)
            or "QTS did not report power-on time for any drives. Check disk-health permissions."
        ),
    )
