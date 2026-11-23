"""Backup platform for Filen."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
import contextlib
from dataclasses import dataclass
from functools import wraps
import json
import logging
from time import monotonic
from typing import Any, Concatenate

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    OnProgressCallback,
    suggested_filename,
)
from homeassistant.core import HomeAssistant, callback

from . import FilenConfigEntry
from .aiofilen import (
    DriveFile,
    FilenAccountError,
    FilenAuthError,
    FilenClient,
    FilenError,
    FilenNotFoundError,
    FilenQuotaError,
)
from .const import DATA_BACKUP_AGENT_LISTENERS, DOMAIN

_LOGGER = logging.getLogger(__name__)

CACHE_TTL = 300
RECENT_TTL = 600  # keep own uploads visible while Filen's listing catches up
METADATA_VERSION = 1
METADATA_SUFFIX = ".metadata.json"


async def async_get_backup_agents(
    hass: HomeAssistant, **kwargs: Any
) -> list[BackupAgent]:
    """Return one agent per loaded config entry."""
    entries: list[FilenConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    return [FilenBackupAgent(hass, entry) for entry in entries]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant, *, listener: Callable[[], None], **kwargs: Any
) -> Callable[[], None]:
    """Register a listener called when agents are added or removed."""
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


def suggested_names(backup: AgentBackup) -> tuple[str, str]:
    """Return the names of the backup file and its metadata file."""
    base = suggested_filename(backup).removesuffix(".tar")
    return f"{base}.tar", f"{base}{METADATA_SUFFIX}"


def handle_backup_errors[**P, R](
    func: Callable[Concatenate[FilenBackupAgent, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[FilenBackupAgent, P], Coroutine[Any, Any, R]]:
    """Turn library errors into the errors the backup manager expects."""

    @wraps(func)
    async def wrapper(
        self: FilenBackupAgent, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        try:
            return await func(self, *args, **kwargs)
        except (BackupAgentError, BackupNotFound):
            raise
        except FilenError as err:
            raise self.translate_error(err) from err
        except TimeoutError as err:
            raise BackupAgentError(
                translation_domain=DOMAIN, translation_key="operation_failed"
            ) from err

    return wrapper


@dataclass(slots=True)
class _StoredBackup:
    backup: AgentBackup
    tar: DriveFile
    metadata: DriveFile


class FilenBackupAgent(BackupAgent):
    """Store Home Assistant backups in Filen."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: FilenConfigEntry) -> None:
        """Initialize the agent."""
        super().__init__()
        self._hass = hass
        self._entry = entry
        self.name = entry.title
        self.unique_id = entry.entry_id
        self._cache: dict[str, _StoredBackup] = {}
        self._cache_expires = 0.0
        self._recent: dict[str, tuple[_StoredBackup, float]] = {}

    @property
    def _client(self) -> FilenClient:
        return self._entry.runtime_data.client

    @property
    def _folder_uuid(self) -> str:
        return self._entry.runtime_data.folder_uuid

    def translate_error(self, err: FilenError) -> BackupAgentError:
        """Map a library error to a translated backup error."""
        if isinstance(err, FilenNotFoundError):
            return BackupNotFound(
                translation_domain=DOMAIN, translation_key="backup_not_found"
            )
        if isinstance(err, FilenAuthError):
            self._entry.async_start_reauth(self._hass)
            key = "auth_failed"
        elif isinstance(err, FilenAccountError):
            key = "account_unusable"
        elif isinstance(err, FilenQuotaError):
            key = "quota_exceeded"
        else:
            key = "operation_failed"
        _LOGGER.debug("Filen error: %s", err, exc_info=err)
        return BackupAgentError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"error": str(err)},
        )

    # ------------------------------------------------------------------ agent API

    @handle_backup_errors
    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: OnProgressCallback,
        **kwargs: Any,
    ) -> None:
        """Upload a backup and then its metadata file."""
        await self._check_free_space(backup.size)
        tar_name, meta_name = suggested_names(backup)

        started = monotonic()
        tar = await self._client.upload(
            await open_stream(),
            size=backup.size,
            folder_uuid=self._folder_uuid,
            name=tar_name,
            mime="application/x-tar",
            progress=lambda sent: on_progress(bytes_uploaded=sent),
        )
        elapsed = max(monotonic() - started, 0.001)
        _LOGGER.debug(
            "Uploaded %s (%.1f MB) in %.0f s, %.1f MB/s",
            tar_name, backup.size / 1e6, elapsed, backup.size / 1e6 / elapsed,
        )  # fmt: skip

        metadata = json.dumps(
            {
                "metadata_version": METADATA_VERSION,
                "backup_id": backup.backup_id,
                "backup_metadata": backup.as_dict(),
            }
        ).encode()
        try:
            meta = await self._client.upload_bytes(
                metadata,
                folder_uuid=self._folder_uuid,
                name=meta_name,
                mime="application/json",
            )
        except FilenError:
            # without metadata HA cannot list the backup; don't leave an orphan
            with contextlib.suppress(FilenError):
                await self._client.delete_file(tar.uuid)
            raise

        stored = _StoredBackup(backup=backup, tar=tar, metadata=meta)
        self._recent[backup.backup_id] = (stored, monotonic())
        self._cache[backup.backup_id] = stored

    @handle_backup_errors
    async def async_download_backup(
        self, backup_id: str, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        """Stream a backup back from Filen."""
        stored = await self._find(backup_id)
        stream = await self._client.download(stored.tar)

        async def iterate() -> AsyncIterator[bytes]:
            try:
                async for chunk in stream:
                    yield chunk
            except FilenError as err:
                raise self.translate_error(err) from err
            finally:
                await stream.aclose()

        return iterate()

    @handle_backup_errors
    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        """Delete a backup and its metadata permanently (not to the trash)."""
        stored = await self._find(backup_id)
        await self._client.delete_file(stored.tar.uuid)
        with contextlib.suppress(FilenNotFoundError):
            await self._client.delete_file(stored.metadata.uuid)
        self._cache.pop(backup_id, None)
        self._recent.pop(backup_id, None)

    @handle_backup_errors
    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """List backups."""
        return [stored.backup for stored in (await self._list_cached()).values()]

    @handle_backup_errors
    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        """Return one backup."""
        return (await self._find(backup_id)).backup

    # ------------------------------------------------------------------ helpers

    async def _check_free_space(self, size: int) -> None:
        try:
            user = await self._client.user_info()
            used, total = user.storage_used, user.max_storage
        except (FilenAuthError, FilenAccountError):
            raise
        except (FilenError, KeyError, TypeError, ValueError) as err:
            _LOGGER.debug("Could not read storage usage, uploading anyway: %s", err)
            return
        if used + size > total:
            raise FilenQuotaError(
                f"backup needs {size / 1e9:.2f} GB, {max(total - used, 0) / 1e9:.2f} GB free"
            )

    async def _find(self, backup_id: str) -> _StoredBackup:
        if stored := (await self._list_cached()).get(backup_id):
            return stored
        raise BackupNotFound(
            translation_domain=DOMAIN, translation_key="backup_not_found"
        )

    async def _list_cached(self) -> dict[str, _StoredBackup]:
        now = monotonic()
        self._recent = {
            k: v for k, v in self._recent.items() if now - v[1] < RECENT_TTL
        }
        if now >= self._cache_expires:
            self._cache = await self._load_backups()
            self._cache_expires = now + CACHE_TTL
        for backup_id, (stored, _) in self._recent.items():
            self._cache.setdefault(backup_id, stored)
        return self._cache

    async def _load_backups(self) -> dict[str, _StoredBackup]:
        files = await self._client.list_files(self._folder_uuid)
        tars = {f.name: f for f in files if f.name.endswith(".tar")}
        metas = [f for f in files if f.name.endswith(METADATA_SUFFIX)]
        semaphore = asyncio.Semaphore(4)

        async def load(meta: DriveFile) -> _StoredBackup | None:
            tar = tars.get(meta.name.removesuffix(METADATA_SUFFIX) + ".tar")
            if tar is None:
                return None
            async with semaphore:
                raw = await self._client.download_bytes(meta)
            try:
                data = json.loads(raw)
                backup = AgentBackup.from_dict(
                    {**data["backup_metadata"], "size": tar.size}
                )
            except (ValueError, KeyError, TypeError) as err:
                _LOGGER.warning("Skipping unreadable metadata %s: %s", meta.name, err)
                return None
            return _StoredBackup(backup=backup, tar=tar, metadata=meta)

        results = await asyncio.gather(*(load(meta) for meta in metas))
        return {stored.backup.backup_id: stored for stored in results if stored}