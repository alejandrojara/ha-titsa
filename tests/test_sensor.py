"""Tests for TITSA sensor entities (aggregated + per-filtered-line + reconfigure cleanup)."""

from __future__ import annotations

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.titsa.const import (
    CONF_LINES,
    CONF_SCHEDULE_ENTITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
    SUBENTRY_TYPE_STOP,
)


async def test_unfiltered_stop_creates_only_aggregated_sensor(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """When no line filter is selected, only the aggregated next_bus sensor is created."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TITSA",
        data={},
        subentries_data=[
            {
                "data": {
                    CONF_STOP_ID: "1580",
                    CONF_STOP_NAME: "Nuevo Obrero",
                    CONF_LINES: [],
                    CONF_SCHEDULE_ENTITY: None,
                },
                "subentry_type": SUBENTRY_TYPE_STOP,
                "title": "Nuevo Obrero (1580)",
                "unique_id": "1580",
            }
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    sensors = [
        e
        for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if e.domain == "sensor"
    ]
    assert len(sensors) == 1

    state = hass.states.get(sensors[0].entity_id)
    assert state is not None
    assert state.state == "2026-11-20T09:56:32+00:00"
    assert state.attributes["stop_id"] == "1580"
    assert state.attributes["stop_name"] == "Nuevo Obrero"
    assert state.attributes["next_line"] == "238"
    assert state.attributes["next_minutes"] == 0
    assert state.attributes["arrivals_count"] == 4
    assert len(state.attributes["arrivals"]) == 4
    assert state.attributes["polling_paused"] is False


async def test_filtered_stop_creates_per_line_sensors_and_cleans_up_on_reconfigure(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """Filtered stop creates aggregated + per-line sensors and removes orphaned lines on reload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TITSA",
        data={},
        subentries_data=[
            {
                "data": {
                    CONF_STOP_ID: "1580",
                    CONF_STOP_NAME: "Nuevo Obrero",
                    CONF_LINES: ["238", "936", "999"],
                    CONF_SCHEDULE_ENTITY: None,
                },
                "subentry_type": SUBENTRY_TYPE_STOP,
                "title": "Nuevo Obrero (1580)",
                "unique_id": "1580",
            }
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    sensors = [
        e
        for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if e.domain == "sensor"
    ]
    # 1 aggregated + 3 per-line sensors (238, 936, 999)
    assert len(sensors) == 4

    subentry = next(iter(entry.subentries.values()))
    line_936_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{subentry.subentry_id}_line_936"
    )
    line_999_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{subentry.subentry_id}_line_999"
    )
    assert line_936_id is not None
    assert line_999_id is not None

    state_936 = hass.states.get(line_936_id)
    assert state_936 is not None
    assert state_936.state == "2026-11-20T10:09:32+00:00"
    assert state_936.attributes["line"] == "936"
    assert state_936.attributes["minutes"] == 13

    # Line 999 has no arriving bus -> unknown (not unavailable)
    state_999 = hass.states.get(line_999_id)
    assert state_999 is not None
    assert state_999.state == STATE_UNKNOWN

    # Update subentry to keep only line 238 -> reload cleans up 936 and 999
    hass.config_entries.async_update_subentry(
        entry,
        subentry,
        data={
            CONF_STOP_ID: "1580",
            CONF_STOP_NAME: "Nuevo Obrero",
            CONF_LINES: ["238"],
            CONF_SCHEDULE_ENTITY: None,
        },
    )
    await hass.async_block_till_done()

    sensors_after = [
        e
        for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if e.domain == "sensor"
    ]
    assert len(sensors_after) == 2
    assert (
        entity_registry.async_get_entity_id("sensor", DOMAIN, f"{subentry.subentry_id}_line_936")
        is None
    )
    assert (
        entity_registry.async_get_entity_id("sensor", DOMAIN, f"{subentry.subentry_id}_line_238")
        is not None
    )
