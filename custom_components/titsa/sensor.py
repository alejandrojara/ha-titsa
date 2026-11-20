"""Sensor platform for TITSA (aggregated stop sensor + per-filtered-line sensors)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TitsaConfigEntry
from .api import BusArrival
from .const import MAX_ARRIVALS_ATTRIBUTE, MAX_LINE_FOLLOWING_ATTRIBUTE
from .coordinator import TitsaStopCoordinator
from .entity import TitsaStopEntity

PARALLEL_UPDATES = 0


def _cleanup_orphaned_line_sensors(
    hass: HomeAssistant,
    entry: TitsaConfigEntry,
    coordinator: TitsaStopCoordinator,
    active_unique_ids: set[str],
) -> None:
    """Remove stale per-line sensors when a stop subentry is reconfigured."""
    entity_registry = er.async_get(hass)
    line_prefix = f"{coordinator.subentry.subentry_id}_line_"
    for entity_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if (
            entity_entry.domain == Platform.SENSOR
            and entity_entry.config_subentry_id == coordinator.subentry.subentry_id
            and entity_entry.unique_id.startswith(line_prefix)
            and entity_entry.unique_id not in active_unique_ids
        ):
            entity_registry.async_remove(entity_entry.entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TitsaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up TITSA sensor entities for every configured stop subentry."""
    for subentry_id, coordinator in entry.runtime_data.coordinators.items():
        entities: list[SensorEntity] = [TitsaNextBusSensor(coordinator)]
        active_unique_ids: set[str] = {f"{subentry_id}_next_bus"}

        # Create 1 individual sensor per selected line (if line filter is configured)
        for line_code in coordinator.selected_lines:
            line_sensor = TitsaLineNextBusSensor(coordinator, line_code)
            entities.append(line_sensor)
            if line_sensor.unique_id:
                active_unique_ids.add(line_sensor.unique_id)

        _cleanup_orphaned_line_sensors(hass, entry, coordinator, active_unique_ids)
        async_add_entities(entities, config_subentry_id=subentry_id)


class TitsaNextBusSensor(TitsaStopEntity, SensorEntity):
    """Aggregated next-bus timestamp sensor for a TITSA stop."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:bus-clock"
    _attr_translation_key = "next_bus"
    _unrecorded_attributes = frozenset(
        {"arrivals", "filtered_lines", "last_estimation", "next_line_name"}
    )

    def __init__(self, coordinator: TitsaStopCoordinator) -> None:
        """Initialize the aggregated stop sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.subentry.subentry_id}_next_bus"

    @property
    def _next_arrival(self) -> BusArrival | None:
        data = self.coordinator.data
        if not data or not data.filtered_arrivals:
            return None
        return data.filtered_arrivals[0]

    @property
    def native_value(self) -> datetime | None:
        """Return estimated arrival timestamp of the next matching bus, or None (unknown)."""
        arrival = self._next_arrival
        return arrival.estimated_arrival if arrival else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return bounded arrival attributes for native cards and ha-titsa-card."""
        data = self.coordinator.data
        if data is None:
            return {
                "stop_id": self.coordinator.stop_id,
                "stop_name": self.coordinator.stop_name,
                "filtered_lines": list(self.coordinator.selected_lines),
                "next_line": None,
                "next_line_name": None,
                "next_destination": None,
                "next_minutes": None,
                "arrivals_count": 0,
                "arrivals": [],
                "last_estimation": None,
                "polling_paused": False,
                "schedule_entity": self.coordinator.schedule_entity,
            }

        next_bus = self._next_arrival
        bounded_arrivals = [
            arrival.to_dict() for arrival in data.filtered_arrivals[:MAX_ARRIVALS_ATTRIBUTE]
        ]
        return {
            "stop_id": data.stop_id,
            "stop_name": data.stop_name,
            "filtered_lines": list(self.coordinator.selected_lines),
            "next_line": next_bus.line if next_bus else None,
            "next_line_name": next_bus.line_name if next_bus else None,
            "next_destination": next_bus.destination if next_bus else None,
            "next_minutes": next_bus.minutes if next_bus else None,
            "arrivals_count": len(data.filtered_arrivals),
            "arrivals": bounded_arrivals,
            "last_estimation": (data.estimation_time.isoformat() if data.estimation_time else None),
            "polling_paused": data.polling_paused,
            "schedule_entity": self.coordinator.schedule_entity,
        }


class TitsaLineNextBusSensor(TitsaStopEntity, SensorEntity):
    """Per-line next-bus timestamp sensor for a filtered line at a TITSA stop."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:bus"
    _attr_translation_key = "line_next_bus"
    _unrecorded_attributes = frozenset({"following_arrivals", "last_estimation", "line_name"})

    def __init__(self, coordinator: TitsaStopCoordinator, line_code: str) -> None:
        """Initialize the per-line sensor."""
        super().__init__(coordinator)
        self._line_code = line_code
        self._attr_unique_id = f"{coordinator.subentry.subentry_id}_line_{line_code}"
        self._attr_translation_placeholders = {"line": line_code}

    @property
    def _matching_arrivals(self) -> list[BusArrival]:
        data = self.coordinator.data
        if not data or not data.filtered_arrivals:
            return []
        return [a for a in data.filtered_arrivals if a.line == self._line_code]

    @property
    def native_value(self) -> datetime | None:
        """Return estimated arrival timestamp of the next bus on this line, or None."""
        matches = self._matching_arrivals
        return matches[0].estimated_arrival if matches else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return line-specific arrival metadata and subsequent departures."""
        data = self.coordinator.data
        matches = self._matching_arrivals
        first = matches[0] if matches else None
        following = [
            {
                "destination": arrival.destination,
                "minutes": arrival.minutes,
                "estimated_arrival": arrival.estimated_arrival.isoformat(),
                "is_approximate": arrival.is_approximate,
            }
            for arrival in matches[1 : 1 + MAX_LINE_FOLLOWING_ATTRIBUTE]
        ]

        return {
            "stop_id": data.stop_id if data else self.coordinator.stop_id,
            "stop_name": data.stop_name if data else self.coordinator.stop_name,
            "line": self._line_code,
            "line_name": first.line_name if first else None,
            "destination": first.destination if first else None,
            "minutes": first.minutes if first else None,
            "following_arrivals": following,
            "last_estimation": (
                data.estimation_time.isoformat() if data and data.estimation_time else None
            ),
            "polling_paused": data.polling_paused if data else False,
        }
