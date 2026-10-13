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
    assert disk.power_on_hours == (int(hours) if days is not None else None)
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
            assert fields["dev_id"] == "1a"
            assert "disk" not in fields
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
    assert snapshot.disks[0].power_on_hours == 240
    assert snapshot.disks[0].power_on_days == 10
    assert snapshot.disks[0].ssd_life_remaining_percent == 0
    assert snapshot.disks[1].power_on_days is None
    assert "private" not in snapshot.model_dump_json()


@pytest.mark.parametrize(
    "inventory_id,summary_id,enclosure,bay,expected",
    [
        ("0:5", "5", 0, 5, 51448),
        ("0:0A", "10", 0, 10, 51448),
        ("5", "5", 0, 5, 51448),
        ("01:0A", "1:a", 1, 10, 51448),
        ("0:5", "6", 0, 5, None),
        ("1:5", "5", 1, 5, None),
        ("0:5", "", 0, 5, None),
    ],
)
async def test_qts_summary_request_and_identity_variants(
    monkeypatch, inventory_id, summary_id, enclosure, bay, expected
):
    def handler(request):
        fields = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.url.path.endswith("authLogin.cgi"):
            xml = "<authSid>private-sid</authSid>"
        elif fields.get("func") == "all_hd_data":
            xml = f"<Disk_Info><entry><HDNo>{inventory_id}</HDNo><Model>Disk</Model></entry></Disk_Info>"
        elif fields.get("func") == "summary":
            assert fields["dev_id"] == inventory_id.replace(":", "")
            assert "disk" not in fields
            # QTS 5.2.9 returns a decimal bay and Exists=Yes for built-in disks.
            xml = f"""<Summary><HDNo>{summary_id}</HDNo><Exists>Yes</Exists>
                <power_on_hours>51448</power_on_hours><hd_ifssd>0</hd_ifssd>
                <SSD_life_support>0</SSD_life_support><SSD_life_percentage>-1</SSD_life_percentage>
                </Summary>"""
        else:
            xml = ""
        return httpx.Response(200, text=f"<QDocRoot>{xml}</QDocRoot>")

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        qts.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            **kwargs, transport=httpx.MockTransport(handler)
        ),
    )
    disk = NASDisk(id="99", name="Disk", enclosure_id=enclosure, bay=bay)
    snapshot = NASSnapshot(id="qnap", name="NAS", host="192.168.4.2", disks=[disk])
    await qts.enrich_disks(config(), snapshot)
    assert disk.power_on_hours == expected
    assert disk.power_on_days == (2143 if expected is not None else None)
    assert disk.ssd_life_remaining_percent is None
    assert bool(snapshot.warnings) == (expected is None)


async def test_missing_qts_connection_explains_missing_lifetime_readings(monkeypatch):
    def unexpected_client(**kwargs):
        pytest.fail("A NAS without QTS credentials must not attempt an HTTP login")

    monkeypatch.setattr(qts.httpx, "AsyncClient", unexpected_client)
    snapshot = NASSnapshot(
        id="qnap", name="NAS", host="192.168.4.2", disks=[NASDisk(id="1", name="Disk")]
    )
    await qts.enrich_disks(config().model_copy(update={"qts": None}), snapshot)
    assert snapshot.disks[0].power_on_hours is None
    assert "QTS connection" in snapshot.warnings[0]


@pytest.mark.parametrize("exists", ["0", "No"])
def test_absent_disk_does_not_get_a_lifetime_reading(exists):
    disk = NASDisk(id="1", name="Disk")
    qts.apply_summary(
        disk,
        ET.fromstring(
            f"<Summary><Exists>{exists}</Exists><power_on_hours>0</power_on_hours></Summary>"
        ),
    )
    assert disk.power_on_hours is None
    assert disk.power_on_days is None


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
                power_on_hours=51448,
                power_on_days=2143,
            )
        ],
    )
    await qts.enrich_disks(config(), snapshot)
    assert snapshot.status == "online" and snapshot.disks[0].temperature_c == 30
    assert (
        snapshot.disks[0].health == "good" and snapshot.disks[0].power_on_days is None
    )
    assert snapshot.disks[0].power_on_hours is None
    assert snapshot.warnings and "private-password" not in snapshot.model_dump_json()


def test_changing_qts_password_invalidates_cache_and_legacy_identity_is_not_guessed():
    assert NASInventory._key(config()) != NASInventory._key(config(password="changed"))
    assert qts.disk_address(NASDisk(id="9", name="HDD1")) == (0, 1)
    assert qts.disk_address(NASDisk(id="9", name="Unknown")) is None
