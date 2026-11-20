"""Tests for TITSA parent ConfigFlow and stop ConfigSubentryFlow."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.titsa.const import (
    CONF_LINES,
    CONF_MANUAL_ONLY,
    CONF_SCHEDULE_ENTITY,
    CONF_SEARCH_AGAIN,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_STOP_QUERY,
    DOMAIN,
    SUBENTRY_TYPE_STOP,
)


async def test_parent_config_flow_and_single_entry(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """Parent entry creates once and aborts on a second attempt."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == "TITSA"
    await hass.async_block_till_done()

    # Second attempt is automatically aborted by single_config_entry: true
    result3 = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result3["type"] is FlowResultType.ABORT
    assert result3["reason"] == "single_instance_allowed"


async def test_stop_subentry_flow_direct_code(
    hass: HomeAssistant,
    mock_titsa_network,
    sample_stop_routes,
) -> None:
    """Add a stop directly by numeric stop code and configure lines + schedule."""
    entry = MockConfigEntry(domain=DOMAIN, title="TITSA", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Populate compact GTFS index on runtime catalog
    entry.runtime_data.catalog.data.stop_routes.update(sample_stop_routes)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP),
        context={"source": SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["last_step"] is False

    # Enter exact stop ID -> jumps directly to configure
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_STOP_QUERY: "1580"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "configure"
    assert result["last_step"] is True
    assert result["description_placeholders"]["stop_id"] == "1580"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Nuevo Obrero Casa",
            CONF_LINES: ["936", "238"],
            CONF_SCHEDULE_ENTITY: "schedule.mananas",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Nuevo Obrero Casa"
    assert result["unique_id"] == "1580"
    assert result["data"] == {
        CONF_STOP_ID: "1580",
        CONF_STOP_NAME: "Nuevo Obrero",
        CONF_LINES: ["238", "936"],
        CONF_MANUAL_ONLY: False,
        CONF_SCHEDULE_ENTITY: "schedule.mananas",
    }
    await hass.async_block_till_done()

    # Adding the exact same stop ID again aborts
    dup = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP),
        context={"source": SOURCE_USER},
    )
    dup = await hass.config_entries.subentries.async_configure(
        dup["flow_id"], {CONF_STOP_QUERY: "1580"}
    )
    assert dup["type"] is FlowResultType.ABORT
    assert dup["reason"] == "already_configured"


