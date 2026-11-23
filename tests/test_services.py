"""Tests for the upload/delete actions and the storage sensors."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.filen.const import DEFAULT_FOLDER_NAME, DOMAIN

from .common import MockConfigEntry
from .conftest import FakeClient


async def _upload(
    hass: HomeAssistant, entry: MockConfigEntry, files: list[str], **extra
):
    return await hass.services.async_call(
        DOMAIN, "upload",
        {"config_entry_id": entry.entry_id, "filename": files, **extra},
        blocking=True, return_response=True,
    )  # fmt: skip


async def _delete(
    hass: HomeAssistant, entry: MockConfigEntry, paths: list[str], **extra
):
    return await hass.services.async_call(
        DOMAIN, "delete",
        {"config_entry_id": entry.entry_id, "path": paths, **extra},
        blocking=True, return_response=True,
    )  # fmt: skip


async def test_upload_creates_folders_and_streams(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    snap = tmp_path / "front_door.jpg"
    snap.write_bytes(b"\xff\xd8" + b"x" * 3_000_000)
    response = await _upload(hass, loaded, [str(snap)], destination_folder="HA/Kameras")
    assert response["files"][0]["path"] == "HA/Kameras/front_door.jpg"
    assert response["files"][0]["size"] == snap.stat().st_size
    stored = next(f for f in fake_client.files.values() if f.name == "front_door.jpg")
    assert fake_client.data[stored.uuid] == snap.read_bytes()
    names = {f.name for f in fake_client.folders.values()}
    assert {"HA", "Kameras"} <= names


async def test_upload_default_folder_and_overwrite(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    f = tmp_path / "a.txt"
    f.write_text("one")
    await _upload(hass, loaded, [str(f)])
    with pytest.raises(ServiceValidationError) as err:
        await _upload(hass, loaded, [str(f)])
    assert err.value.translation_key == "file_exists"

    f.write_text("two")
    await _upload(hass, loaded, [str(f)], overwrite=True)
    stored = next(
        f2 for f2 in fake_client.files.values() if f2.name == "a.txt"
    )
    assert fake_client.data[stored.uuid] == b"two"
    assert len(fake_client.trashed) == 1
    assert len(fake_client.files) == 1


async def test_upload_rejects_unallowed_path(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path
) -> None:
    outside = tmp_path / "sub" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("x")
    hass.config.allowlist_external_dirs = set()
    with pytest.raises(ServiceValidationError) as err:
        await _upload(hass, loaded, [str(outside)])
    assert err.value.translation_key == "no_access_to_path"


async def test_upload_rejects_path_traversal(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path
) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")
    with pytest.raises(ServiceValidationError) as err:
        await _upload(hass, loaded, [str(f)], destination_folder="../etc")
    assert err.value.translation_key == "invalid_path"


async def test_delete_trash_and_permanent(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
) -> None:
    f = tmp_path / "a.txt"
    f.write_text("one")
    await _upload(hass, loaded, [str(f)], destination_folder="HA")
    response = await _delete(hass, loaded, ["HA/a.txt"])
    assert response["deleted"] == ["HA/a.txt"]
    assert len(fake_client.trashed) == 1

    f.write_text("two")
    await _upload(hass, loaded, [str(f)], destination_folder="HA")
    await _delete(hass, loaded, ["HA/a.txt"], permanently=True)
    assert len(fake_client.deleted) == 1
    assert fake_client.files == {}


async def test_delete_missing_path(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await _delete(hass, loaded, ["HA/gibt-es-nicht.txt"])
    assert err.value.translation_key == "path_not_found"


async def test_delete_protects_backup_folder(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await _delete(hass, loaded, [f"{DEFAULT_FOLDER_NAME}/backup.tar"])
    assert err.value.translation_key == "backup_folder_protected"


async def test_storage_sensors(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.storage_used = 2 * 1024**3
    fake_client.max_storage = 10 * 1024**3
    await fake_client_coordinator_refresh(hass, loaded)
    registry = er.async_get(hass)
    # byte sensors are displayed in GB (10^9 bytes, suggested unit)
    for key, expected in (
        ("storage_used", 2 * 1024**3 / 1e9),
        ("storage_free", 8 * 1024**3 / 1e9),
        ("storage_total", 10 * 1024**3 / 1e9),
        ("storage_used_percent", 20.0),
    ):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"42_{key}")
        assert entity_id is not None, key
        state = hass.states.get(entity_id)
        assert float(state.state) == pytest.approx(expected), key


async def fake_client_coordinator_refresh(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()