"""Tests for the TITSA manual refresh button platform."""

from __future__ import annotations

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.titsa.const import (
    CONF_LINES,
    CONF_SCHEDULE_ENTITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
    SUBENTRY_TYPE_STOP,
)


async def test_refresh_button_triggers_update_and_enforces_cooldown(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """Pressing the refresh button refreshes arrivals and blocks immediate repeat presses."""
    mock_fetch, _ = mock_titsa_network
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

    assert mock_fetch.call_count == 1
    subentry = next(iter(entry.subentries.values()))
    entity_registry = er.async_get(hass)
    button_id = entity_registry.async_get_entity_id(
        BUTTON_DOMAIN, DOMAIN, f"{subentry.subentry_id}_refresh"
    )
    assert button_id is not None

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: button_id},
        blocking=True,
    )
    assert mock_fetch.call_count == 2

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: button_id},
            blocking=True,
        )
    assert mock_fetch.call_count == 2
