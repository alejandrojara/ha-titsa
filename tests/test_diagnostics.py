"""Tests for TITSA diagnostics."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.titsa.const import (
    CONF_LINES,
    CONF_SCHEDULE_ENTITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
    SUBENTRY_TYPE_STOP,
)
from custom_components.titsa.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_output(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """Diagnostics report catalog stats and per-stop coordinator state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TITSA",
        data={},
        subentries_data=[
            {
                "data": {
                    CONF_STOP_ID: "1580",
                    CONF_STOP_NAME: "Nuevo Obrero",
                    CONF_LINES: ["238"],
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

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["subentries_count"] == 1
    assert diag["catalog"]["stops_count"] == 6
    assert diag["catalog"]["lines_count"] == 6
    assert len(diag["stops"]) == 1
    assert diag["stops"][0]["stop_id"] == "1580"
    assert diag["stops"][0]["selected_lines"] == ["238"]
    assert diag["stops"][0]["filtered_arrivals_count"] == 1
    assert diag["stops"][0]["last_update_success"] is True