async def test_stop_subentry_flow_text_search_disambiguation_and_reconfigure(
    hass: HomeAssistant,
    mock_titsa_network,
    sample_stop_routes,
) -> None:
    """Text search matching two stops shows select_stop; reconfigure updates & clears schedule."""
    entry = MockConfigEntry(domain=DOMAIN, title="TITSA", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.catalog.data.stop_routes.update(sample_stop_routes)

    # Search text matching 1580 and 1581
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP),
        context={"source": SOURCE_USER},
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_STOP_QUERY: "nuevo obrero"}
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "select_stop"

    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_STOP_ID: "1580"}
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "configure"

    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"],
        {
            CONF_NAME: "Nuevo Obrero (1580)",
            CONF_LINES: ["238", "936"],
            CONF_SCHEDULE_ENTITY: "schedule.mananas",
        },
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    subentry = next(iter(entry.subentries.values()))

    # Reconfigure: change lines to ["238"] and clear schedule_entity
    reconf = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert reconf["type"] is FlowResultType.FORM
    assert reconf["step_id"] == "configure"

    reconf = await hass.config_entries.subentries.async_configure(
        reconf["flow_id"],
        {
            CONF_NAME: "Parada Trabajo",
            CONF_LINES: ["238"],
        },
    )
    assert reconf["type"] is FlowResultType.ABORT
    assert reconf["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    updated = entry.subentries[subentry.subentry_id]
    assert updated.title == "Parada Trabajo"
    assert updated.data[CONF_LINES] == ["238"]
    assert updated.data[CONF_SCHEDULE_ENTITY] is None


async def test_stop_subentry_flow_not_found(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """Unknown stop name shows stop_not_found error."""
    entry = MockConfigEntry(domain=DOMAIN, title="TITSA", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP),
        context={"source": SOURCE_USER},
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_STOP_QUERY: "parada inventada xyz"}
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "user"
    assert flow["errors"] == {CONF_STOP_QUERY: "stop_not_found"}


async def test_stop_subentry_flow_live_validation_when_catalog_empty(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """Numeric stop code falls back to live validation when stop is not in CSV cache."""
    mock_fetch, _ = mock_titsa_network
    entry = MockConfigEntry(domain=DOMAIN, title="TITSA", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Simulate empty CSV cache
    entry.runtime_data.catalog.data.stops.clear()

    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP),
        context={"source": SOURCE_USER},
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_STOP_QUERY: "1580"}
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "configure"
    assert mock_fetch.call_count == 1

    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"],
        {
            CONF_NAME: "Nuevo Obrero (1580)",
            CONF_LINES: [],
        },
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # Clean unload should succeed and cancel background tasks/listeners
    assert await hass.config_entries.async_unload(entry.entry_id) is True


async def test_stop_subentry_flow_aborts_when_entry_not_loaded(
    hass: HomeAssistant,
) -> None:
    """Subentry flow aborts gracefully if parent entry is not loaded."""
    entry = MockConfigEntry(domain=DOMAIN, title="TITSA", data={})
    entry.add_to_hass(hass)

    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP),
        context={"source": SOURCE_USER},
    )
    assert flow["type"] is FlowResultType.ABORT
    assert flow["reason"] == "entry_not_loaded"


async def test_stop_subentry_flow_search_again_from_select_and_configure(
    hass: HomeAssistant,
    mock_titsa_network,
    sample_stop_routes,
) -> None:
    """Enabling search_again returns cleanly to step user without creating a subentry."""
    entry = MockConfigEntry(domain=DOMAIN, title="TITSA", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.catalog.data.stop_routes.update(sample_stop_routes)

    # Start search matching two stops -> select_stop
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP),
        context={"source": SOURCE_USER},
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_STOP_QUERY: "nuevo obrero"}
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "select_stop"
    assert flow["last_step"] is False

    # Request new search from select_stop -> back to user
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_SEARCH_AGAIN: True}
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "user"

    # Search exact code 1580 -> configure, then go back to user via search_again
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_STOP_QUERY: "1580"}
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "configure"

    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"],
        {
            CONF_NAME: "No debe guardarse",
            CONF_LINES: [],
            CONF_SEARCH_AGAIN: True,
        },
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "user"
    assert len(entry.subentries) == 0


async def test_stop_subentry_flow_rejects_invalid_line_when_catalog_empty(
    hass: HomeAssistant,
    mock_titsa_network,
) -> None:
    """When Open Data catalog is empty (custom_value fallback), invalid line text is rejected."""
    entry = MockConfigEntry(domain=DOMAIN, title="TITSA", data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Empty catalog & empty live arrivals -> triggers custom_value=True with regex validation
    entry.runtime_data.catalog.data.lines.clear()
    entry.runtime_data.catalog.data.stop_routes.clear()
    mock_fetch, _ = mock_titsa_network
    mock_fetch.return_value = mock_fetch.return_value.__class__(
        stop_id="1580",
        stop_name="Nuevo Obrero",
        estimation_time=mock_fetch.return_value.estimation_time,
        arrivals=(),
    )

    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_STOP),
        context={"source": SOURCE_USER},
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_STOP_QUERY: "1580"}
    )
    assert flow["step_id"] == "configure"

    # Invalid arbitrary text in lines is rejected instead of creating a broken subentry
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"],
        {
            CONF_NAME: "Nuevo Obrero (1580)",
            CONF_LINES: ["texto invalido"],
        },
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "configure"
    assert flow["errors"] == {CONF_LINES: "invalid_line"}
