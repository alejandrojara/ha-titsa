"""Shared pytest fixtures for TITSA integration tests."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.titsa.api import (
    CANARY_TZ,
    LineInfo,
    StopArrivals,
    StopInfo,
    parse_correspondencias_html,
    parse_lines_csv,
    parse_stops_csv,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in Home Assistant for all tests."""
    return


@pytest.fixture
def html_1580() -> str:
    """Return sample HTML for stop 1580 (Nuevo Obrero)."""
    return (FIXTURES_DIR / "correspondencias_1580.html").read_text(encoding="utf-8")


@pytest.fixture
def html_empty() -> str:
    """Return sample HTML for an empty stop."""
    return (FIXTURES_DIR / "correspondencias_empty.html").read_text(encoding="utf-8")


@pytest.fixture
def html_edge() -> str:
    """Return edge-case HTML fixture."""
    return (FIXTURES_DIR / "correspondencias_edge.html").read_text(encoding="utf-8")


@pytest.fixture
def sample_arrivals_1580(html_1580: str) -> StopArrivals:
    """Return parsed StopArrivals for stop 1580."""
    return parse_correspondencias_html(
        html_1580,
        "1580",
        now=datetime(2026, 11, 20, 9, 56, 35, tzinfo=CANARY_TZ),
    )


@pytest.fixture
def sample_stops() -> dict[str, StopInfo]:
    """Return parsed sample stops catalog."""
    return parse_stops_csv((FIXTURES_DIR / "paradas_sample.csv").read_text(encoding="utf-8"))


@pytest.fixture
def sample_lines() -> dict[str, LineInfo]:
    """Return parsed sample lines catalog."""
    return parse_lines_csv((FIXTURES_DIR / "lineas_sample.csv").read_text(encoding="utf-8"))


@pytest.fixture
def sample_stop_routes() -> dict[str, list[str]]:
    """Return compact stop->routes GTFS sample mapping."""
    return {
        "1580": ["126", "232", "238", "936", "937"],
        "1581": ["232", "238"],
        "2625": ["15"],
    }


@pytest.fixture
def mock_titsa_network(
    sample_arrivals_1580: StopArrivals,
    sample_stops: dict[str, StopInfo],
    sample_lines: dict[str, LineInfo],
    sample_stop_routes: dict[str, list[str]],
) -> Generator[tuple[AsyncMock, AsyncMock]]:
    """Patch TITSA web provider and Open Data client so tests never hit external networks."""
    mock_fetch_arrivals = AsyncMock(return_value=sample_arrivals_1580)
    mock_fetch_stops = AsyncMock(return_value=sample_stops)
    mock_fetch_lines = AsyncMock(return_value=sample_lines)
    mock_gtfs_meta = AsyncMock(
        return_value=("https://datos.tenerife.es/mock.zip", "2026-11-20T00:00:00")
    )

    with (
        patch(
            "custom_components.titsa.api.provider.TitsaWebProvider.async_get_stop_arrivals",
            mock_fetch_arrivals,
        ),
        patch(
            "custom_components.titsa.api.open_data.TitsaOpenDataClient.async_fetch_stops",
            mock_fetch_stops,
        ),
        patch(
            "custom_components.titsa.api.open_data.TitsaOpenDataClient.async_fetch_lines",
            mock_fetch_lines,
        ),
        patch(
            "custom_components.titsa.api.open_data.TitsaOpenDataClient.async_get_gtfs_metadata",
            mock_gtfs_meta,
        ),
        patch(
            "custom_components.titsa.catalog.TitsaCatalogStore.async_refresh_gtfs_index",
            AsyncMock(return_value=True),
        ) as mock_gtfs_refresh,
    ):
        yield mock_fetch_arrivals, mock_gtfs_refresh
