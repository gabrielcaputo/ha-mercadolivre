"""Sensors for Mercado Livre packages."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Package
from .const import KIND_ETA, KIND_PLACE, KIND_STATUS, KIND_TRACKING, PACKAGE_STATES
from .coordinator import SESSION_STATES, AccountCoordinator, MercadoLivreConfigEntry
from .entity import AccountEntity, PackageEntity, SummaryEntity, add_package_entities

_STATUS_ICONS = {
    "out_for_delivery": "mdi:truck-fast",
    "shipped": "mdi:truck-delivery",
    "delivered": "mdi:package-variant-closed-check",
    "not_delivered": "mdi:package-variant-closed-remove",
    "cancelled": "mdi:cancel",
}


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _summary(package: Package) -> dict[str, Any]:
    last = package.last_event
    return {
        "item": package.item_title,
        "item_info": package.item_info,
        "products": package.products,
        "account": package.account,
        "status": package.status,
        "headline": package.headline,
        "eta": _iso(package.eta_end),
        "window_start": _iso(package.eta_window_start),
        "window_end": _iso(package.eta_window_end),
        "location": package.location,
        "last_event": last.label if last else None,
        "image": package.image,
        "url": package.status_url,
        "history": [
            {
                "date": e.date.isoformat() if e.date else e.date_text,
                "event": e.label,
                "place": e.place,
            }
            for e in package.events
        ],
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MercadoLivreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            ToReceiveSensor(entry, "to_receive"),
            ToReceiveTodaySensor(entry, "to_receive_today"),
            DeliveredRecentlySensor(entry, "delivered_recently"),
        ],
        config_subentry_id=runtime.summary_subentry_id,
    )
    for subentry_id, coordinator in runtime.accounts.items():
        async_add_entities(
            [
                SessionSensor(coordinator, "session"),
                AccountToReceiveSensor(coordinator, "account_to_receive"),
            ],
            config_subentry_id=subentry_id,
        )

    def _factory(coordinator: AccountCoordinator, sid: str) -> list[Entity]:
        return [
            PackageStatusSensor(coordinator, sid, KIND_STATUS),
            PackageEtaSensor(coordinator, sid, KIND_ETA),
            PackageTrackingSensor(coordinator, sid, KIND_TRACKING),
            PackagePlaceSensor(coordinator, sid, KIND_PLACE),
        ]

    add_package_entities(entry, async_add_entities, _factory)


# Summary sensors (every account)


class ToReceiveSensor(SummaryEntity, SensorEntity):
    """Number of packages still to be received."""

    _attr_icon = "mdi:package-variant-closed"

    def _packages(self) -> list[Package]:
        return [p for d in self.datas for p in d.pending]

    @property
    def native_value(self) -> int:
        """Pending packages."""
        return len(self._packages())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The pending packages."""
        return {"packages": [_summary(p) for p in self._packages()]}


class ToReceiveTodaySensor(ToReceiveSensor):
    """Number of packages expected today."""

    _attr_icon = "mdi:truck-fast"

    def _packages(self) -> list[Package]:
        return [p for d in self.datas for p in d.arriving_today]


class DeliveredRecentlySensor(SummaryEntity, SensorEntity):
    """Packages delivered in the last days."""

    _attr_icon = "mdi:package-variant-closed-check"

    def _packages(self) -> list[Package]:
        return sorted(
            (p for d in self.datas for p in d.delivered),
            key=lambda p: p.delivered_on or date.min,
            reverse=True,
        )

    @property
    def native_value(self) -> int:
        """Packages delivered recently."""
        return len(self._packages())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The delivered packages, newest first."""
        return {
            "packages": [
                {
                    "item": p.item_title,
                    "item_info": p.item_info,
                    "products": p.products,
                    "account": p.account,
                    "delivered_on": _iso(p.delivered_on),
                    "image": p.image,
                    "url": p.status_url,
                }
                for p in self._packages()
            ]
        }


# Account sensors


class SessionSensor(AccountEntity, SensorEntity):
    """Whether the account's session works."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SESSION_STATES
    _attr_icon = "mdi:account-key"

    @property
    def available(self) -> bool:
        """Always available: it reports the failures itself."""
        return True

    @property
    def native_value(self) -> str:
        """Session state."""
        return self.coordinator.session


class AccountToReceiveSensor(AccountEntity, SensorEntity):
    """Packages still to be received on this account."""

    _attr_icon = "mdi:package-variant-closed"

    @property
    def native_value(self) -> int | None:
        """Pending packages."""
        data = self.coordinator.data
        return len(data.pending) if data else None


# Package sensors


class PackageStatusSensor(PackageEntity, SensorEntity):
    """Delivery status."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PACKAGE_STATES

    @property
    def native_value(self) -> str | None:
        """Normalized status."""
        return self.package.status if self.package else None

    @property
    def icon(self) -> str:
        """Icon by status."""
        return _STATUS_ICONS.get(self.native_value or "", "mdi:package-variant")

    @property
    def entity_picture(self) -> str | None:
        """Product image."""
        return self.package.image if self.package else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Status details."""
        package = self.package
        if package is None:
            return {}
        return {
            "item": package.item_title,
            "account": package.account,
            "item_info": package.item_info,
            "products": package.products,
            "seller": package.seller,
            "intro": package.intro,
            "headline": package.headline,
            "description": package.description,
            "steps": package.steps,
            "raw_status": package.raw_status,
            "sub_status": package.sub_status,
            "logistic_type": package.logistic_type,
            "order_id": package.order_id,
            "shipment_id": package.shipment_id,
            "url": package.status_url,
        }


class PackageEtaSensor(PackageEntity, SensorEntity):
    """Expected delivery date (latest day of the promised window)."""

    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-clock"

    @property
    def native_value(self) -> date | None:
        """Expected (or actual) delivery date."""
        package = self.package
        if package is None:
            return None
        return package.delivered_on or package.eta_end

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The promise as shown by Mercado Livre."""
        package = self.package
        if package is None:
            return {}
        return {
            "text": package.headline,
            "earliest": _iso(package.eta_start),
            "latest": _iso(package.eta_end),
            "window_start": _iso(package.eta_window_start),
            "window_end": _iso(package.eta_window_end),
        }


class PackageTrackingSensor(PackageEntity, SensorEntity):
    """Latest tracking event, with the full history as an attribute."""

    _attr_icon = "mdi:timeline-text"

    @property
    def native_value(self) -> str | None:
        """Latest event (truncated to the state length limit)."""
        last = self.package.last_event if self.package else None
        return last.label[:255] if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Every event with its date and place."""
        package = self.package
        if package is None:
            return {}
        last = package.last_event
        return {
            "last_event_time": last.date.isoformat() if last and last.date else None,
            "history": [
                {
                    "date": e.date.isoformat() if e.date else e.date_text,
                    "step": e.step,
                    "event": e.label,
                    "place": e.place,
                    "latitude": e.latitude,
                    "longitude": e.longitude,
                }
                for e in package.events
            ],
        }


class PackagePlaceSensor(PackageEntity, SensorEntity):
    """Last place the package was seen."""

    _attr_icon = "mdi:map-marker"

    @property
    def native_value(self) -> str | None:
        """Place name, e.g. "Cajamar"."""
        return self.package.location if self.package else None

    @property
    def extra_state_attributes(self) -> dict[str, float | None]:
        """Coordinates of the place."""
        package = self.package
        return {
            "latitude": package.latitude if package else None,
            "longitude": package.longitude if package else None,
        }
