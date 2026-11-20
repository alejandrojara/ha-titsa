"""Base entity for TITSA stop subentries."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, STOP_WEB_URL_TEMPLATE
from .coordinator import TitsaStopCoordinator


class TitsaStopEntity(CoordinatorEntity[TitsaStopCoordinator]):
    """Base entity representing a TITSA bus stop device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TitsaStopCoordinator) -> None:
        """Initialize the base TITSA stop entity."""
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.subentry.subentry_id)},
            name=coordinator.subentry.title or coordinator.stop_name,
            manufacturer="TITSA",
            model="TITSA Bus Stop",
            serial_number=coordinator.stop_id,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=STOP_WEB_URL_TEMPLATE.format(stop_id=coordinator.stop_id),
        )
