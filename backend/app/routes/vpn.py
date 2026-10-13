"""Authenticated VPN snapshot, connection settings and metric history."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..deps import require_auth
from ..services.vpn import (
    VPNConnection,
    VPNConnectionUpdate,
    VPNControlError,
    VPNControlRequest,
    VPNReadError,
    VPNService,
    VPNSnapshot,
)

router = APIRouter(dependencies=[Depends(require_auth)])


def service(request: Request) -> VPNService:
    monitor = getattr(request.app.state, "vpn", None)
    if monitor is None:
        raise HTTPException(503, "VPN monitoring is starting. Try again shortly.")
    return monitor


@router.get("", response_model=VPNSnapshot)
async def snapshot(monitor: VPNService = Depends(service)):
    reading = monitor.snapshot.model_copy(deep=True)
    if reading.status != "disabled" and (
        not reading.collected_at
        or (datetime.now(UTC) - reading.collected_at).total_seconds() > 90
    ):
        reading.stale = True
        reading.status = "unknown"
        reading.connected_clients = reading.download_mbps = reading.upload_mbps = None
        reading.control_available = False
    return reading


@router.get("/connection", response_model=VPNConnection)
async def connection(monitor: VPNService = Depends(service)):
    try:
        return await monitor.connection()
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Could not read VPN connection settings.") from exc


@router.put("/connection", response_model=VPNConnection)
async def update_connection(
    update: VPNConnectionUpdate, monitor: VPNService = Depends(service)
):
    try:
        result = await monitor.update(update)
    except VPNReadError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            503, "Could not validate or save VPN connection settings."
        ) from exc
    await monitor.collect_once()
    return result


@router.get("/history")
async def history(
    range: Literal["1h", "6h", "24h"] = Query("1h"),
    monitor: VPNService = Depends(service),
):
    try:
        config = await monitor.connection()
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Could not read VPN connection settings.") from exc
    now = datetime.now(UTC)
    hours = {"1h": 1, "6h": 6, "24h": 24}[range]
    selector = (
        "{host="
        + json.dumps(config.host)
        + ",interface="
        + json.dumps(config.interface)
        + "}"
    )
    names = ("download_mbps", "upload_mbps", "connected_clients", "connection_status")
    try:
        results = await asyncio.gather(
            *(
                monitor.victoria.query_range(
                    f"last_over_time(vpn_{name}{selector}[45s])",
                    (now - timedelta(hours=hours)).isoformat(),
                    now.isoformat(),
                    "30s" if hours == 1 else "2m" if hours == 6 else "5m",
                )
                for name in names
            )
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            503, "VPN history is unavailable. Check the metrics store."
        ) from exc
    # Bound VM's lookback so an outage appears as a gap instead of carried-forward traffic.
    return {
        name: [
            {"x": float(stamp) * 1000, "y": float(value)}
            for series in result.get("data", {}).get("result", [])[:1]
            for stamp, value in series.get("values", [])
            if value not in ("NaN", "+Inf", "-Inf")
        ]
        for name, result in zip(names, results)
    }


@router.post("/tunnel", response_model=VPNSnapshot)
async def control_tunnel(
    action: VPNControlRequest, monitor: VPNService = Depends(service)
):
    try:
        return await monitor.control(action.action)
    except VPNControlError as exc:
        raise HTTPException(409, str(exc)) from exc
    except VPNReadError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "Could not read VPN connection settings.") from exc
