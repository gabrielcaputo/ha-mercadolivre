"""The Mercado Livre integration."""

from __future__ import annotations

import asyncio
import random
from types import MappingProxyType

import aiohttp

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import MercadoLivreClient
from .const import (
    CONF_SSID,
    CONF_USER_AGENT,
    DOMAIN,
    REQUEST_DELAY,
    SUBENTRY_ACCOUNT,
    SUBENTRY_DELIVERIES,
    SUBENTRY_SUMMARY,
)
from .coordinator import AccountCoordinator, MercadoLivreConfigEntry, MercadoLivreRuntime
from .entity import (
    account_device_info,
    async_remove_untracked_devices,
    summary_device_info,
)
from .driver import DriverManager
from .geocode import Geocoder

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER, Platform.SENSOR, Platform.TEXT]


async def async_setup_entry(hass: HomeAssistant, entry: MercadoLivreConfigEntry) -> bool:
    """Set up Mercado Livre from a config entry."""
    summary_id = _ensure_subentry(hass, entry, SUBENTRY_SUMMARY, "Resumo", "Summary")
    deliveries_id = _ensure_subentry(
        hass, entry, SUBENTRY_DELIVERIES, "Entregas", "Deliveries"
    )

    # Own session without a cookie jar: each account's cookie is sent
    # explicitly and nothing Mercado Livre sets leaks between accounts.
    session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
    geocoder = Geocoder(hass)
    accounts: dict[str, AccountCoordinator] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_ACCOUNT:
            continue
        client = MercadoLivreClient(
            session, subentry.data[CONF_SSID], subentry.data.get(CONF_USER_AGENT)
        )
        accounts[subentry.subentry_id] = AccountCoordinator(
            hass, entry, subentry, client, geocoder
        )
    driver = DriverManager(hass, entry)
    entry.runtime_data = MercadoLivreRuntime(
        accounts=accounts,
        summary_subentry_id=summary_id,
        deliveries_subentry_id=deliveries_id,
        driver=driver,
    )

    # One account at a time, spaced out. A failing account doesn't fail the
    # setup: it reports itself through its session sensor and a repair issue.
    for index, coordinator in enumerate(accounts.values()):
        if index:
            await asyncio.sleep(random.uniform(*REQUEST_DELAY))
        await coordinator.async_refresh()

    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=summary_id,
        **summary_device_info(),
    )
    for subentry_id, coordinator in accounts.items():
        # Created up front so package devices can link to it.
        coordinator.account_device_id = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry_id,
            **account_device_info(coordinator),
        ).id
    _delete_orphan_issues(hass, accounts)

    @callback
    def _cleanup() -> None:
        async_remove_untracked_devices(hass, entry)

    # Packages delivered while Home Assistant was down or of removed
    # accounts, then on every update.
    _cleanup()
    for coordinator in accounts.values():
        entry.async_on_unload(coordinator.async_add_listener(_cleanup))

    # Drivers being followed before a restart, with their last position.
    await driver.async_load()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    driver.async_start()
    entry.async_on_unload(driver.async_unload)
    # Maps that status pages start showing (package out for delivery).
    for coordinator in accounts.values():
        entry.async_on_unload(
            coordinator.async_add_listener(driver.async_follow_status_pages)
        )
    # Accounts added, removed or reconfigured, and option changes.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: MercadoLivreConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _ensure_subentry(
    hass: HomeAssistant,
    entry: MercadoLivreConfigEntry,
    kind: str,
    title_pt: str,
    title_en: str,
) -> str:
    """Return the id of the subentry grouping one kind of device, creating it."""
    for subentry in entry.subentries.values():
        if subentry.unique_id == kind:
            return subentry.subentry_id
    subentry = ConfigSubentry(
        data=MappingProxyType({}),
        subentry_type=kind,
        title=title_pt if hass.config.language.startswith("pt") else title_en,
        unique_id=kind,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry.subentry_id


def _delete_orphan_issues(
    hass: HomeAssistant, accounts: dict[str, AccountCoordinator]
) -> None:
    """Drop "session expired" issues of accounts that were removed."""
    current = {f"session_expired_{c.user_id}" for c in accounts.values()}
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id not in current:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_unload_entry(hass: HomeAssistant, entry: MercadoLivreConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
