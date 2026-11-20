"""Diagnostics support for TITSA."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import TitsaConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: TitsaConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for the TITSA parent config entry and all stop subentries."""
    runtime_data = entry.runtime_data
    return {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "pref_disable_polling": entry.pref_disable_polling,
            "subentries_count": len(entry.subentries),
        },
        "catalog": runtime_data.catalog.get_diagnostics(),
        "stops": [
            coordinator.get_diagnostics() for coordinator in runtime_data.coordinators.values()
        ],
    }
