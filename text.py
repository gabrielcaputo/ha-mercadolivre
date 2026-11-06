"""Where the user pastes the app's shared tracking link."""

from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MercadoLivreConfigEntry
from .entity import SummaryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MercadoLivreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the shared link input."""
    async_add_entities(
        [SharedLinkText(entry, "shared_link")],
        config_subentry_id=entry.runtime_data.summary_subentry_id,
    )


class SharedLinkText(SummaryEntity, TextEntity):
    """Paste a shared tracking link to follow the driver on the map.

    Only needed for a link shared from the app: the one on the status page is
    picked up by itself.

    The value is consumed, not kept: the link shows the user's address to
    anyone who has it, so it never becomes a state in the recorder.
    """

    _attr_icon = "mdi:map-marker-path"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = 255
    _attr_native_value = ""

    async def async_set_value(self, value: str) -> None:
        """Start following the driver of the link's package."""
        if value.strip():
            await self.entry.runtime_data.driver.async_add_link(value)
        self.async_write_ha_state()
