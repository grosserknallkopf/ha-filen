"""Constants for the Filen integration."""

from collections.abc import Callable
from datetime import timedelta
from typing import Final

from homeassistant.util.hass_dict import HassKey

DOMAIN: Final = "filen"

CONF_CREDENTIALS: Final = "credentials"
CONF_FOLDER_NAME: Final = "folder_name"

DEFAULT_FOLDER_NAME: Final = "Home Assistant Backups"
CLIENT_NAME: Final = "ha-filen"

DATA_BACKUP_AGENT_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(
    f"{DOMAIN}.backup_agent_listeners"
)

DEFAULT_UPLOAD_FOLDER: Final = "Home Assistant Uploads"
STORAGE_UPDATE_INTERVAL: Final = timedelta(minutes=30)
MEDIA_VIEW_URL: Final = "/api/filen/media"