"""The TITSA (Tenerife public bus transport) integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import TitsaOpenDataClient, TitsaRealtimeProvider, TitsaWebProvider
from .catalog import TitsaCatalogStore
from .const import (
    HTTP_TIMEOUT_SECONDS,
    MAX_CONCURRENT_REQUESTS,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_TYPE_STOP,
    USER_AGENT,
)
from .coordinator import TitsaStopCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]


@dataclass(slots=True)
class TitsaRuntimeData:
    """Runtime data stored on the parent TITSA config entry."""

    provider: TitsaRealtimeProvider
    catalog: TitsaCatalogStore
    coordinators: dict[str, TitsaStopCoordinator] = field(default_factory=dict)


type TitsaConfigEntry = ConfigEntry[TitsaRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: TitsaConfigEntry) -> bool:
    """Set up TITSA from a config entry and all configured stop subentries."""
    session = async_get_clientsession(hass)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    provider = TitsaWebProvider(
        session,
        semaphore=semaphore,
        timeout=HTTP_TIMEOUT_SECONDS,
        user_agent=USER_AGENT,
    )
    open_data_client = TitsaOpenDataClient(session, user_agent=USER_AGENT)
    catalog = TitsaCatalogStore(hass, open_data_client)

    # Load lightweight CSV catalog immediately (non-fatal if Open Data is temporarily down)
    await catalog.async_ensure_csv_fresh()
    # Build/refresh compact GTFS stop->routes index in background without blocking startup
    catalog.start_background_gtfs_refresh()

    coordinators: dict[str, TitsaStopCoordinator] = {}
    try:
        for subentry in entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_TYPE_STOP:
                continue
            coordinator = TitsaStopCoordinator(hass, entry, subentry, provider)
            coordinators[subentry.subentry_id] = coordinator

        if coordinators:
            await asyncio.gather(
                *(
                    coordinator.async_config_entry_first_refresh()
                    for coordinator in coordinators.values()
                )
            )
            for coordinator in coordinators.values():
                coordinator.async_setup_schedule_listener()
    except BaseException:
        catalog.cancel_background_tasks()
        for coordinator in coordinators.values():
            coordinator.async_cleanup()
        raise

    entry.runtime_data = TitsaRuntimeData(
        provider=provider,
        catalog=catalog,
        coordinators=coordinators,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TitsaConfigEntry) -> bool:
    """Unload a TITSA config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime_data = entry.runtime_data
    runtime_data.catalog.cancel_background_tasks()
    for coordinator in runtime_data.coordinators.values():
        coordinator.async_cleanup()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: TitsaConfigEntry) -> None:
    """Remove cached Open Data storage when the parent TITSA entry is deleted."""
    store: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await store.async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: TitsaConfigEntry) -> None:
    """Reload TITSA config entry when subentries are added, updated, or removed."""
    _LOGGER.debug("Reloading TITSA entry %s after subentry change", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
