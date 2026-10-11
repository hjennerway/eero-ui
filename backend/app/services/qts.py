"""Optional QTS disk lifetime readings, using the same summary as the QTS UI.

Protocol verified against QTS storageManagerV2Store.js and HddSmartWizard.js:
disk/qsmart.cgi func=summary takes dev_id (inventory HDNo without the colon).
Summary reports power_on_hours, SSD_life_support and SSD_life_percentage.
Inventory IDs are hexadecimal enclosure:bay, not SNMP indexes; built-in disk
summaries can instead return the bay as a decimal HDNo.
"""

import asyncio
import base64
import re
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import httpx
from pydantic import BaseModel, Field, SecretStr

if TYPE_CHECKING:
    from .nas import NASConfig, NASDisk, NASSnapshot


class QTSConfig(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=4096)
    port: int = Field(default=443, ge=1, le=65535)
    https: bool = True
    verify_tls: bool = True


def _number(value: str | None, maximum: int | None = None) -> int | None:
    if value is None or not re.fullmatch(r"\d+", value.strip()):
        return None
    result = int(value.strip())
    return result if maximum is None or result <= maximum else None


def disk_address(disk: "NASDisk") -> tuple[int, int] | None:
    if disk.enclosure_id is not None and disk.bay is not None:
        return disk.enclosure_id, disk.bay
    # Legacy built-in HDD labels identify bays; table indexes do not.
    match = re.fullmatch(r"HDD\s*(\d+)", disk.name, re.I)
    return (0, int(match[1])) if match else None


def _qts_address(hd_no: str) -> tuple[int, int] | None:
    hd_no = hd_no.strip()
    if re.fullmatch(r"[0-9a-fA-F]+:[0-9a-fA-F]+", hd_no):
        enclosure, bay = hd_no.split(":")
        return int(enclosure, 16), int(bay, 16)
    if re.fullmatch(r"\d+", hd_no):
        return 0, int(hd_no)
    return None


def apply_summary(disk: "NASDisk", summary: ET.Element) -> None:
    if summary.findtext("Exists", "").strip().casefold() in {"0", "no"}:
        return
    hours = _number(summary.findtext("power_on_hours"))
    disk.power_on_hours = hours
    disk.power_on_days = hours // 24 if hours is not None else None
    if (
        summary.findtext("hd_ifssd") == "1"
        and summary.findtext("SSD_life_support") == "1"
    ):
        disk.ssd_life_remaining_percent = _number(
            summary.findtext("SSD_life_percentage"), 100
        )


async def _request(
    client: httpx.AsyncClient, endpoint: str, data: dict[str, str]
) -> ET.Element:
    # SID and login credentials are POST fields, never query strings/logged URLs.
    async with client.stream("POST", endpoint, data=data) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise ValueError("QTS response is too large")
    xml = body.decode("utf-8")
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("Unsupported QTS XML")
    root = ET.fromstring(xml)
    if root.findtext("authPassed") == "0":
        raise ValueError("QTS authentication failed")
    return root


async def enrich_disks(config: "NASConfig", snapshot: "NASSnapshot") -> None:
    qts = config.qts
    if not snapshot.disks:
        return
    if qts is None:
        snapshot.warnings.append(
            "Power-on time and SSD life need a QTS connection. "
            "Use QTS connection to add this NAS's login details."
        )
        return
    try:
        async with asyncio.timeout(25), httpx.AsyncClient(
            base_url=f"{'https' if qts.https else 'http'}://{config.host}:{qts.port}/cgi-bin/",
            verify=qts.verify_tls,
            timeout=5,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            login = await _request(
                client,
                "authLogin.cgi",
                {
                    "user": qts.username,
                    "pwd": base64.b64encode(
                        qts.password.get_secret_value().encode()
                    ).decode(),
                },
            )
            sid = login.findtext("authSid")
            if not sid:
                raise ValueError("QTS login did not return a session")
            try:
                inventory = await _request(
                    client, "disk/qsmart.cgi", {"func": "all_hd_data", "sid": sid}
                )
                entries: dict[tuple[int, int], str] = {}
                for entry in inventory.findall(".//Disk_Info/entry"):
                    hd_no = entry.findtext("HDNo", "").strip()
                    if not entry.findtext("Model"):
                        continue
                    address = _qts_address(hd_no)
                    if address is not None:
                        entries[address] = hd_no
                matched = 0
                for disk in snapshot.disks:
                    hd_no = entries.get(disk_address(disk))
                    if hd_no is None:
                        continue
                    result = await _request(
                        client,
                        "disk/qsmart.cgi",
                        {
                            "func": "summary",
                            "kick": "1",
                            "dev_id": hd_no.replace(":", ""),
                            "sid": sid,
                        },
                    )
                    summary = result.find(".//Summary")
                    if summary is not None and _qts_address(
                        summary.findtext("HDNo", "")
                    ) == disk_address(disk):
                        apply_summary(disk, summary)
                        matched += 1
                if not matched:
                    snapshot.warnings.append(
                        "QTS did not report matching disk lifetime summaries on this firmware."
                    )
            finally:
                try:
                    await _request(
                        client,
                        "authLogout.cgi",
                        {"sid": sid, "logout": "1", "user": qts.username},
                    )
                except (httpx.HTTPError, ValueError, ET.ParseError):
                    pass
    except (TimeoutError, httpx.HTTPError, ValueError, ET.ParseError):
        # Do not report partial/old lifetime readings as a successful refresh.
        for disk in snapshot.disks:
            disk.power_on_hours = None
            disk.power_on_days = None
            disk.ssd_life_remaining_percent = None
        snapshot.warnings.append(
            "Could not read QTS disk age and SSD life. Check QTS credentials, permissions, port and TLS settings."
        )
