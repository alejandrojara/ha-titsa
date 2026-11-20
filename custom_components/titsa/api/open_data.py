"""Cabildo de Tenerife / TITSA Open Data (CC BY 4.0) catalog & GTFS index client."""

from __future__ import annotations

from collections.abc import Mapping
import csv
import io
from pathlib import Path
import zipfile

import aiohttp

from .exceptions import TitsaConnectionError, TitsaParseError
from .models import LineInfo, StopInfo
from .provider import DEFAULT_USER_AGENT
from .utils import clean_titsa_stop_name, line_sort_key, normalize_line_code, normalize_text

CKAN_PACKAGE_SHOW_URL = "https://datos.tenerife.es/ckan/api/3/action/package_show"

STOPS_PACKAGE_ID = "749d9208-ad97-47f9-a497-0385df40420d"
LINES_PACKAGE_ID = "31a02e4a-9d70-44d2-bf39-b8cb072bd4bb"
GTFS_PACKAGE_ID = "36c2e26f-0d18-4b5a-b214-1636168e0765"

FALLBACK_STOPS_CSV_URL = (
    "https://datos.tenerife.es/ckan/dataset/749d9208-ad97-47f9-a497-0385df40420d"
    "/resource/459b3a8b-f77f-42e0-ab4a-90b6400f25d8/download/paradas-de-guagua.csv"
)
FALLBACK_LINES_CSV_URL = (
    "https://datos.tenerife.es/ckan/dataset/31a02e4a-9d70-44d2-bf39-b8cb072bd4bb"
    "/resource/d6340b7d-ed4d-4a43-8051-a9b6127d30c2/download/lineas-y-horarios.csv"
)
FALLBACK_GTFS_ZIP_URL = (
    "https://datos.tenerife.es/ckan/dataset/36c2e26f-0d18-4b5a-b214-1636168e0765"
    "/resource/9f291323-8b78-453a-9008-4f0e3bfb3ce3/download/fichero-zip-de-google-transit.zip"
)


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.strip().replace(",", "."))
    except ValueError:
        return None


def parse_stops_csv(csv_text: str) -> dict[str, StopInfo]:
    """Parse Cabildo de Tenerife paradas-de-guagua.csv (handles UTF-8 BOM)."""
    cleaned = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(cleaned))
    if not reader.fieldnames:
        raise TitsaParseError("Empty stops CSV")

    stops: dict[str, StopInfo] = {}
    for row in reader:
        stop_id = (row.get("parada_id") or row.get("stop_id") or "").strip()
        if not stop_id:
            continue
        raw_name = (row.get("parada_nombre") or row.get("stop_name") or "").strip()
        clean_name = clean_titsa_stop_name(raw_name) or f"Stop {stop_id}"
        stops[stop_id] = StopInfo(
            stop_id=stop_id,
            name=clean_name,
            raw_name=raw_name or clean_name,
            latitude=_parse_float(row.get("latitud") or row.get("stop_lat")),
            longitude=_parse_float(row.get("longitud") or row.get("stop_lon")),
        )
    return stops


def parse_lines_csv(csv_text: str) -> dict[str, LineInfo]:
    """Parse Cabildo de Tenerife lineas-y-horarios.csv (handles UTF-8 BOM)."""
    cleaned = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(cleaned))
    if not reader.fieldnames:
        raise TitsaParseError("Empty lines CSV")

    lines: dict[str, LineInfo] = {}
    for row in reader:
        raw_code = (row.get("linea_numero") or row.get("route_short_name") or "").strip()
        code = normalize_line_code(raw_code)
        if not code:
            continue
        raw_name = (row.get("linea_nombre") or row.get("route_long_name") or "").strip()
        name = " ".join(raw_name.split())
        url = (row.get("linea_url") or row.get("route_url") or "").strip() or None
        lines[code] = LineInfo(line=code, name=name, url=url)
    return lines


