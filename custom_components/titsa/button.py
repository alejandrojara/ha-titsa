"""Button platform for TITSA on-demand stop refresh."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TitsaConfigEntry
from .coordinator import TitsaStopCoordinator
from .entity import TitsaStopEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TitsaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up TITSA manual refresh buttons for every configured stop subentry."""
    for subentry_id, coordinator in entry.runtime_data.coordinators.items():
        async_add_entities(
            [TitsaRefreshButton(coordinator)],
            config_subentry_id=subentry_id,
        )


class TitsaRefreshButton(TitsaStopEntity, ButtonEntity):
    """Button triggering a cooldown-protected one-shot arrival refresh."""

    _attr_icon = "mdi:refresh"
    _attr_translation_key = "refresh"

    def __init__(self, coordinator: TitsaStopCoordinator) -> None:
        """Initialize the refresh button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.subentry.subentry_id}_refresh"

    async def async_press(self) -> None:
        """Handle manual refresh button press."""
        await self.coordinator.async_manual_refresh()
