"""Package position on the map.

City of the last distribution center, the driver's live position while a
shared tracking link is being followed, or home once delivered. The state is
the usual zone one, translated as "on the way" / "at home".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.location import distance

from .api import DriverPosition
from .const import DRIVER_ACCURACY, KIND_LOCATION, STATUS_DELIVERED
from .coordinator import AccountCoordinator, MercadoLivreConfigEntry
from .entity import PackageEntity, add_package_entities

# Distribution center positions are only city-level.
_CITY_ACCURACY = 5000


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MercadoLivreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up package trackers."""

    def _factory(coordinator: AccountCoordinator, sid: str) -> list[Entity]:
        return [PackageTracker(coordinator, sid, KIND_LOCATION)]

    add_package_entities(entry, async_add_entities, _factory)


class PackageTracker(PackageEntity, TrackerEntity):
    """Where the package is: with the driver, or where it was last seen."""

    _attr_icon = "mdi:package-variant-closed"
    # Trackers default to diagnostic; here the position is a main feature.
    _attr_entity_category = None

    async def async_added_to_hass(self) -> None:
        """Also follow the driver updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.config_entry.runtime_data.driver.async_add_listener(
                self.shipment_id, self.async_write_ha_state
            )
        )

    @property
    def _driver(self) -> DriverPosition | None:
        return self.coordinator.config_entry.runtime_data.driver.positions.get(
            self.shipment_id
        )

    @property
    def source_type(self) -> SourceType:
        """GPS-like coordinates."""
        return SourceType.GPS

    @property
    def _delivered(self) -> bool:
        return self.package is not None and self.package.status == STATUS_DELIVERED

    @property
    def latitude(self) -> float | None:
        """Latitude."""
        if self._delivered:
            return self.hass.config.latitude
        if driver := self._driver:
            return driver.latitude
        return self.package.latitude if self.package else None

    @property
    def longitude(self) -> float | None:
        """Longitude."""
        if self._delivered:
            return self.hass.config.longitude
        if driver := self._driver:
            return driver.longitude
        return self.package.longitude if self.package else None

    @property
    def location_accuracy(self) -> float:
        """Precision, in meters.

        With the driver, the radius Mercado Livre draws around an approximate
        position. A large one may put the package "home" while still far
        away; automations should use distance_to_home_m.
        """
        if self._delivered:
            return DRIVER_ACCURACY
        if driver := self._driver:
            return driver.accuracy if driver.accuracy is not None else DRIVER_ACCURACY
        return _CITY_ACCURACY

    @property
    def icon(self) -> str:
        """Truck while with the driver."""
        if self._delivered:
            return "mdi:package-variant-closed-check"
        return "mdi:truck-fast" if self._driver else "mdi:package-variant-closed"

    @property
    def entity_picture(self) -> str | None:
        """Product image, shown as the map marker."""
        return self.package.image if self.package else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Where the position comes from; distance to home with the driver."""
        if self._delivered:
            return {"source": "delivered"}
        driver = self._driver
        if driver is None or driver.latitude is None or driver.longitude is None:
            return {"source": "distribution_center"}
        updated = self.coordinator.config_entry.runtime_data.driver.updated_at.get(
            self.shipment_id
        )
        to_home = distance(
            self.hass.config.latitude,
            self.hass.config.longitude,
            driver.latitude,
            driver.longitude,
        )
        return {
            "source": "driver",
            "distance_to_home_m": round(to_home) if to_home is not None else None,
            "low_accuracy": driver.low_accuracy,
            "updated_at": updated.isoformat() if updated else None,
        }