def extract_stop_routes_from_gtfs_zip(zip_path: Path | str) -> dict[str, list[str]]:
    """Extract a compact stop_id -> sorted line codes mapping from a GTFS ZIP.

    Designed to run in a worker thread (blocking file/ZIP I/O). Uses column-indexed
    csv.reader on stop_times.txt to keep RAM and CPU minimal on Raspberry Pi devices.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            required = {"routes.txt", "trips.txt", "stop_times.txt"}
            missing = required - names
            if missing:
                raise TitsaParseError(f"GTFS ZIP missing required files: {sorted(missing)}")

            # 1. route_id -> normalized route_short_name
            route_id_to_line: dict[str, str] = {}
            with zf.open("routes.txt") as raw_f:
                reader = csv.DictReader(io.TextIOWrapper(raw_f, encoding="utf-8-sig"))
                for row in reader:
                    route_id = (row.get("route_id") or "").strip()
                    short_name = (row.get("route_short_name") or route_id).strip()
                    line_code = normalize_line_code(short_name)
                    if route_id and line_code:
                        route_id_to_line[route_id] = line_code

            # 2. trip_id -> normalized line code
            trip_id_to_line: dict[str, str] = {}
            with zf.open("trips.txt") as raw_f:
                trips_reader = csv.DictReader(io.TextIOWrapper(raw_f, encoding="utf-8-sig"))
                for trip_row in trips_reader:
                    trip_id = (trip_row.get("trip_id") or "").strip()
                    route_id = (trip_row.get("route_id") or "").strip()
                    mapped_line = route_id_to_line.get(route_id)
                    if trip_id and mapped_line:
                        trip_id_to_line[trip_id] = mapped_line

            # 3. Stream stop_times.txt with indexed csv.reader for speed & low RAM
            stop_routes: dict[str, set[str]] = {}
            with zf.open("stop_times.txt") as raw_f:
                text_f = io.TextIOWrapper(raw_f, encoding="utf-8-sig")
                csv_reader = csv.reader(text_f)
                header = next(csv_reader, None)
                if not header:
                    raise TitsaParseError("Empty stop_times.txt in GTFS ZIP")
                header_clean = [col.strip() for col in header]
                try:
                    trip_idx = header_clean.index("trip_id")
                    stop_idx = header_clean.index("stop_id")
                except ValueError as err:
                    raise TitsaParseError(
                        f"Missing trip_id/stop_id columns in stop_times.txt: {header_clean}"
                    ) from err

                min_len = max(trip_idx, stop_idx) + 1
                for stop_time_row in csv_reader:
                    if len(stop_time_row) < min_len:
                        continue
                    stop_line = trip_id_to_line.get(stop_time_row[trip_idx].strip())
                    if not stop_line:
                        continue
                    stop_id = stop_time_row[stop_idx].strip()
                    if stop_id:
                        routes_set = stop_routes.get(stop_id)
                        if routes_set is None:
                            stop_routes[stop_id] = {stop_line}
                        else:
                            routes_set.add(stop_line)
    except zipfile.BadZipFile as err:
        raise TitsaParseError(f"Invalid GTFS ZIP archive: {err}") from err

    return {
        stop_id: sorted(lines, key=line_sort_key) for stop_id, lines in sorted(stop_routes.items())
    }


def search_stops(
    stops: Mapping[str, StopInfo],
    query: str,
    *,
    limit: int = 50,
) -> list[StopInfo]:
    """Search stops by exact ID, ID prefix, or accent-insensitive name tokens."""
    stripped = query.strip()
    if not stripped:
        return []

    exact: list[StopInfo] = []
    id_prefix: list[StopInfo] = []
    name_starts: list[StopInfo] = []
    name_contains: list[StopInfo] = []

    norm_query = normalize_text(stripped)
    tokens = norm_query.split()

    for stop in stops.values():
        if stop.stop_id == stripped:
            exact.append(stop)
            continue
        if stripped.isdigit() and stop.stop_id.startswith(stripped):
            id_prefix.append(stop)
            continue

        norm_name = normalize_text(stop.name)
        if all(token in norm_name for token in tokens):
            if norm_name.startswith(norm_query):
                name_starts.append(stop)
            else:
                name_contains.append(stop)

    id_prefix.sort(key=lambda s: (len(s.stop_id), s.stop_id))
    name_starts.sort(key=lambda s: (normalize_text(s.name), s.stop_id))
    name_contains.sort(key=lambda s: (normalize_text(s.name), s.stop_id))

    combined = exact + id_prefix + name_starts + name_contains
    return combined[:limit]


class TitsaOpenDataClient:
    """Async HTTP client for Cabildo de Tenerife Open Data (CKAN + CSV + GTFS)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        gtfs_timeout: float = 90.0,
    ) -> None:
        """Initialize the Open Data client."""
        self._session = session
        self._headers = {"User-Agent": user_agent}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._gtfs_timeout = aiohttp.ClientTimeout(total=gtfs_timeout)

    async def _async_resolve_resource(
        self,
        package_id: str,
        preferred_format: str,
        fallback_url: str,
    ) -> tuple[str, str | None]:
        """Resolve latest download URL and metadata_modified timestamp via CKAN."""
        try:
            async with self._session.get(
                CKAN_PACKAGE_SHOW_URL,
                params={"id": package_id},
                headers=self._headers,
                timeout=self._timeout,
            ) as response:
                if response.status != 200:
                    return (fallback_url, None)
                payload = await response.json()
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return (fallback_url, None)

        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            return (fallback_url, None)

        modified = result.get("metadata_modified")
        resources = result.get("resources") or []
        fmt_upper = preferred_format.upper()
        for res in resources:
            if not isinstance(res, dict):
                continue
            res_fmt = str(res.get("format") or "").upper()
            res_url = str(res.get("url") or "").strip()
            if res_fmt == fmt_upper and res_url:
                return (res_url, str(modified) if modified else None)

        return (fallback_url, str(modified) if modified else None)

    async def _async_fetch_text(self, url: str) -> str:
        """Fetch a UTF-8 text resource."""
        try:
            async with self._session.get(
                url,
                headers=self._headers,
                timeout=self._timeout,
            ) as response:
                if response.status != 200:
                    raise TitsaConnectionError(
                        f"HTTP {response.status} fetching Open Data resource {url}",
                        status=response.status,
                    )
                raw = await response.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise TitsaConnectionError(f"Error fetching Open Data resource {url}: {err}") from err

        return raw.decode("utf-8-sig", errors="replace")

    async def async_fetch_stops(self) -> dict[str, StopInfo]:
        """Download and parse the lightweight stops CSV (~330 KB)."""
        url, _ = await self._async_resolve_resource(STOPS_PACKAGE_ID, "CSV", FALLBACK_STOPS_CSV_URL)
        text = await self._async_fetch_text(url)
        return parse_stops_csv(text)

    async def async_fetch_lines(self) -> dict[str, LineInfo]:
        """Download and parse the lightweight lines CSV (~22 KB)."""
        url, _ = await self._async_resolve_resource(LINES_PACKAGE_ID, "CSV", FALLBACK_LINES_CSV_URL)
        text = await self._async_fetch_text(url)
        return parse_lines_csv(text)

    async def async_get_gtfs_metadata(self) -> tuple[str, str | None]:
        """Return (gtfs_zip_url, metadata_modified) from CKAN."""
        return await self._async_resolve_resource(GTFS_PACKAGE_ID, "ZIP", FALLBACK_GTFS_ZIP_URL)

    async def async_download_gtfs_to_file(self, url: str, dest_path: Path) -> None:
        """Stream the GTFS ZIP archive to disk in chunks to avoid high memory usage."""
        try:
            async with self._session.get(
                url,
                headers=self._headers,
                timeout=self._gtfs_timeout,
            ) as response:
                if response.status != 200:
                    raise TitsaConnectionError(
                        f"HTTP {response.status} downloading GTFS ZIP from {url}",
                        status=response.status,
                    )
                with dest_path.open("wb") as file_obj:
                    async for chunk in response.content.iter_chunked(65536):
                        if chunk:
                            file_obj.write(chunk)
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise TitsaConnectionError(f"Error downloading GTFS ZIP from {url}: {err}") from err
