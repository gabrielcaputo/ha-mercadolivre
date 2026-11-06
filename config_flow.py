"""Config flow for Mercado Livre."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import AuthError, MercadoLivreClient, MercadoLivreError, extract_ssid
from .const import (
    CONF_QUIET_HOURS,
    CONF_SSID,
    CONF_USER_AGENT,
    DOMAIN,
    SUBENTRY_ACCOUNT,
    SUBENTRY_DELIVERIES,
    SUBENTRY_SUMMARY,
)

_LOGGER = logging.getLogger(__name__)


def _account_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME, description={"suggested_value": defaults.get(CONF_NAME)}
            ): TextSelector(),
            vol.Required(CONF_SSID): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_USER_AGENT,
                description={"suggested_value": defaults.get(CONF_USER_AGENT)},
            ): TextSelector(),
        }
    )


async def _async_validate(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, Any], str | None, dict[str, str]]:
    """Check the cookie. Returns (account data, user id, errors)."""
    user_agent = (user_input.get(CONF_USER_AGENT) or "").strip() or None
    try:
        ssid = extract_ssid(user_input[CONF_SSID])
        session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
        user_id = await MercadoLivreClient(session, ssid, user_agent).async_get_user_id()
    except AuthError:
        return {}, None, {"base": "invalid_auth"}
    except MercadoLivreError:
        _LOGGER.exception("Error validating Mercado Livre session")
        return {}, None, {"base": "cannot_connect"}
    data: dict[str, Any] = {CONF_SSID: ssid}
    if user_agent:
        data[CONF_USER_AGENT] = user_agent
    return data, user_id, {}


class MercadoLivreConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the integration with its first account."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MercadoLivreOptionsFlow:
        """Options shared by every account."""
        return MercadoLivreOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Accounts are added as subentries."""
        return {SUBENTRY_ACCOUNT: AccountSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the first account."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, user_id, errors = await _async_validate(self.hass, user_input)
            if not errors:
                pt = self.hass.config.language.startswith("pt")
                return self.async_create_entry(
                    title="Mercado Livre",
                    data={},
                    subentries=[
                        {
                            "subentry_type": SUBENTRY_ACCOUNT,
                            "title": user_input[CONF_NAME].strip(),
                            "data": data,
                            "unique_id": user_id,
                        },
                        {
                            "subentry_type": SUBENTRY_SUMMARY,
                            "title": "Resumo" if pt else "Summary",
                            "data": {},
                            "unique_id": SUBENTRY_SUMMARY,
                        },
                        {
                            "subentry_type": SUBENTRY_DELIVERIES,
                            "title": "Entregas" if pt else "Deliveries",
                            "data": {},
                            "unique_id": SUBENTRY_DELIVERIES,
                        },
                    ],
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _account_schema(), user_input
            ),
            errors=errors,
        )


class AccountSubentryFlow(ConfigSubentryFlow):
    """Add an account, or replace its cookie when the session expires."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add an account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, user_id, errors = await _async_validate(self.hass, user_input)
            if not errors:
                if any(
                    s.unique_id == user_id
                    for s in self._get_entry().subentries.values()
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip(),
                    data=data,
                    unique_id=user_id,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _account_schema(), user_input
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Replace the cookie (or name, or user agent) of an account."""
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, user_id, errors = await _async_validate(self.hass, user_input)
            if not errors:
                if user_id != subentry.unique_id:
                    return self.async_abort(reason="wrong_account")
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=user_input[CONF_NAME].strip(),
                    data=data,
                )
        defaults = {
            CONF_NAME: subentry.title,
            CONF_USER_AGENT: subentry.data.get(CONF_USER_AGENT),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_account_schema(defaults),
            errors=errors,
        )


class MercadoLivreOptionsFlow(OptionsFlow):
    """Options shared by every account (changes reload the integration)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_QUIET_HOURS,
                    default=self.config_entry.options.get(CONF_QUIET_HOURS, True),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
