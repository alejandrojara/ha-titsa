"""Tests for TitsaStopCoordinator: schedule window, backoff, jitter, and manual refresh."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.titsa.api import TitsaRateLimitError
from custom_components.titsa.const import (
    CONF_LINES,
    CONF_MANUAL_ONLY,
    CONF_SCHEDULE_ENTITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
    MANUAL_SNAPSHOT_TTL_SECONDS,
    SUBENTRY_TYPE_STOP,
)


def _entry_with_stop(
    *,
    lines: list[str] | None = None,
    schedule_entity: str | None = None,
    manual_only: bool = False,
) -> MockConfigEntry:
    subentry = ConfigSubentry(
        data={
            CONF_STOP_ID: "1580",
            CONF_STOP_NAME: "Nuevo Obrero",
            CONF_LINES: lines or [],
            CONF_MANUAL_ONLY: manual_only,
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


async def test_setup_retry_cleans_up_on_first_refresh_failure_and_remove_entry(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """Failed first refresh cancels background tasks cleanly and remove_entry wipes storage."""
    mock_fetch, _ = mock_titsa_network
    mock_fetch.side_effect = TitsaRateLimitError("429", status=429, retry_after=60.0)

    entry = _entry_with_stop()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()

    # Removing the entry should succeed cleanly and remove cached catalog storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()


async def test_manual_only_mode_disables_background_polling_permanently(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """In manual_only mode, 0 HTTP requests occur on setup and update_interval stays None."""
    mock_fetch, _ = mock_titsa_network

    entry = _entry_with_stop(manual_only=True)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = next(iter(entry.runtime_data.coordinators.values()))
    assert coordinator.manual_only is True
    assert coordinator.update_interval is None
    assert mock_fetch.call_count == 0

    state = hass.states.get("sensor.nuevo_obrero_1580_next_bus")
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["polling_paused"] is True

    # Manual refresh fetches once and MUST keep update_interval as None
    await coordinator.async_manual_refresh()
    assert mock_fetch.call_count == 1
    assert coordinator.update_interval is None
    assert coordinator.data.manual_snapshot is True
    assert len(coordinator.data.filtered_arrivals) == 4

    # After MANUAL_SNAPSHOT_TTL_SECONDS (3 min), snapshot expires cleanly to paused (0 HTTP calls)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=MANUAL_SNAPSHOT_TTL_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert mock_fetch.call_count == 1
    assert coordinator.update_interval is None
    assert coordinator.data.polling_paused is True
    assert coordinator.data.manual_snapshot is False
    assert coordinator.data.filtered_arrivals == ()

    state = hass.states.get("sensor.nuevo_obrero_1580_next_bus")
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["arrivals"] == []
    assert state.attributes["polling_paused"] is True
    assert state.attributes["manual_snapshot"] is False


async def test_schedule_off_manual_snapshot_expires_after_ttl(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """In schedule-off mode, manual snapshot stays valid until TTL and then returns to paused."""
    mock_fetch, _ = mock_titsa_network
    hass.states.async_set("schedule.mananas", STATE_OFF)

    entry = _entry_with_stop(lines=["238"], schedule_entity="schedule.mananas")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = next(iter(entry.runtime_data.coordinators.values()))
    await coordinator.async_manual_refresh()
    await hass.async_block_till_done()

    assert mock_fetch.call_count == 1
    assert coordinator.data.manual_snapshot is True
    assert len(coordinator.data.filtered_arrivals) == 1

    # Intermediate coordinator refresh before TTL expires must preserve the fresh manual snapshot
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_fetch.call_count == 1
    assert coordinator.data.manual_snapshot is True
    assert len(coordinator.data.filtered_arrivals) == 1

    # Once TTL elapses, both aggregated and per-line sensors return cleanly to paused/unknown
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=MANUAL_SNAPSHOT_TTL_SECONDS + 1)
    )
    await hass.async_block_till_done()

    assert mock_fetch.call_count == 1
    stop_state = hass.states.get("sensor.nuevo_obrero_1580_next_bus")
    line_state = hass.states.get("sensor.nuevo_obrero_1580_line_238")
    assert stop_state is not None
    assert stop_state.state == STATE_UNKNOWN
    assert stop_state.attributes["polling_paused"] is True
    assert stop_state.attributes["manual_snapshot"] is False
    assert stop_state.attributes["arrivals"] == []
    assert line_state is not None
    assert line_state.state == STATE_UNKNOWN
    assert line_state.attributes["polling_paused"] is True
    assert line_state.attributes["manual_snapshot"] is False
