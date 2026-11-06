"""Live driver position from the shared tracking link.

While a package is out for delivery its status page shows the driver on a
map, with a "share" button for a public link whose page polls a JSON with
the driver's position. The coordinator reads that link from the status page
and the driver is followed from it until the package is delivered. A link
shared from the app can still be pasted (text entity).

This runs apart from the account coordinators: it polls far more often, uses
no cookie, and must not trigger their cleanup and entity updates.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
import random
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import (
    BlockedError,
    DriverPosition,
    MercadoLivreError,
    Package,
    ParseError,
    SharedTrackingClient,
    TrackingEndedError,
    extract_shared_token,
)
from .const import (
    BACKOFF_BASE,
    CONF_USER_AGENT,
    DOMAIN,
    DRIVER_GRACE,
    DRIVER_INTERVAL,
    DRIVER_JITTER,
    DRIVER_POSITIONS_SAVE_DELAY,
    DRIVER_POSITIONS_STORAGE_KEY,
    DRIVER_STORAGE_KEY,
    STATUS_OUT_FOR_DELIVERY,
    STORAGE_VERSION,
)

if TYPE_CHECKING:
    from .coordinator import MercadoLivreConfigEntry

_LOGGER = logging.getLogger(__name__)

# After a network error, try again in this long.
_RETRY_SECONDS = 120


class DriverManager:
    """Follows the driver of each package that has a shared tracking link."""

    def __init__(self, hass: HomeAssistant, entry: MercadoLivreConfigEntry) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self._session = async_create_clientsession(
            hass, cookie_jar=aiohttp.DummyCookieJar()
        )
        # Shipment id -> token. The link is a public bearer link to the
        # user's address, so it's kept only here, never in a state.
        self._store: Store[dict[str, str]] = Store(
            hass, STORAGE_VERSION, DRIVER_STORAGE_KEY
        )
        self._positions_store: Store[dict[str, dict[str, Any]]] = Store(
            hass, STORAGE_VERSION, DRIVER_POSITIONS_STORAGE_KEY
        )
        self._clients: dict[str, SharedTrackingClient] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._listeners: dict[str, list[CALLBACK_TYPE]] = {}
        self.positions: dict[str, DriverPosition] = {}
        self.updated_at: dict[str, datetime] = {}
        # Links that stopped working, so they aren't followed again while the
        # status page still shows them.
        self._ended_tokens: set[str] = set()

    def _client(self, token: str) -> SharedTrackingClient:
        user_agent = next(
            (
                c.subentry.data.get(CONF_USER_AGENT)
                for c in self.entry.runtime_data.accounts.values()
            ),
            None,
        )
        return SharedTrackingClient(self._session, token, user_agent)

    async def async_load(self) -> None:
        """Restore the links and last positions saved before a restart.

        Runs before the entities are added, so their first state already
        shows the driver.
        """
        for shipment_id, token in (await self._store.async_load() or {}).items():
            self._clients[shipment_id] = self._client(token)
        saved = await self._positions_store.async_load() or {}
        for shipment_id, item in saved.items():
            if shipment_id not in self._clients:
                continue
            try:
                self.positions[shipment_id] = DriverPosition(
                    shipment_id,
                    latitude=item["latitude"],
                    longitude=item["longitude"],
                    accuracy=item.get("accuracy"),
                    low_accuracy=item.get("low_accuracy", False),
                )
                self.updated_at[shipment_id] = datetime.fromisoformat(
                    item["updated_at"]
                )
            except (KeyError, TypeError, ValueError):
                continue

    @callback
    def async_start(self) -> None:
        """Resume polling the restored links, now that packages are known."""
        for shipment_id in self._clients:
            self._schedule(shipment_id, 0)
        self.async_follow_status_pages()

    @callback
    def async_follow_status_pages(self) -> None:
        """Follow the drivers whose map the status pages show.

        Called after each account update, so the coordinator data the polls
        check is already the one that named the link.
        """
        added = False
        for coordinator in self.entry.runtime_data.accounts.values():
            if coordinator.data is None:
                continue
            for package in coordinator.data.pending:
                token = package.share_token
                if (
                    token is None
                    or package.status != STATUS_OUT_FOR_DELIVERY
                    or package.shipment_id in self._clients
                    or token in self._ended_tokens
                ):
                    continue
                self._clients[package.shipment_id] = self._client(token)
                self._schedule(package.shipment_id, 0)
                added = True
                _LOGGER.info(
                    "Following the driver of shipment %s (status page map)",
                    package.shipment_id,
                )
        if added:
            self.entry.async_create_background_task(
                self.hass, self._async_save(), f"{DOMAIN} save shared links"
            )

    @callback
    def async_unload(self) -> None:
        """Stop polling (the links stay saved)."""
        for unsub in self._timers.values():
            unsub()
        self._timers.clear()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {sid: client.token for sid, client in self._clients.items()}
        )

    def _positions_data(self) -> dict[str, dict[str, Any]]:
        return {
            sid: {
                "latitude": position.latitude,
                "longitude": position.longitude,
                "accuracy": position.accuracy,
                "low_accuracy": position.low_accuracy,
                "updated_at": self.updated_at[sid].isoformat(),
            }
            for sid, position in self.positions.items()
            if sid in self.updated_at
        }

    @callback
    def _save_positions(self) -> None:
        # Delayed: positions change every ~30 s; the store flushes on shutdown.
        self._positions_store.async_delay_save(
            self._positions_data, DRIVER_POSITIONS_SAVE_DELAY
        )

    def _package(self, shipment_id: str) -> tuple[Package | None, bool]:
        """The tracked package, and whether every account has fresh data."""
        fetched = True
        for coordinator in self.entry.runtime_data.accounts.values():
            data = coordinator.data
            if data is None or not data.fetched:
                fetched = False
                continue
            if package := data.tracked.get(shipment_id):
                return package, True
        return None, fetched

    def _out_for_delivery(self) -> list[str]:
        return [
            p.shipment_id
            for c in self.entry.runtime_data.accounts.values()
            if c.data
            for p in c.data.pending
            if p.status == STATUS_OUT_FOR_DELIVERY
        ]

    async def async_add_link(self, value: str) -> None:
        """Start following the driver of the link's package."""
        try:
            token = extract_shared_token(value)
        except ParseError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_link"
            ) from err
        client = self._client(token)
        try:
            position = await client.async_get_position()
        except MercadoLivreError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="link_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        shipment_id = position.shipment_id
        if shipment_id is None or self._package(shipment_id)[0] is None:
            # The id comes from the map's timer name; if that ever changes,
            # the package out for delivery is the one being shared.
            candidates = self._out_for_delivery()
            if len(candidates) != 1:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="shipment_not_found"
                )
            shipment_id = candidates[0]

        self._cancel(shipment_id)
        self._clients[shipment_id] = client
        await self._async_save()
        self._update(shipment_id, position)
        self._schedule(shipment_id, None)
        _LOGGER.info("Following the driver of shipment %s", shipment_id)

    def _cancel(self, shipment_id: str) -> None:
        if unsub := self._timers.pop(shipment_id, None):
            unsub()

    def _schedule(self, shipment_id: str, delay: float | None) -> None:
        if delay is None:
            delay = DRIVER_INTERVAL.total_seconds() + random.uniform(
                0, DRIVER_JITTER.total_seconds()
            )

        @callback
        def _run(_now: datetime) -> None:
            self._timers.pop(shipment_id, None)
            self.entry.async_create_background_task(
                self.hass,
                self._async_poll(shipment_id),
                f"{DOMAIN} driver {shipment_id}",
            )

        self._cancel(shipment_id)
        self._timers[shipment_id] = async_call_later(
            self.hass, delay, HassJob(_run, cancel_on_shutdown=True)
        )

    def _should_stop(self, shipment_id: str) -> bool:
        package, fetched = self._package(shipment_id)
        if package is None:
            # Unknown only because an account couldn't fetch: keep waiting.
            return fetched
        if package.status != STATUS_OUT_FOR_DELIVERY:
            return True
        end = package.eta_window_end
        return end is not None and dt_util.now() > end + DRIVER_GRACE

    async def _async_poll(self, shipment_id: str) -> None:
        client = self._clients.get(shipment_id)
        if client is None:
            return
        if self._should_stop(shipment_id):
            await self._async_stop(shipment_id, "the package is no longer out for delivery")
            return
        try:
            position = await client.async_get_position()
        except TrackingEndedError as err:
            await self._async_stop(shipment_id, f"the link stopped working ({err})")
            return
        except BlockedError as err:
            _LOGGER.warning("Shared tracking blocked (%s); pausing", err)
            self._schedule(shipment_id, BACKOFF_BASE.total_seconds())
            return
        except MercadoLivreError as err:
            _LOGGER.debug("Could not read the driver position: %s", err)
            self._schedule(shipment_id, _RETRY_SECONDS)
            return
        if not position.has_position and position.next_path is None:
            # The page stopped refreshing its map: the delivery is over.
            await self._async_stop(shipment_id, "the map is gone")
            return
        self._update(shipment_id, position)
        self._schedule(shipment_id, None)

    async def _async_stop(self, shipment_id: str, reason: str) -> None:
        _LOGGER.info("Stopped following the driver of %s: %s", shipment_id, reason)
        self._cancel(shipment_id)
        if client := self._clients.pop(shipment_id, None):
            self._ended_tokens.add(client.token)
        self.positions.pop(shipment_id, None)
        self.updated_at.pop(shipment_id, None)
        await self._async_save()
        self._save_positions()
        self._notify(shipment_id)

    def _update(self, shipment_id: str, position: DriverPosition) -> None:
        if position.has_position:
            self.positions[shipment_id] = position
            self.updated_at[shipment_id] = dt_util.now()
            self._save_positions()
        self._notify(shipment_id)

    def _notify(self, shipment_id: str) -> None:
        for listener in list(self._listeners.get(shipment_id, [])):
            listener()

    @callback
    def async_add_listener(
        self, shipment_id: str, listener: CALLBACK_TYPE
    ) -> Callable[[], None]:
        """Call ``listener`` when this package's driver position changes."""
        self._listeners.setdefault(shipment_id, []).append(listener)

        @callback
        def _remove() -> None:
            self._listeners[shipment_id].remove(listener)

        return _remove
