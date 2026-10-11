"""QTS lifetime field semantics, disk identity and credential boundaries."""

import base64
from urllib.parse import parse_qs
from xml.etree import ElementTree as ET

import httpx
import pytest

from app.services import qts
from app.services.nas import NASConfig, NASDisk, NASInventory, NASSnapshot


def config(**updates):
    return NASConfig(
        id="qnap",
        network_id="home",
        name="NAS",
        host="192.168.4.2",
        community="private-community",
        qts={"username": "monitor", "password": "private-password", **updates},
    )


@pytest.mark.parametrize(
    "hours,life,ssd,support,days,remaining",
    [
        ("24001", "98", "1", "1", 1000, 98),
        ("0", "0", "1", "1", 0, 0),
        ("-1", "-1", "1", "1", None, None),
        ("N/A", "101", "1", "1", None, None),
        ("23", "100", "0", "1", 0, None),
        ("24", "100", "1", "0", 1, None),
    ],
)
def test_summary_uses_reported_hours_and_supported_ssd_percentage(
    hours, life, ssd, support, days, remaining
):
    disk = NASDisk(id="9", name="Disk")
    qts.apply_summary(
        disk,
        ET.fromstring(f"""<Summary>
        <power_on_hours>{hours}</power_on_hours><hd_ifssd>{ssd}</hd_ifssd>
        <SSD_life_support>{support}</SSD_life_support><SSD_life_percentage>{life}</SSD_life_percentage>
    </Summary>"""),
    )
    assert disk.power_on_days == days
    assert disk.ssd_life_remaining_percent == remaining


async def test_qts_matches_physical_disks_not_table_indexes_and_keeps_credentials_private(
    monkeypatch,
):
    calls = []

    def handler(request):
        fields = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        calls.append((request, fields))
        assert request.method == "POST"
        assert not request.url.query
        if request.url.path.endswith("authLogin.cgi"):
            assert fields["pwd"] == base64.b64encode(b"private-password").decode()
            return httpx.Response(
                200,
                text="<QDocRoot><authPassed>1</authPassed><authSid>private-sid</authSid></QDocRoot>",
            )
        assert fields["sid"] == "private-sid"
        if fields.get("func") == "all_hd_data":
            xml = "<Disk_Info><entry><HDNo>1:a</HDNo><Model>SSD</Model></entry></Disk_Info>"
        elif fields.get("func") == "summary":
            assert fields["disk"] == "1:a"
            xml = """<Summary><HDNo>1:a</HDNo><Exists>1</Exists><power_on_hours>240</power_on_hours>
                <hd_ifssd>1</hd_ifssd><SSD_life_support>1</SSD_life_support>
                <SSD_life_percentage>0</SSD_life_percentage></Summary>"""
        else:
            assert request.url.path.endswith("authLogout.cgi")
            xml = ""
        return httpx.Response(
            200, text=f"<QDocRoot><authPassed>1</authPassed>{xml}</QDocRoot>"
        )

    original_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["verify"] is True
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(qts.httpx, "AsyncClient", client)
    snapshot = NASSnapshot(
        id="qnap",
        name="NAS",
        host="192.168.4.2",
        status="online",
        disks=[
            NASDisk(id="9", name="SSD", enclosure_id=1, bay=10),
            NASDisk(id="10", name="Other disk", enclosure_id=0, bay=10),
        ],
    )
    await qts.enrich_disks(config(), snapshot)
    assert len(calls) == 4
    assert not snapshot.warnings
    assert snapshot.disks[0].power_on_days == 10
    assert snapshot.disks[0].ssd_life_remaining_percent == 0
    assert snapshot.disks[1].power_on_days is None
    assert "private" not in snapshot.model_dump_json()


@pytest.mark.parametrize(
    "payload",
    [
        "<QDocRoot><authPassed>0</authPassed><message>private-password</message></QDocRoot>",
        "<QDocRoot>broken",
        "<!DOCTYPE foo><QDocRoot />",
        "<QDocRoot><authPassed>1</authPassed></QDocRoot>",
    ],
)
async def test_qts_failure_preserves_snmp_and_sanitizes_warning(monkeypatch, payload):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        qts.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=payload)),
        ),
    )
    snapshot = NASSnapshot(
        id="qnap",
        name="NAS",
        host="192.168.4.2",
        status="online",
        disks=[
            NASDisk(
                id="1",
                name="Disk",
                enclosure_id=0,
                bay=1,
                health="good",
                temperature_c=30,
            )
        ],
    )
    await qts.enrich_disks(config(), snapshot)
    assert snapshot.status == "online" and snapshot.disks[0].temperature_c == 30
    assert (
        snapshot.disks[0].health == "good" and snapshot.disks[0].power_on_days is None
    )
    assert snapshot.warnings and "private-password" not in snapshot.model_dump_json()


def test_changing_qts_password_invalidates_cache_and_legacy_identity_is_not_guessed():
    assert NASInventory._key(config()) != NASInventory._key(config(password="changed"))
    assert qts.disk_address(NASDisk(id="9", name="HDD1")) == (0, 1)
    assert qts.disk_address(NASDisk(id="9", name="Unknown")) is None
