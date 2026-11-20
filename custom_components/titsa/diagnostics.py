"""Diagnostics support for TITSA."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import TitsaConfigEntry
from .api import TitsaWebProvider


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: TitsaConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for the TITSA parent config entry and all stop subentries."""
    runtime_data = entry.runtime_data
    ssl_fallback = (
        runtime_data.provider.ssl_fallback_active
        if isinstance(runtime_data.provider, TitsaWebProvider)
        else False
    )
    return {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "pref_disable_polling": entry.pref_disable_polling,
            "subentries_count": len(entry.subentries),
        },
        "provider": {
            "type": type(runtime_data.provider).__name__,
            "ssl_fallback_active": ssl_fallback,
        },
        "catalog": runtime_data.catalog.get_diagnostics(),
        "stops": [
            coordinator.get_diagnostics() for coordinator in runtime_data.coordinators.values()
        ],
    }
