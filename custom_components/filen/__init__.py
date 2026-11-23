"""The Filen integration: Home Assistant backups in Filen."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .aiofilen import (
    Credentials,
    FilenAccountError,
    FilenAuthError,
    FilenClient,
    FilenError,
)
from .const import (
    CONF_CREDENTIALS,
    CONF_FOLDER_NAME,
    DATA_BACKUP_AGENT_LISTENERS,
    DEFAULT_FOLDER_NAME,
    DOMAIN,
)
from .coordinator import FilenStorageCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class FilenRuntimeData:
    """Objects shared by the backup agent, actions, sensors and media source."""

    client: FilenClient
    folder_uuid: str
    coordinator: FilenStorageCoordinator


type FilenConfigEntry = ConfigEntry[FilenRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once for all accounts."""
    from .services import async_setup_services  # noqa: PLC0415

    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FilenConfigEntry) -> bool:
    """Set up Filen from a config entry."""
    client = FilenClient(async_get_clientsession(hass))
    client.set_credentials(Credentials.from_dict(entry.data[CONF_CREDENTIALS]))
    try:
        # Filen's API key is long-lived; one account call validates it.
        await client.user_info()
        folder = await client.ensure_folder(
            client.root_folder_uuid,
            entry.data.get(CONF_FOLDER_NAME, DEFAULT_FOLDER_NAME),
        )
    except FilenAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except FilenAccountError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="account_unusable",
            translation_placeholders={"error": str(err)},
        ) from err
    except FilenError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err

    coordinator = FilenStorageCoordinator(hass, entry, client)
    entry.runtime_data = FilenRuntimeData(
        client=client, folder_uuid=folder.uuid, coordinator=coordinator
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Storage sensors are extras: read usage in the background so a slow answer
    # never delays the backup agent; the sensors fill in a few seconds later.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} storage first refresh"
    )

    def _notify_backup_listeners() -> None:
        for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
            listener()

    entry.async_on_unload(entry.async_on_state_change(_notify_backup_listeners))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FilenConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)