"""Actions for Filen: upload files from Home Assistant, delete files in Filen."""

from __future__ import annotations

from collections.abc import AsyncIterator
import logging
from pathlib import Path, PurePosixPath
from typing import IO, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_FILENAME
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from . import FilenConfigEntry
from .aiofilen import (
    FilenAuthError,
    FilenClient,
    FilenError,
    FilenNotFoundError,
)
from .const import CONF_FOLDER_NAME, DEFAULT_FOLDER_NAME, DEFAULT_UPLOAD_FOLDER, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_CONFIG_ENTRY_ID = "config_entry_id"
CONF_DESTINATION_FOLDER = "destination_folder"
CONF_OVERWRITE = "overwrite"
CONF_PATH = "path"
CONF_PERMANENTLY = "permanently"

READ_CHUNK = 1024 * 1024

UPLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): cv.string,
        vol.Required(CONF_FILENAME): vol.All(
            cv.ensure_list, vol.Length(min=1), [cv.string]
        ),
        vol.Optional(CONF_DESTINATION_FOLDER, default=DEFAULT_UPLOAD_FOLDER): cv.string,
        vol.Optional(CONF_OVERWRITE, default=False): cv.boolean,
    }
)
DELETE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIG_ENTRY_ID): cv.string,
        vol.Required(CONF_PATH): vol.All(
            cv.ensure_list, vol.Length(min=1), [cv.string]
        ),
        vol.Optional(CONF_PERMANENTLY, default=False): cv.boolean,
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions. Admin only for users; automations and scripts may use them."""

    async def upload(call: ServiceCall) -> ServiceResponse:
        return await _async_upload(hass, call)

    async def delete(call: ServiceCall) -> ServiceResponse:
        return await _async_delete(hass, call)

    async_register_admin_service(
        hass, DOMAIN, "upload", upload, UPLOAD_SCHEMA, SupportsResponse.OPTIONAL
    )
    async_register_admin_service(
        hass, DOMAIN, "delete", delete, DELETE_SCHEMA, SupportsResponse.OPTIONAL
    )


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> FilenConfigEntry:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry


def _clean_folder(raw: str) -> str:
    """Normalize a Filen folder path; reject '..' and similar tricks."""
    parts = [p for p in raw.replace("\\", "/").split("/") if p.strip()]
    if any(p.strip() in (".", "..") for p in parts):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_path",
            translation_placeholders={"path": raw},
        )
    return "/".join(p.strip() for p in parts)


def _check_files(hass: HomeAssistant, filenames: list[str]) -> list[tuple[Path, int]]:
    """Validate local paths (allowlist, existence, unique names); runs in the executor."""
    result: list[tuple[Path, int]] = []
    seen: set[str] = set()
    for name in filenames:
        if not hass.config.is_allowed_path(name):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_access_to_path",
                translation_placeholders={"filename": name},
            )
        path = Path(name)
        if not path.is_file():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="file_not_found",
                translation_placeholders={"filename": name},
            )
        if path.name in seen:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="duplicate_name",
                translation_placeholders={"name": path.name},
            )
        seen.add(path.name)
        result.append((path, path.stat().st_size))
    return result


async def _read_file(hass: HomeAssistant, path: Path) -> AsyncIterator[bytes]:
    """Stream a local file without blocking the event loop."""
    handle: IO[bytes] = await hass.async_add_executor_job(path.open, "rb")
    try:
        while chunk := await hass.async_add_executor_job(handle.read, READ_CHUNK):
            yield chunk
    finally:
        await hass.async_add_executor_job(handle.close)


def _filen_error(
    hass: HomeAssistant,
    entry: FilenConfigEntry,
    err: FilenError,
    key: str,
    **placeholders: str,
) -> HomeAssistantError:
    if isinstance(err, FilenAuthError):
        entry.async_start_reauth(hass)
        return HomeAssistantError(
            translation_domain=DOMAIN, translation_key="auth_failed"
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={**placeholders, "error": str(err)},
    )


async def _async_upload(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry = _loaded_entry(hass, call.data[CONF_CONFIG_ENTRY_ID])
    folder_path = _clean_folder(call.data[CONF_DESTINATION_FOLDER])
    files = await hass.async_add_executor_job(
        _check_files, hass, call.data[CONF_FILENAME]
    )
    client = entry.runtime_data.client
    uploaded: list[dict[str, Any]] = []
    current = folder_path
    try:
        folder_uuid = await client.ensure_folder_path(
            client.root_folder_uuid, folder_path
        )
        for path, size in files:
            current = str(path)
            existing = await client.find_file(folder_uuid, path.name)
            if existing is not None and not call.data[CONF_OVERWRITE]:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="file_exists",
                    translation_placeholders={"path": f"{folder_path}/{path.name}"},
                )
            # Filen identifies files by uuid, so the new version can be uploaded
            # under the final name right away; the old one is trashed afterwards.
            result = await client.upload(
                _read_file(hass, path),
                size=size,
                folder_uuid=folder_uuid,
                name=path.name,
            )
            if existing is not None:
                try:
                    await client.trash_file(existing.uuid)
                except FilenError as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="overwrite_incomplete",
                        translation_placeholders={
                            "path": f"{folder_path}/{path.name}",
                            "error": str(err),
                        },
                    ) from err
            uploaded.append(
                {
                    "path": f"{folder_path}/{path.name}".lstrip("/"),
                    "size": size,
                    "uuid": result.uuid,
                }
            )
    except FilenError as err:
        raise _filen_error(hass, entry, err, "upload_failed", filename=current) from err
    _LOGGER.debug("Uploaded %d file(s) to Filen folder %s", len(uploaded), folder_path)
    return {"files": uploaded}


async def _async_delete(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry = _loaded_entry(hass, call.data[CONF_CONFIG_ENTRY_ID])
    client = entry.runtime_data.client
    backup_folder = entry.data.get(CONF_FOLDER_NAME, DEFAULT_FOLDER_NAME)
    targets = [PurePosixPath("/" + _clean_folder(raw)) for raw in call.data[CONF_PATH]]
    for raw, target in zip(call.data[CONF_PATH], targets, strict=True):
        if target.parts[1:2] == (backup_folder,):
            # Backups are managed by Home Assistant's backup settings (retention).
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="backup_folder_protected",
                translation_placeholders={"path": raw, "folder": backup_folder},
            )

    deleted: list[str] = []
    missing: list[str] = []
    failed: list[str] = []
    for raw, target in zip(call.data[CONF_PATH], targets, strict=True):
        try:
            folder_uuid = await client.find_folder_path(
                client.root_folder_uuid, str(target.parent)
            )
            file = (
                await client.find_file(folder_uuid, target.name)
                if folder_uuid
                else None
            )
            if file is None:
                missing.append(raw)
                continue
            if call.data[CONF_PERMANENTLY]:
                await client.delete_file(file.uuid)
            else:
                await client.trash_file(file.uuid)
            deleted.append(str(target).lstrip("/"))
        except FilenNotFoundError:
            missing.append(raw)
        except FilenAuthError as err:
            raise _filen_error(hass, entry, err, "delete_failed", path=raw) from err
        except FilenError as err:
            _LOGGER.warning("Deleting %s in Filen failed: %s", raw, err)
            failed.append(f"{raw} ({err})")

    if failed:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="delete_failed",
            translation_placeholders={
                "path": ", ".join(failed),
                "error": f"{len(deleted)} deleted",
            },
        )
    if missing:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="path_not_found",
            translation_placeholders={"path": ", ".join(missing)},
        )
    return {"deleted": deleted}