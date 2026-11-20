"""Tests for TitsaCatalogStore (.storage caching, GTFS background extraction, hybrid fallback)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
import zipfile

from homeassistant.core import HomeAssistant

from custom_components.titsa.api import TitsaConnectionError, TitsaOpenDataClient
from custom_components.titsa.catalog import TitsaCatalogStore


def _write_sample_gtfs(dest: Path) -> None:
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(
            "routes.txt",
            "route_id,agency_id,route_short_name,route_long_name,route_type\n"
            "r1,2,238,SC - San Matias,3\n"
            "r2,2,936,Circular Anaza,3\n",
        )
        zf.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign\nr1,1,t1,SC\nr2,1,t2,SC\n",
        )
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t1,07:00:00,07:00:00,1580,1\n"
            "t2,07:05:00,07:05:00,1580,2\n",
        )


async def test_catalog_csv_cache_and_gtfs_extraction(
    hass: HomeAssistant,
    sample_stops,
    sample_lines,
) -> None:
    """Verify CSV caching, GTFS temp-file extraction, and hybrid route lookup."""
    client = AsyncMock(spec=TitsaOpenDataClient)
    client.async_fetch_stops.return_value = sample_stops
    client.async_fetch_lines.return_value = sample_lines
    client.async_get_gtfs_metadata.return_value = (
        "https://datos.tenerife.es/mock.zip",
        "2026-11-20T00:00:46",
    )

    async def _fake_download(url: str, dest_path: Path) -> None:
        _write_sample_gtfs(dest_path)

    client.async_download_gtfs_to_file.side_effect = _fake_download

    catalog = TitsaCatalogStore(hass, client)
    assert await catalog.async_ensure_csv_fresh() is True
    assert client.async_fetch_stops.call_count == 1
    assert catalog.has_stops is True
    assert catalog.has_gtfs_index is False

    # Before GTFS finishes, hybrid lookup falls back to live + CSV lines
    fallback_lines, is_specific = catalog.get_routes_for_stop("1580", live_lines=["999"])
    assert is_specific is False
    assert "999" in fallback_lines
    assert "15" in fallback_lines

    # Second CSV check uses fresh cache (0 network calls)
    assert await catalog.async_ensure_csv_fresh() is True
    assert client.async_fetch_stops.call_count == 1

    # Build GTFS compact index
    assert await catalog.async_refresh_gtfs_index() is True
    assert catalog.has_gtfs_index is True
    assert client.async_download_gtfs_to_file.call_count == 1

    # Now lookup returns exact stop-specific routes merged with any live line
    stop_lines, is_specific = catalog.get_routes_for_stop("1580", live_lines=["975"])
    assert is_specific is True
    assert stop_lines == ["238", "936", "975"]

    # If metadata_modified is unchanged, even after staleness it skips re-downloading ZIP
    catalog.data.gtfs_updated_at = "2020-01-01T00:00:00+00:00"
    assert await catalog.async_refresh_gtfs_index() is True
    assert client.async_download_gtfs_to_file.call_count == 1


async def test_catalog_survives_open_data_outage(hass: HomeAssistant) -> None:
    """Open Data connection errors do not crash setup or catalog refresh."""
    client = AsyncMock(spec=TitsaOpenDataClient)
    client.async_fetch_stops.side_effect = TitsaConnectionError("timeout")
    client.async_fetch_lines.side_effect = TitsaConnectionError("timeout")
    client.async_get_gtfs_metadata.return_value = ("https://example.invalid/gtfs.zip", None)
    client.async_download_gtfs_to_file.side_effect = TitsaConnectionError("timeout")

    catalog = TitsaCatalogStore(hass, client)
    assert await catalog.async_ensure_csv_fresh() is False
    assert await catalog.async_refresh_gtfs_index() is False
    assert catalog.has_stops is False
    assert catalog.has_gtfs_index is False
