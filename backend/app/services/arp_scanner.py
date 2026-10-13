"""Hourly ARP discovery worker; runs separately with host networking and NET_RAW."""

import json
import logging
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Network
from pathlib import Path

from .discovery import DiscoveredDevice, DiscoverySnapshot, read_snapshot

LOGGER = logging.getLogger(__name__)


def parse_scan(
    output: str, subnet: IPv4Network, seen: datetime
) -> list[DiscoveredDevice]:
    devices: dict[tuple[str, str], DiscoveredDevice] = {}
    for line in output.splitlines():
        columns = line.split()
        if len(columns) < 2:
            continue
        try:
            device = DiscoveredDevice(
                ip=columns[0], mac=columns[1], last_seen=seen, connected=True
            )
        except ValueError:
            continue
        if device.ip in subnet and device.ip not in (
            subnet.network_address,
            subnet.broadcast_address,
        ):
            devices[(str(device.ip), device.mac)] = device
    return sorted(devices.values(), key=lambda d: (int(d.ip), d.mac))


def scan_lan(subnet: IPv4Network) -> str:
    # Use the host route, never arp-scan's default Docker bridge/localnet range.
    target = str(subnet.network_address + 1)
    route = json.loads(
        subprocess.run(
            ["ip", "-j", "route", "get", target],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    )[0]
    if route.get("gateway") or not route.get("dev"):
        raise ValueError("ARP discovery needs a directly attached LAN")
    return subprocess.run(
        [
            "arp-scan",
            f"--interface={route['dev']}",
            "--plain",
            "--quiet",
            "--numeric",
            "--ignoredups",
            "--retry=2",
            "--timeout=500",
            "--bandwidth=64000",
            str(subnet),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    ).stdout


def scan_once(
    previous: DiscoverySnapshot | None,
    network_id: str,
    subnet: IPv4Network,
    interval: int,
) -> DiscoverySnapshot:
    if previous and (previous.network_id != network_id or previous.subnet != subnet):
        previous = None
    status = "ok"
    try:
        output = scan_lan(subnet)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        LOGGER.warning("LAN scan failed; previous observations are unconfirmed")
        status, output = "error", ""
    now = datetime.now(UTC)
    devices = parse_scan(output, subnet, now) if status == "ok" else []
    current = {(str(d.ip), d.mac) for d in devices}
    # Keep missing devices for seven days, but never call cached neighbours online.
    if previous and previous.network_id == network_id and previous.subnet == subnet:
        devices.extend(
            d.model_copy(update={"connected": False})
            for d in previous.devices
            if (str(d.ip), d.mac) not in current
            and now - d.last_seen < timedelta(days=7)
        )
    devices.sort(
        key=lambda d: (not d.connected, -d.last_seen.timestamp(), int(d.ip), d.mac)
    )
    return DiscoverySnapshot(
        network_id=network_id,
        subnet=subnet,
        interval_seconds=interval,
        status=status,
        last_scan=now,
        last_success=now
        if status == "ok"
        else previous.last_success
        if previous
        else None,
        next_scan=now + timedelta(seconds=interval),
        devices=devices[:1024],
    )


def write_snapshot(path: Path, snapshot: DiscoverySnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(snapshot.model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    network_id = os.environ["EERO_DISCOVERY_NETWORK_ID"]
    subnet = DiscoverySnapshot.private_lan(
        IPv4Network(os.environ["EERO_DISCOVERY_SUBNET"])
    )
    interval = min(
        86400, max(3600, int(os.environ.get("EERO_DISCOVERY_INTERVAL", "3600")))
    )
    path = Path(os.environ.get("EERO_DISCOVERY_FILE", "/discovery/arp.json"))
    previous = None
    try:
        previous = read_snapshot(path)
    except (OSError, ValueError):
        pass
    while True:
        if previous and previous.network_id == network_id and previous.subnet == subnet:
            remaining = (previous.next_scan - datetime.now(UTC)).total_seconds()
            if 0 < remaining <= interval:
                time.sleep(remaining)
        previous = scan_once(previous, network_id, subnet, interval)
        write_snapshot(path, previous)
        LOGGER.info(
            "LAN scan %s: %d responding devices; next scan %s",
            previous.status,
            sum(d.connected for d in previous.devices),
            previous.next_scan.isoformat(),
        )


if __name__ == "__main__":
    main()
