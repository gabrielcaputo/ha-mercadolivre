"""Data update coordinator for Mercado Livre."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import logging
import os
import random
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    TZ,
    AuthError,
    BlockedError,
    MercadoLivreClient,
    MercadoLivreError,
    Package,
    TrackingEvent,
    parse_delivered,
    parse_eta,
    parse_eta_window,
)
from .const import (
    BACKOFF_BASE,
    BACKOFF_MAX,
    CONF_QUIET_HOURS,
    DELIVERED_DAYS,
    DOMAIN,
    FINAL_STATES,
    HISTORY_SAVE_DELAY,
    HISTORY_STORAGE_KEY,
    KEEP_FINAL_FOR,
    QUIET_HOURS_END,
    QUIET_HOURS_START,
    REQUEST_DELAY,
    STATUS_DELIVERED,
    STATUS_OUT_FOR_DELIVERY,
    STATUS_REFRESH,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_OUT_FOR_DELIVERY,
    UPDATE_JITTER,
    UPDATE_JITTER_OUT_FOR_DELIVERY,
)
from .driver import DriverManager
from .geocode import Geocoder

_LOGGER = logging.getLogger(__name__)

# Status changes, under the config folder, to check later how the pages
# were read. Written only while this logger is at debug level.
DEBUG_DIR = "mercadolivre_debug"
DEBUG_TRANSITIONS_FILE = "transitions.jsonl"

type MercadoLivreConfigEntry = ConfigEntry[MercadoLivreRuntime]

SESSION_OK = "ok"
SESSION_EXPIRED = "expired"
SESSION_BLOCKED = "blocked"
SESSION_ERROR = "error"
SESSION_STATES = [SESSION_OK, SESSION_EXPIRED, SESSION_BLOCKED, SESSION_ERROR]


@dataclass
class MercadoLivreRuntime:
    """Runtime data of the config entry."""

    # One coordinator per account subentry, keyed by subentry id.
    accounts: dict[str, AccountCoordinator]
    summary_subentry_id: str
    deliveries_subentry_id: str
    driver: DriverManager


@dataclass
class MercadoLivreData:
    """Everything the entities show."""

    today: date
    # False when this data is a placeholder (requests blocked during setup);
    # nothing should be cleaned up based on it.
    fetched: bool = True
    # Packages with their own device: pending ones, plus ones that just
    # reached a final state (kept for a while so automations see the change).
    tracked: dict[str, Package] = field(default_factory=dict)
    # Delivered within the last DELIVERED_DAYS days, newest first.
    delivered: list[Package] = field(default_factory=list)

    @property
    def pending(self) -> list[Package]:
        """Packages still to be received."""
        return [p for p in self.tracked.values() if p.status not in FINAL_STATES]

    @property
    def arriving_today(self) -> list[Package]:
        """Pending packages expected today (or already out for delivery)."""
        return [
            p
            for p in self.pending
            if p.status == STATUS_OUT_FOR_DELIVERY
            or (p.eta_start and p.eta_end and p.eta_start <= self.today <= p.eta_end)
        ]


def _jittered(base: timedelta, jitter: timedelta) -> timedelta:
    return base + timedelta(seconds=random.uniform(0, jitter.total_seconds()))


class AccountCoordinator(DataUpdateCoordinator[MercadoLivreData]):
    """Polls one account's purchases list and the tracking of its packages."""

    config_entry: MercadoLivreConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MercadoLivreConfigEntry,
        subentry: ConfigSubentry,
        client: MercadoLivreClient,
        geocoder: Geocoder,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {subentry.title}",
            update_interval=_jittered(UPDATE_INTERVAL, UPDATE_JITTER),
        )
        self.subentry = subentry
        self.user_id: str = subentry.unique_id or subentry.subentry_id
        self.client = client
        self.geocoder = geocoder
        self.account_device_id: str | None = None
        self.session = SESSION_OK
        self._blocked_count = 0
        # The history of each tracked package, kept across restarts: the
        # status page drops it once delivered.
        self._history_store: Store[dict[str, list[dict[str, Any]]]] = Store(
            hass, STORAGE_VERSION, f"{HISTORY_STORAGE_KEY}.{self.user_id}"
        )
        self._restored_history: dict[str, list[TrackingEvent]] | None = None
        # Packs whose status page was read and that aren't worth following
        # (old, cancelled, not a shipment), so it isn't read again.
        self._ignored_packs: set[str | None] = set()
        # Packs to be received whose status page didn't name a shipment yet
        # (payment pending...): read again after this time.
        self._pack_retry_at: dict[str | None, datetime] = {}

    @property
    def account_name(self) -> str:
        """Name the user gave the account."""
        return self.subentry.title

    @property
    def _issue_id(self) -> str:
        return f"session_expired_{self.user_id}"

    def _in_quiet_hours(self, now: datetime) -> bool:
        if not self.config_entry.options.get(CONF_QUIET_HOURS, True):
            return False
        # Keep polling at night only for a package due today that is out for
        # delivery (not one stuck in that state since a failed attempt).
        if self.data and any(
            p.status == STATUS_OUT_FOR_DELIVERY and p.eta_end == now.date()
            for p in self.data.pending
        ):
            return False
        return QUIET_HOURS_START <= now.hour < QUIET_HOURS_END

    async def _async_update_data(self) -> MercadoLivreData:
        now = dt_util.now()
        if self.data is not None and self._in_quiet_hours(now):
            wake = now.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)
            self.update_interval = _jittered(wake - now, UPDATE_JITTER)
            _LOGGER.debug("Quiet hours; next update in %s", self.update_interval)
            self.data.today = now.date()  # Keep "arriving today" right after 0h.
            return self.data

        try:
            data = await self._async_fetch(now)
        except AuthError as err:
            # Only this account stops; the others keep working. Polling
            # resumes when the account is reconfigured (which reloads).
            self.session = SESSION_EXPIRED
            self.update_interval = None
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="session_expired",
                translation_placeholders={"account": self.account_name},
            )
            if self.data is None:
                return MercadoLivreData(today=now.date(), fetched=False)
            raise UpdateFailed(f"Session expired: {err}") from err
        except BlockedError as err:
            self.session = SESSION_BLOCKED
            self._blocked_count += 1
            backoff = min(
                BACKOFF_BASE * 2 ** (self._blocked_count - 1), BACKOFF_MAX
            )
            self.update_interval = _jittered(backoff, UPDATE_JITTER)
            _LOGGER.warning(
                "Mercado Livre is limiting requests (%s); pausing for %s",
                err,
                self.update_interval,
            )
            if self.data is None:
                # Failing the first refresh would make Home Assistant retry
                # setup within seconds; start empty and wait out the backoff.
                return MercadoLivreData(today=now.date(), fetched=False)
            raise UpdateFailed(f"Requests blocked: {err}") from err
        except MercadoLivreError as err:
            self.session = SESSION_ERROR
            if self.data is None:
                return MercadoLivreData(today=now.date(), fetched=False)
            raise UpdateFailed(f"Error fetching purchases: {err}") from err

        self.session = SESSION_OK
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        self._blocked_count = 0
        self.update_interval = (
            _jittered(UPDATE_INTERVAL_OUT_FOR_DELIVERY, UPDATE_JITTER_OUT_FOR_DELIVERY)
            if any(p.status == STATUS_OUT_FOR_DELIVERY for p in data.pending)
            else _jittered(UPDATE_INTERVAL, UPDATE_JITTER)
        )
        return data

    async def _async_fetch(self, now: datetime) -> MercadoLivreData:
        listed = await self.client.async_get_packages()
        data = MercadoLivreData(today=now.date())
        previous = self.data.tracked if self.data else {}
        # The list names packs, not shipments.
        by_pack = {p.pack_id: p for p in previous.values() if p.pack_id}
        if self._restored_history is None:
            self._restored_history = _load_history(
                await self._history_store.async_load() or {}
            )

        for package in listed:
            package.account = self.account_name
            if package.pack_id in self._ignored_packs:
                continue
            if (retry := self._pack_retry_at.get(package.pack_id)) and now < retry:
                continue
            prev = by_pack.get(package.pack_id) or previous.get(package.shipment_id)
            if prev is not None:
                _carry_over(package, prev)
            elif package.past and not (
                package.bought_on
                and (data.today - package.bought_on).days <= DELIVERED_DAYS
            ):
                # Too old to have been delivered recently: not worth a read.
                continue

            if self._needs_status(package, prev, now):
                await self._async_update_status(package, prev, now)
            elif prev:
                _copy_tracking(package, prev)
                # The list didn't change, so the status page's texts still hold.
                package.intro = prev.intro
                package.headline = prev.headline
                package.description = prev.description

            if not package.shipment_id or package.shipment_id in data.tracked:
                # Not a shipment (a service, a digital purchase) or not yet
                # one, or its status page failed and it's tried again next time.
                if package.status_fetched_at:
                    if package.past:
                        self._ignored_packs.add(package.pack_id)
                    else:
                        self._pack_retry_at[package.pack_id] = now + STATUS_REFRESH
                continue
            self._pack_retry_at.pop(package.pack_id, None)

            delivered_at: datetime | None = None
            if package.status == STATUS_DELIVERED:
                # Delivered recently: keep its device, listed with the pending
                # ones. Headline is "Out 28 - 15h40".
                delivered_at = parse_delivered(package.headline or "", data.today)
                if delivered_at:
                    package.delivered_on = delivered_at.date()
                elif package.delivered_on is None and prev is not None:
                    # Seen being delivered, in a format not understood.
                    package.delivered_on = data.today
                if (
                    package.delivered_on is None
                    or (data.today - package.delivered_on).days > DELIVERED_DAYS
                ):
                    if prev is None:
                        self._ignored_packs.add(package.pack_id)
                    continue
                data.delivered.append(package)
            elif package.status in FINAL_STATES or package.past:
                # Cancelled, returned...: only kept briefly if it was being
                # tracked.
                if prev is None:
                    self._ignored_packs.add(package.pack_id)
                    continue
                package.final_since = prev.final_since or now
                if now - package.final_since > KEEP_FINAL_FOR:
                    continue

            if prev is None and (saved := self._restored_history.pop(
                package.shipment_id, None
            )):
                # First update after a restart.
                _merge_events(package, Package(package.shipment_id, events=saved))

            if package.status == STATUS_DELIVERED:
                _add_delivered_event(package, delivered_at)

            # The status page's headline is more precise than the list's
            # ("Chega hoje entre as 10:30 e 12:15 h" vs "Chegará hoje").
            _set_eta(package, data.today)
            await self._async_locate(package, prev)
            data.tracked[package.shipment_id] = package
            if prev is not None and prev.status != package.status:
                await self._async_record(
                    now, "status", package, prev, delivered_at=delivered_at
                )

        for shipment_id, prev in previous.items():
            if shipment_id not in data.tracked:
                await self._async_record(now, "removed", prev, prev)

        data.delivered.sort(key=lambda p: p.delivered_on or data.today, reverse=True)
        self._history_store.async_delay_save(
            lambda: _dump_history(data.tracked), HISTORY_SAVE_DELAY
        )
        return data

    async def _async_record(
        self,
        now: datetime,
        kind: str,
        package: Package,
        prev: Package,
        delivered_at: datetime | None = None,
    ) -> None:
        """Note a status change (or a package no longer followed) on disk.

        The texts are kept as Mercado Livre wrote them, to check later how
        they were read; one JSON per line in ``mercadolivre_debug/``, only
        with debug logging on (the file holds purchase details).
        """
        last = package.last_event
        record = {
            "time": now.isoformat(),
            "kind": kind,
            "account": self.account_name,
            "shipment_id": package.shipment_id,
            "pack_id": package.pack_id,
            "item": package.item_title,
            "from": prev.status,
            "to": package.status,
            "raw_status": package.raw_status,
            "sub_status": package.sub_status,
            "past": package.past,
            "intro": package.intro,
            "headline": package.headline,
            "description": package.description,
            "delivered_at": _iso(delivered_at),
            "delivered_on": _iso(package.delivered_on),
            "eta": [_iso(package.eta_start), _iso(package.eta_end)],
            "window": [_iso(package.eta_window_start), _iso(package.eta_window_end)],
            "last_event": [last.date_text, last.label] if last else None,
            "status_fetched_at": _iso(package.status_fetched_at),
        }
        _LOGGER.info(
            "Shipment %s (%s): %s %s -> %s, headline %r",
            package.shipment_id,
            package.item_title,
            kind,
            prev.status,
            package.status,
            package.headline,
        )
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        path = self.hass.config.path(DEBUG_DIR, DEBUG_TRANSITIONS_FILE)
        line = json.dumps(record, ensure_ascii=False)
        try:
            await self.hass.async_add_executor_job(_append_line, path, line)
        except OSError as err:
            _LOGGER.warning("Could not write %s: %s", path, err)

    @staticmethod
    def _needs_status(package: Package, prev: Package | None, now: datetime) -> bool:
        """Whether to read the (heavy) status page for this package.

        The list no longer gives a status, so it's always read for a new
        package (pending ones, and on startup the purchases of the last days).
        """
        if prev is None or prev.status_fetched_at is None:
            return True
        if prev.status in FINAL_STATES:
            # The final history was read when it got there.
            return False
        if package.past:
            # Left the "to receive" lists: read how it ended.
            return True
        if prev.list_signature != package.list_signature:
            return True
        return now - prev.status_fetched_at > STATUS_REFRESH

    async def _async_update_status(
        self, package: Package, previous: Package | None, now: datetime
    ) -> None:
        # Space requests out like someone clicking through the pages.
        await asyncio.sleep(random.uniform(*REQUEST_DELAY))
        try:
            await self.client.async_update_status(package)
        except (AuthError, BlockedError):
            raise
        except MercadoLivreError as err:
            # The list alone still gives a usable state; keep the old history.
            _LOGGER.warning(
                "Could not read tracking for shipment %s: %s", package.shipment_id, err
            )
            if previous:
                _copy_tracking(package, previous)
        else:
            package.status_fetched_at = now
            if previous:
                _merge_events(package, previous)
                if not package.steps:
                    # Once delivered the page has no steps at all.
                    package.steps = [
                        {**step, "state": "completed"}
                        if package.status == STATUS_DELIVERED
                        else step
                        for step in previous.steps
                    ]

    async def _async_locate(self, package: Package, previous: Package | None) -> None:
        """Geocode the current place and every place in the history."""
        known = {}
        if previous:
            known = {
                e.place: (e.latitude, e.longitude)
                for e in previous.events
                if e.place and e.latitude is not None
            }
        for event in package.events:
            if not event.place:
                continue
            coords = known.get(event.place) or await self.geocoder.async_lookup(
                event.place
            )
            if coords:
                event.latitude, event.longitude = coords
                known[event.place] = coords
        for event in reversed(package.events):
            if event.place:
                package.latitude, package.longitude = event.latitude, event.longitude
                break


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _append_line(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as file:
        file.write(line + "\n")


def _set_eta(package: Package, today: date) -> None:
    if package.status == STATUS_DELIVERED:
        if package.delivered_on:
            package.eta_start = package.eta_end = package.delivered_on
        return
    if eta := parse_eta(package.headline or "", today):
        package.eta_start, package.eta_end = eta
        package.eta_window_start, package.eta_window_end = parse_eta_window(
            package.headline or "", package.eta_start
        )


def _merge_events(package: Package, previous: Package) -> None:
    """Keep the history lines the status page stopped showing.

    On the last stretch ("Seu pacote está muito perto.") the page lists only
    the latest event, which would lose the places the package went through.
    """
    shown = {(e.label, e.date or e.date_text) for e in package.events}
    first = package.events[0].date if package.events else None
    older = [
        e
        for e in previous.events
        if (e.label, e.date or e.date_text) not in shown
        and (first is None or e.date is None or e.date <= first)
    ]
    package.events = older + package.events


# "Entregamos seu pacote às 11:01h na Rua ..."
_DELIVERED_AT_RE = re.compile(r"\bàs\s+(\d{1,2})[:h](\d{2})", re.I)
DELIVERED_EVENT = "Seu pacote foi entregue."


def _add_delivered_event(package: Package, when: datetime | None = None) -> None:
    """End the history with the delivery.

    Once delivered, the status page has no history at all, so the last line
    would stay "Seu pacote está muito perto.".
    """
    if not package.events or any(e.label == DELIVERED_EVENT for e in package.events):
        return
    day = package.delivered_on
    if (
        when is None
        and day
        and (match := _DELIVERED_AT_RE.search(package.description or ""))
    ):
        try:
            when = datetime(
                day.year, day.month, day.day,
                int(match.group(1)), int(match.group(2)), tzinfo=TZ,
            )  # fmt: skip
        except ValueError:
            pass
    package.events.append(
        TrackingEvent(
            step="Entrega",
            label=DELIVERED_EVENT,
            date_text=when.strftime("%H:%M") if when else "",
            date=when,
            place=None,
        )
    )


def _dump_history(tracked: dict[str, Package]) -> dict[str, list[dict[str, Any]]]:
    return {
        sid: [
            {
                "step": e.step,
                "label": e.label,
                "date_text": e.date_text,
                "date": e.date.isoformat() if e.date else None,
                "place": e.place,
                "latitude": e.latitude,
                "longitude": e.longitude,
            }
            for e in package.events
        ]
        for sid, package in tracked.items()
        if package.events
    }


def _load_history(
    saved: dict[str, list[dict[str, Any]]],
) -> dict[str, list[TrackingEvent]]:
    history: dict[str, list[TrackingEvent]] = {}
    for sid, events in saved.items():
        try:
            history[sid] = [
                TrackingEvent(
                    step=e["step"],
                    label=e["label"],
                    date_text=e["date_text"],
                    date=datetime.fromisoformat(e["date"]) if e["date"] else None,
                    place=e["place"],
                    latitude=e["latitude"],
                    longitude=e["longitude"],
                )
                for e in events
            ]
        except (KeyError, TypeError, ValueError):
            continue
    return history


def _carry_over(package: Package, previous: Package) -> None:
    """Keep what only the status page tells, for when it isn't re-read."""
    package.shipment_id = package.shipment_id or previous.shipment_id
    package.item_title = package.item_title or previous.item_title
    package.item_info = previous.item_info
    package.image = package.image or previous.image
    package.raw_status = previous.raw_status
    package.sub_status = previous.sub_status
    package.logistic_type = previous.logistic_type
    package.delivered_on = previous.delivered_on
    package.share_token = previous.share_token


def _copy_tracking(package: Package, previous: Package) -> None:
    package.steps = previous.steps
    package.events = previous.events
    package.status_fetched_at = previous.status_fetched_at
