"""Config flow for Filen."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_CODE, CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .aiofilen import (
    FilenAccountBlocked,
    FilenAuthError,
    FilenClient,
    FilenConnectionError,
    FilenError,
    FilenTwoFactorRequired,
)
from .const import (
    CONF_CREDENTIALS,
    CONF_FOLDER_NAME,
    DEFAULT_FOLDER_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PREFLIGHT_FILE = "ha-filen-connection-test.txt"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)
STEP_PASSWORD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)
STEP_TWO_FACTOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CODE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code")
        ),
    }
)


class FilenConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Filen."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._client: FilenClient | None = None

    def _placeholders(self) -> dict[str, str]:
        return {"email": self._email or ""}

    def _get_client(self) -> FilenClient:
        if self._client is None:
            self._client = FilenClient(async_get_clientsession(self.hass))
        return self._client

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for e-mail and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip().lower()
            self._password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(None)  # set after login, from the account id
            result = await self._async_login(None, errors)
            if result is not None:
                return result
            if errors.get("base") == "invalid_2fa":
                # Filen only reveals 2FA on the first login attempt
                return await self.async_step_two_factor()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, {CONF_EMAIL: self._email} if self._email else {}
            ),
            description_placeholders=self._placeholders(),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication when the stored API key stopped working."""
        self._email = entry_data[CONF_EMAIL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            result = await self._async_login(None, errors)
            if result is not None:
                return result
            if errors.get("base") == "invalid_2fa":
                return await self.async_step_two_factor()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_PASSWORD_SCHEMA,
            description_placeholders=self._placeholders(),
            errors=errors,
        )

    async def async_step_two_factor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the 2FA code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = "".join(user_input[CONF_CODE].split())
            result = await self._async_login(code, errors)
            if result is not None:
                return result
            if errors.get("base") == "invalid_auth":
                # wrong password: go back to where the password is entered
                step = "reauth_confirm" if self.source == SOURCE_REAUTH else "user"
                return self.async_show_form(
                    step_id=step,
                    data_schema=STEP_PASSWORD_SCHEMA
                    if step == "reauth_confirm"
                    else STEP_USER_SCHEMA,
                    description_placeholders=self._placeholders(),
                    errors=errors,
                )
        return self.async_show_form(
            step_id="two_factor",
            data_schema=STEP_TWO_FACTOR_SCHEMA,
            description_placeholders=self._placeholders(),
            errors=errors,
        )

    async def _async_login(
        self, code: str | None, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        assert self._email is not None and self._password is not None
        client = self._get_client()
        try:
            credentials = await client.login(self._email, self._password, code)
        except FilenTwoFactorRequired:
            errors["base"] = "invalid_2fa"
            return None
        except FilenAccountBlocked:
            errors["base"] = "account_blocked"
            return None
        except FilenAuthError:
            errors["base"] = "invalid_auth"
            return None
        except FilenConnectionError:
            errors["base"] = "cannot_connect"
            return None
        except FilenError:
            _LOGGER.exception("Unexpected error during login")
            errors["base"] = "unknown"
            return None
        self._password = None  # not needed anymore, never stored

        await self.async_set_unique_id(str(client.user.user_id))
        data = {CONF_CREDENTIALS: credentials.as_dict()}
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        self._abort_if_unique_id_configured()

        if not await self._async_preflight(client, errors):
            return None
        return self.async_create_entry(
            title=self._email,
            data={
                CONF_EMAIL: self._email,
                CONF_FOLDER_NAME: DEFAULT_FOLDER_NAME,
                **data,
            },
        )

    async def _async_preflight(
        self, client: FilenClient, errors: dict[str, str]
    ) -> bool:
        """Create the backup folder and round-trip a small file, so problems show up now."""
        try:
            folder = await client.ensure_folder(
                client.root_folder_uuid, DEFAULT_FOLDER_NAME
            )
            probe = b"Home Assistant connection test for Filen.\n"
            uploaded = await client.upload_bytes(
                probe, folder_uuid=folder.uuid, name=PREFLIGHT_FILE
            )
            try:
                if await client.download_bytes(uploaded) != probe:
                    raise FilenError("connection test file came back different")
            finally:
                await client.delete_file(uploaded.uuid)
        except FilenConnectionError:
            errors["base"] = "cannot_connect"
            return False
        except FilenError:
            _LOGGER.exception("Connection test with Filen failed")
            errors["base"] = "preflight_failed"
            return False
        return True