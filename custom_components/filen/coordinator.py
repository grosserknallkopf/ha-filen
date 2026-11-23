"""Storage usage coordinator for Filen."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aiofilen import FilenAuthError, FilenClient, FilenError
from .const import DOMAIN, STORAGE_UPDATE_INTERVAL

if TYPE_CHECKING:
    from . import FilenConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StorageInfo:
    """Storage usage of the Filen account in bytes."""

    used: int
    total: int

    @property
    def free(self) -> int:
        return max(self.total - self.used, 0)


class FilenStorageCoordinator(DataUpdateCoordinator[StorageInfo]):
    """Poll storage usage; backups never depend on it."""

    config_entry: FilenConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: FilenConfigEntry, client: FilenClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=STORAGE_UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> StorageInfo:
        try:
            user = await self.client.user_info()
            return StorageInfo(used=user.storage_used, total=user.max_storage)
        except FilenAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except (FilenError, KeyError, TypeError, ValueError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err