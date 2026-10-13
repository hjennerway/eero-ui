"""QTS settings persistence, network boundaries, password secrecy and test reads."""

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import nas_settings
from app.services.nas import NASDisk, NASInventory, NASSnapshot

URL = "/api/nas/qnap/qts"
UPDATE = {
    "username": "monitor",
    "password": "private-password",
    "port": 8080,
    "https": False,
}


@pytest.fixture
def nas_path(tmp_path, monkeypatch):
    path = tmp_path / "nas.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "qnap",
                    "name": "NAS",
                    "network_id": "network-123",
                    "host": "192.168.4.2",
                    "community": "private-community",
                    "notes": "Keep this metadata",
                },
                {
                    "id": "other",
                    "name": "Other NAS",
                    "network_id": "network-456",
                    "host": "192.168.4.3",
                    "community": "other-private-community",
                    "qts": {"username": "other", "password": "other-private-password"},
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "nas_file", str(path))
    return path


async def test_save_preserve_replace_remove_and_keep_other_configuration(
    auth_client, nas_path
):
    original = json.loads(nas_path.read_text())
    response = await auth_client.get(URL)
    assert response.status_code == 200
    assert not response.json()["enabled"] and not response.json()["password_set"]
    for payload, password in (
        (UPDATE, "private-password"),
        ({**UPDATE, "password": None, "port": 443, "https": True}, "private-password"),
        (
            {**UPDATE, "password": "replacement-private-password"},
            "replacement-private-password",
        ),
    ):
        response = await auth_client.put(URL, json=payload)
        assert response.status_code == 200
        assert response.json()["password_set"] is True
        assert "private" not in response.text and "password" not in response.json()
        saved = json.loads(nas_path.read_text())
        assert saved[0]["qts"]["password"] == password
        assert saved[0]["community"] == original[0]["community"]
        assert saved[0]["notes"] == original[0]["notes"]
        assert saved[1] == original[1]
        assert "private" not in (await auth_client.get(URL)).text
    if os.name != "nt":
        assert nas_path.stat().st_mode & 0o777 == 0o600
    response = await auth_client.put(URL, json={"enabled": False})
    assert response.status_code == 200 and not response.json()["password_set"]
    assert json.loads(nas_path.read_text()) == original


async def test_wrong_network_cannot_read_or_change_connection(auth_client, nas_path):
    original = nas_path.read_bytes()
    for method in ("GET", "PUT", "POST"):
        url = URL + ("/test" if method == "POST" else "") + "?network_id=network-456"
        response = await auth_client.request(
            method, url, **({"json": UPDATE} if method != "GET" else {})
        )
        assert response.status_code == 404
        assert "private" not in response.text
    assert nas_path.read_bytes() == original


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("PUT", ""), ("POST", "/test")])
async def test_qts_settings_require_authentication(
    async_client, nas_path, method, suffix
):
    response = await async_client.request(
        method, URL + suffix, **({"json": UPDATE} if method != "GET" else {})
    )
    assert response.status_code == 401


async def test_qts_writes_require_csrf_header(auth_client, nas_path):
    for method, suffix in (("PUT", ""), ("POST", "/test")):
        response = await auth_client.request(
            method, URL + suffix, json=UPDATE, headers={"X-Requested-With": ""}
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "monitor"},
        {"username": "", "password": "private-password"},
        {**UPDATE, "port": 0},
        {**UPDATE, "host": "private-password"},
        {**UPDATE, "password": {"private-password": True}},
        {**UPDATE, "password": "private-password" * 400},
    ],
)
async def test_invalid_settings_do_not_echo_secrets_or_change_file(
    auth_client, nas_path, payload
):
    original = nas_path.read_bytes()
    response = await auth_client.put(URL, json=payload)
    assert response.status_code == 422
    assert "private-password" not in response.text
    assert nas_path.read_bytes() == original


async def test_unwritable_configuration_keeps_original_and_cleans_temporary_file(
    auth_client, nas_path, monkeypatch
):
    original = nas_path.read_bytes()

    def fail(*args):
        raise PermissionError("private-password should not be displayed")

    monkeypatch.setattr(nas_settings.os, "replace", fail)
    response = await auth_client.put(URL, json=UPDATE)
    assert response.status_code == 503 and "private-password" not in response.text
    assert nas_path.read_bytes() == original
    assert not list(nas_path.parent.glob(".nas-*"))


async def test_test_connection_uses_entered_credentials_without_saving(
    auth_client, nas_path, monkeypatch
):
    original = nas_path.read_bytes()
    poll = AsyncMock(
        return_value=NASSnapshot(
            id="qnap",
            name="NAS",
            host="192.168.4.2",
            status="online",
            disks=[NASDisk(id="1", name="Disk", power_on_hours=51448)],
        )
    )
    monkeypatch.setattr("app.routes.nas.poll_nas", poll)
    response = await auth_client.post(URL + "/test", json=UPDATE)
    assert response.status_code == 200 and response.json()["success"]
    assert "1 drive" in response.json()["message"]
    assert poll.call_args.args[0].qts.password.get_secret_value() == "private-password"
    assert nas_path.read_bytes() == original
    assert "private" not in response.text
    poll.return_value = NASSnapshot(
        id="qnap",
        name="NAS",
        host="192.168.4.2",
        warnings=["Could not read QTS disk age and SSD life."],
    )
    response = await auth_client.post(URL + "/test", json=UPDATE)
    assert response.status_code == 200 and not response.json()["success"]


async def test_saved_password_is_used_for_test_and_cache_key_changes(
    auth_client, nas_path, monkeypatch
):
    inventory = NASInventory()
    before = (await inventory._configs(str(nas_path)))[0]
    await auth_client.put(URL, json=UPDATE)
    after = (await inventory._configs(str(nas_path)))[0]
    assert inventory._key(before) != inventory._key(after)
    poll = AsyncMock(
        return_value=NASSnapshot(id="qnap", name="NAS", host="192.168.4.2")
    )
    monkeypatch.setattr("app.routes.nas.poll_nas", poll)
    await auth_client.post(URL + "/test", json={**UPDATE, "password": None})
    assert poll.call_args.args[0].qts.password.get_secret_value() == "private-password"


async def test_concurrent_updates_preserve_both_nas_connections(nas_path):
    await asyncio.gather(
        *(
            asyncio.to_thread(
                nas_settings.save_connection,
                str(nas_path),
                network,
                nas_id,
                nas_settings.QTSConnectionUpdate(
                    username=nas_id, password=f"{nas_id}-private"
                ),
            )
            for network, nas_id in (("network-123", "qnap"), ("network-456", "other"))
        )
    )
    saved = json.loads(nas_path.read_text())
    assert [c["qts"]["password"] for c in saved] == ["qnap-private", "other-private"]
