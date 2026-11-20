"""Persistent Open Data catalog cache (stops CSV, lines CSV, and compact GTFS index)."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
import logging
from pathlib import Path
import tempfile
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import (
    CatalogData,
    LineInfo,
    StopInfo,
    TitsaError,
    TitsaOpenDataClient,
    extract_stop_routes_from_gtfs_zip,
    line_sort_key,
    normalize_line_code,
    search_stops,
)
from .const import CATALOG_REFRESH_INTERVAL, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)
CSV_FAILURE_BACKOFF_SECONDS = 300.0


def _is_stale(iso_timestamp: str | None) -> bool:
    """Return True if timestamp is missing, invalid, or older than CATALOG_REFRESH_INTERVAL."""
    if not iso_timestamp:
        return True
    try:
        updated = datetime.fromisoformat(iso_timestamp)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
    except ValueError:
        return True
    return (datetime.now(UTC) - updated) >= CATALOG_REFRESH_INTERVAL


class TitsaCatalogStore:
    """Manage local .storage cache of Cabildo de Tenerife / TITSA Open Data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: TitsaOpenDataClient,
        *,
        store: Store[dict] | None = None,
    ) -> None:
        """Initialize the catalog store."""
        self._hass = hass
        self._client = client
        self._store: Store[dict] = store or Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data = CatalogData()
        self._loaded = False
        self._csv_lock = asyncio.Lock()
        self._gtfs_lock = asyncio.Lock()
        self._gtfs_ready_event = asyncio.Event()
        self._gtfs_task: asyncio.Task[None] | None = None
        self._csv_last_failure_monotonic: float | None = None

    @property
    def data(self) -> CatalogData:
        """Return the in-memory catalog data."""
        return self._data

    @property
    def has_stops(self) -> bool:
        """Return True if stops catalog is populated."""
        return bool(self._data.stops)

    @property
    def has_gtfs_index(self) -> bool:
        """Return True if the compact GTFS stop→routes index is populated."""
        return bool(self._data.stop_routes)

    async def async_load(self) -> None:
        """Load persisted catalog from .storage without performing network I/O."""
        if self._loaded:
            return
        raw = await self._store.async_load()
        if isinstance(raw, dict):
            self._data = CatalogData.from_storage_dict(raw)
            if self._data.stop_routes:
                self._gtfs_ready_event.set()
        self._loaded = True

    def _is_csv_in_failure_backoff(self) -> bool:
        if self._csv_last_failure_monotonic is None:
            return False
        return (time.monotonic() - self._csv_last_failure_monotonic) < CSV_FAILURE_BACKOFF_SECONDS

    async def async_ensure_csv_fresh(self, *, force: bool = False) -> bool:
        """Ensure lightweight stops (329 KB) and lines (22 KB) CSVs are loaded and fresh."""
        await self.async_load()
        if (
            not force
            and self._data.stops
            and self._data.lines
            and not _is_stale(self._data.csv_updated_at)
        ):
            return True
        if not force and self._is_csv_in_failure_backoff():
            return bool(self._data.stops)

        async with self._csv_lock:
            if (
                not force
                and self._data.stops
                and self._data.lines
                and not _is_stale(self._data.csv_updated_at)
            ):
                return True
            if not force and self._is_csv_in_failure_backoff():
                return bool(self._data.stops)
            try:
                stops, lines = await asyncio.gather(
                    self._client.async_fetch_stops(),
                    self._client.async_fetch_lines(),
                )
            except TitsaError as err:
                self._csv_last_failure_monotonic = time.monotonic()
                _LOGGER.warning(
                    "Unable to refresh TITSA Open Data CSV catalog (continuing with cache): %s",
                    err,
                )
                return bool(self._data.stops)

            self._csv_last_failure_monotonic = None
            self._data.stops = stops
            self._data.lines = lines
            self._data.csv_updated_at = datetime.now(UTC).isoformat()
            await self._store.async_save(self._data.to_storage_dict())
            _LOGGER.debug(
                "Cached TITSA Open Data CSV catalog: %d stops, %d lines",
                len(stops),
                len(lines),
            )
            return True

    async def async_refresh_gtfs_index(self, *, force: bool = False) -> bool:
        """Build or refresh compact stop_id -> routes index from the GTFS ZIP."""
        await self.async_load()
        if not force and self._data.stop_routes and not _is_stale(self._data.gtfs_updated_at):
            self._gtfs_ready_event.set()
            return True

        async with self._gtfs_lock:
            if not force and self._data.stop_routes and not _is_stale(self._data.gtfs_updated_at):
                self._gtfs_ready_event.set()
                return True

            temp_path: Path | None = None
            try:
                gtfs_url, modified = await self._client.async_get_gtfs_metadata()
                # Skip heavy 21 MB download if CKAN metadata_modified hasn't changed
                if (
                    not force
                    and self._data.stop_routes
                    and modified
                    and modified == self._data.gtfs_modified
                ):
                    self._data.gtfs_updated_at = datetime.now(UTC).isoformat()
                    await self._store.async_save(self._data.to_storage_dict())
                    self._gtfs_ready_event.set()
                    return True

                with tempfile.NamedTemporaryFile(
                    prefix="titsa_gtfs_", suffix=".zip", delete=False
                ) as tmp:
                    temp_path = Path(tmp.name)

                await self._client.async_download_gtfs_to_file(gtfs_url, temp_path)
                stop_routes = await self._hass.async_add_executor_job(
                    extract_stop_routes_from_gtfs_zip, temp_path
                )
            except TitsaError as err:
                _LOGGER.warning(
                    "Unable to build TITSA GTFS stop-routes index (fallback active): %s",
                    err,
                )
                return bool(self._data.stop_routes)
            finally:
                if temp_path is not None:
                    with contextlib.suppress(OSError):
                        temp_path.unlink(missing_ok=True)

            self._data.stop_routes = stop_routes
            self._data.gtfs_updated_at = datetime.now(UTC).isoformat()
            if modified:
                self._data.gtfs_modified = modified
            await self._store.async_save(self._data.to_storage_dict())
            self._gtfs_ready_event.set()
            _LOGGER.debug(
                "Cached compact TITSA GTFS stop-routes index for %d stops",
                len(stop_routes),
            )
            return True

    def start_background_gtfs_refresh(self) -> asyncio.Task[None]:
        """Schedule non-blocking GTFS index build in the background if not already running."""
        if self._gtfs_task is not None and not self._gtfs_task.done():
            return self._gtfs_task

        async def _runner() -> None:
            try:
                await self.async_refresh_gtfs_index()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error building TITSA GTFS index in background")

        self._gtfs_task = self._hass.async_create_background_task(
            _runner(), name="titsa_gtfs_index_refresh"
        )
        return self._gtfs_task

    def cancel_background_tasks(self) -> None:
        """Cancel any running background catalog refresh task."""
        if self._gtfs_task is not None and not self._gtfs_task.done():
            self._gtfs_task.cancel()
        self._gtfs_task = None

    def get_stop(self, stop_id: str) -> StopInfo | None:
        """Return cached StopInfo by stop_id, if known."""
        return self._data.stops.get(stop_id.strip())

    def get_line(self, line_code: str) -> LineInfo | None:
        """Return cached LineInfo by normalized line code, if known."""
        return self._data.lines.get(normalize_line_code(line_code))

    def search_stops(self, query: str, *, limit: int = 50) -> list[StopInfo]:
        """Search cached stops by stop code or accent-insensitive name."""
        return search_stops(self._data.stops, query, limit=limit)

    def get_routes_for_stop(
        self,
        stop_id: str,
        *,
        live_lines: list[str] | None = None,
        extra_lines: list[str] | None = None,
    ) -> tuple[list[str], bool]:
        """Return (sorted_line_codes, is_stop_specific) using the hybrid strategy.

        Priority:
        1. Compact GTFS index for stop_id merged with any live/extra lines.
        2. Fallback when GTFS index isn't ready yet: live lines + full CSV lines catalog.
        """
        clean_id = stop_id.strip()
        gtfs_lines = self._data.stop_routes.get(clean_id)
        merged: set[str] = set()

        if gtfs_lines:
            merged.update(normalize_line_code(line) for line in gtfs_lines if line)
            if live_lines:
                merged.update(normalize_line_code(line) for line in live_lines if line)
            if extra_lines:
                merged.update(normalize_line_code(line) for line in extra_lines if line)
            merged.discard("")
            return (sorted(merged, key=line_sort_key), True)

        # Fallback: live lines first + full CSV lines catalog
        if live_lines:
            merged.update(normalize_line_code(line) for line in live_lines if line)
        if extra_lines:
            merged.update(normalize_line_code(line) for line in extra_lines if line)
        merged.update(self._data.lines.keys())
        merged.discard("")
        return (sorted(merged, key=line_sort_key), False)

    def format_line_label(self, line_code: str) -> str:
        """Format a human-readable dropdown label for a line code."""
        code = normalize_line_code(line_code)
        info = self._data.lines.get(code)
        if info and info.name:
            return f"{code} — {info.name}"
        return code

    def get_diagnostics(self) -> dict[str, object]:
        """Return non-sensitive cache statistics for diagnostics."""
        return {
            "stops_count": len(self._data.stops),
            "lines_count": len(self._data.lines),
            "gtfs_indexed_stops": len(self._data.stop_routes),
            "csv_updated_at": self._data.csv_updated_at,
            "gtfs_updated_at": self._data.gtfs_updated_at,
            "gtfs_modified": self._data.gtfs_modified,
            "gtfs_background_running": bool(self._gtfs_task and not self._gtfs_task.done()),
        }
