"""Diagnostics for Filen."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant

from . import FilenConfigEntry
from .aiofilen import FilenError
from .const import CONF_CREDENTIALS

TO_REDACT = {CONF_EMAIL, CONF_CREDENTIALS, "title", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FilenConfigEntry
) -> dict[str, Any]:
    """Return diagnostics without secrets (e-mail and all keys are redacted)."""
    result: dict[str, Any] = {"entry": async_redact_data(entry.as_dict(), TO_REDACT)}
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return result
    client = runtime.client
    try:
        user = await client.user_info()
        result["storage"] = {
            "used_bytes": user.storage_used,
            "limit_bytes": user.max_storage,
        }
        files = await client.list_files(runtime.folder_uuid)
        result["backup_folder"] = {
            "files": len(files),
            "tar_files": sum(1 for f in files if f.name.endswith(".tar")),
            "total_bytes": sum(f.size for f in files),
        }
    except FilenError as err:
        result["error"] = f"{type(err).__name__}: {err}"
    return result