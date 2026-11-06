"""Base entities for Mercado Livre."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Package
from .const import DOMAIN
from .coordinator import AccountCoordinator, MercadoLivreConfigEntry, MercadoLivreData

PURCHASES_URL = "https://myaccount.mercadolivre.com.br/my_purchases/list"

SUMMARY_IDENTIFIER = (DOMAIN, "summary")


def account_identifier(user_id: str) -> tuple[str, str]:
    """Device registry identifier of an account device."""
    return (DOMAIN, f"account_{user_id}")


def package_identifier(user_id: str, shipment_id: str) -> tuple[str, str]:
    """Device registry identifier of a package device."""
    return (DOMAIN, f"shipment_{user_id}_{shipment_id}")


def summary_device_info() -> DeviceInfo:
    """Device with the totals of every account."""
    return DeviceInfo(
        identifiers={SUMMARY_IDENTIFIER},
        name="Mercado Livre",
        manufacturer="Mercado Livre",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=PURCHASES_URL,
    )


def account_device_info(coordinator: AccountCoordinator) -> DeviceInfo:
    """Device representing one Mercado Livre account."""
    return DeviceInfo(
        identifiers={account_identifier(coordinator.user_id)},
        name=coordinator.account_name,
        manufacturer="Mercado Livre",
        model="Conta",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=PURCHASES_URL,
    )


class SummaryEntity(Entity):
    """An entity combining every account; updates when any account does."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: MercadoLivreConfigEntry, key: str) -> None:
        """Initialize."""
        self.entry = entry
        self._attr_translation_key = key
        self._attr_unique_id = f"summary_{key}"
        self._attr_device_info = summary_device_info()

    @property
    def accounts(self) -> list[AccountCoordinator]:
        """All account coordinators."""
        return list(self.entry.runtime_data.accounts.values())

    @property
    def datas(self) -> list[MercadoLivreData]:
        """Latest data of every account that has some."""
        return [c.data for c in self.accounts if c.data is not None]

    async def async_added_to_hass(self) -> None:
        """Follow every account."""
        for coordinator in self.accounts:
            self.async_on_remove(
                coordinator.async_add_listener(self.async_write_ha_state)
            )


class AccountEntity(CoordinatorEntity[AccountCoordinator]):
    """An entity of one account's device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AccountCoordinator, key: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.user_id}_{key}"
        self._attr_device_info = account_device_info(coordinator)

    @property
    def available(self) -> bool:
        """Keep the last known data through failed updates."""
        return self.coordinator.data is not None


class PackageEntity(CoordinatorEntity[AccountCoordinator]):
    """An entity of one package's device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: AccountCoordinator, shipment_id: str, key: str
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.shipment_id = shipment_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{shipment_id}_{key}"
        package = coordinator.data.tracked[shipment_id]
        self._attr_device_info = DeviceInfo(
            identifiers={package_identifier(coordinator.user_id, shipment_id)},
            name=package.item_title or f"Envio {shipment_id}",
            manufacturer="Mercado Livre",
            model=package.item_info,
            serial_number=shipment_id,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=package.status_url,
            via_device_id=coordinator.account_device_id,
        )

    @property
    def package(self) -> Package | None:
        """Current data for this package."""
        return self.coordinator.data.tracked.get(self.shipment_id)

    @property
    def available(self) -> bool:
        """Available while the package is tracked.

        A failed update keeps the last known data instead of making every
        package unavailable for hours of backoff; the session sensor reports
        the failure.
        """
        return self.package is not None


def add_package_entities(
    entry: MercadoLivreConfigEntry,
    async_add_entities: Callable[..., None],
    factory: Callable[[AccountCoordinator, str], list[Entity]],
) -> None:
    """Add entities for every account's packages, now and as they appear."""
    runtime = entry.runtime_data
    for coordinator in runtime.accounts.values():
        known: set[str] = set()

        @callback
        def _add_new(coordinator: AccountCoordinator = coordinator, known: set[str] = known) -> None:
            tracked = coordinator.data.tracked if coordinator.data else {}
            new = [sid for sid in tracked if sid not in known]
            known.intersection_update(tracked)
            if not new:
                return
            known.update(new)
            async_add_entities(
                [e for sid in new for e in factory(coordinator, sid)],
                config_subentry_id=runtime.deliveries_subentry_id,
            )

        _add_new()
        entry.async_on_unload(coordinator.async_add_listener(_add_new))


@callback
def async_remove_untracked_devices(
    hass: HomeAssistant, entry: MercadoLivreConfigEntry
) -> None:
    """Remove devices of packages no longer tracked and of removed accounts.

    Accounts without fresh data (session expired, blocked) keep their
    packages until they fetch again.
    """
    runtime = entry.runtime_data
    keep = {SUMMARY_IDENTIFIER}
    keep_prefixes: list[str] = []
    for coordinator in runtime.accounts.values():
        keep.add(account_identifier(coordinator.user_id))
        data = coordinator.data
        if data is None or not data.fetched:
            keep_prefixes.append(f"shipment_{coordinator.user_id}_")
            continue
        keep.update(
            package_identifier(coordinator.user_id, sid) for sid in data.tracked
        )
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if device.identifiers & keep:
            continue
        if any(
            ident.startswith(prefix)
            for _, ident in device.identifiers
            for prefix in keep_prefixes
        ):
            continue
        registry.async_remove_device(device.id)
