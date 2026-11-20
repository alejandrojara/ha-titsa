"""Decoupled Python API client for TITSA (Tenerife public bus transport).

This package intentionally has zero Home Assistant imports so it can be tested
independently and extracted to a standalone PyPI library in the future.
"""

from .exceptions import (
    TitsaConnectionError,
    TitsaError,
    TitsaParseError,
    TitsaRateLimitError,
    TitsaStopNotFoundError,
)
from .models import BusArrival, CatalogData, LineInfo, StopArrivals, StopInfo
from .open_data import (
    TitsaOpenDataClient,
    extract_stop_routes_from_gtfs_bytes,
    extract_stop_routes_from_gtfs_zip,
    parse_lines_csv,
    parse_stops_csv,
    search_stops,
)
from .parser import CANARY_TZ, parse_correspondencias_html
from .provider import TitsaRealtimeProvider, TitsaWebProvider
from .utils import (
    clean_titsa_stop_name,
    line_sort_key,
    normalize_line_code,
    normalize_text,
    parse_retry_after,
)

__all__ = [
    "CANARY_TZ",
    "BusArrival",
    "CatalogData",
    "LineInfo",
    "StopArrivals",
    "StopInfo",
    "TitsaConnectionError",
    "TitsaError",
    "TitsaOpenDataClient",
    "TitsaParseError",
    "TitsaRateLimitError",
    "TitsaRealtimeProvider",
    "TitsaStopNotFoundError",
    "TitsaWebProvider",
    "clean_titsa_stop_name",
    "extract_stop_routes_from_gtfs_bytes",
    "extract_stop_routes_from_gtfs_zip",
    "line_sort_key",
    "normalize_line_code",
    "normalize_text",
    "parse_correspondencias_html",
    "parse_lines_csv",
    "parse_retry_after",
    "parse_stops_csv",
    "search_stops",
]
