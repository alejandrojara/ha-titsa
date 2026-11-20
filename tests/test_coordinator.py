"""Tests for TitsaStopCoordinator: schedule window, backoff, jitter, and manual refresh."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.titsa.api import TitsaRateLimitError
from custom_components.titsa.const import (
    CONF_LINES,
    CONF_SCHEDULE_ENTITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
    SUBENTRY_TYPE_STOP,
)


def _entry_with_stop(
    *,
    lines: list[str] | None = None,
    schedule_entity: str | None = None,
) -> MockConfigEntry:
    subentry = ConfigSubentry(
        data={
            CONF_STOP_ID: "1580",
            CONF_STOP_NAME: "Nuevo Obrero",
            CONF_LINES: lines or [],
            CONF_SCHEDULE_ENTITY: schedule_entity,
        },
        subentry_type=SUBENTRY_TYPE_STOP,
        title="Nuevo Obrero (1580)",
        unique_id="1580",
    )
    return MockConfigEntry(
        domain=DOMAIN,
        title="TITSA",
        data={},
        subentries_data=[
            {
                "data": dict(subentry.data),
                "subentry_type": subentry.subentry_type,
                "title": subentry.title,
                "unique_id": subentry.unique_id,
            }
        ],
    )


async def test_schedule_off_pauses_polling_with_zero_http_requests(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """When schedule entity is OFF, 0 HTTP requests are made and sensor is unknown + paused."""
    mock_fetch, _ = mock_titsa_network
    hass.states.async_set("schedule.mananas", STATE_OFF)

    entry = _entry_with_stop(lines=["238"], schedule_entity="schedule.mananas")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # ZERO HTTP requests while schedule is off
    assert mock_fetch.call_count == 0

    state = hass.states.get("sensor.nuevo_obrero_1580_next_bus")
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["arrivals"] == []
    assert state.attributes["polling_paused"] is True

    # Transition OFF -> ON triggers immediate refresh
    hass.states.async_set("schedule.mananas", STATE_ON)
    await hass.async_block_till_done()

    assert mock_fetch.call_count == 1
    state = hass.states.get("sensor.nuevo_obrero_1580_next_bus")
    assert state is not None
    assert state.state != STATE_UNKNOWN
    assert state.attributes["polling_paused"] is False
    assert state.attributes["next_line"] == "238"

    # Transition ON -> OFF immediately pauses and clears arrivals (0 extra HTTP calls)
    hass.states.async_set("schedule.mananas", STATE_OFF)
    await hass.async_block_till_done()

    assert mock_fetch.call_count == 1
    state = hass.states.get("sensor.nuevo_obrero_1580_next_bus")
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["arrivals"] == []
    assert state.attributes["polling_paused"] is True


async def test_manual_refresh_while_paused_and_cooldown(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """Manual refresh works once even when schedule is OFF, then enforces 30s cooldown."""
    mock_fetch, _ = mock_titsa_network
    hass.states.async_set("schedule.mananas", STATE_OFF)

    entry = _entry_with_stop(schedule_entity="schedule.mananas")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert mock_fetch.call_count == 0
    coordinator = next(iter(entry.runtime_data.coordinators.values()))

    # Manual one-shot refresh fetches live data even outside schedule window
    await coordinator.async_manual_refresh()
    assert mock_fetch.call_count == 1
    assert coordinator.data.manual_snapshot is True
    assert len(coordinator.data.filtered_arrivals) == 4

    # Immediate second press is blocked by cooldown
    with pytest.raises(HomeAssistantError):
        await coordinator.async_manual_refresh()
    assert mock_fetch.call_count == 1


async def test_coordinator_rate_limit_backoff(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """Rate limit error raises UpdateFailed with retry_after backoff."""
    mock_fetch, _ = mock_titsa_network

    entry = _entry_with_stop()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = next(iter(entry.runtime_data.coordinators.values()))
    mock_fetch.side_effect = TitsaRateLimitError("429", status=429, retry_after=240.0)

    with pytest.raises(UpdateFailed) as exc_info:
        await coordinator._async_update_data()

    assert exc_info.value.retry_after == 240.0
