"""Unit tests for Cabildo de Tenerife Open Data CSV, GTFS extraction, and search."""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from custom_components.titsa.api import (
    CatalogData,
    TitsaParseError,
    clean_titsa_stop_name,
    extract_stop_routes_from_gtfs_zip,
    line_sort_key,
    normalize_line_code,
    parse_lines_csv,
    parse_stops_csv,
    search_stops,
)


def test_parse_stops_csv_with_utf8_bom() -> None:
    """Parse stops CSV prefixed with UTF-8 BOM and clean names."""
    raw = (
        "\ufeffparada_id,parada_nombre,latitud,longitud,parada_url\n"
        "1580,NUEVO  OBRERO,28.4451,-16.2921,http://www.titsa.com/correspondencias.php?idc=1580\n"
        "1100,ACORÁN,28.4184,-16.3038,http://www.titsa.com/correspondencias.php?idc=1100\n"
    )
    stops = parse_stops_csv(raw)

    assert set(stops) == {"1580", "1100"}
    assert stops["1580"].name == "Nuevo Obrero"
    assert stops["1580"].latitude == pytest.approx(28.4451)
    assert stops["1580"].longitude == pytest.approx(-16.2921)
    assert stops["1100"].name == "Acorán"


def test_parse_lines_csv_with_utf8_bom() -> None:
    """Parse lines CSV prefixed with UTF-8 BOM and normalize line codes."""
    raw = (
        "\ufefflinea_numero,linea_nombre,linea_url\n"
        "015,SANTA CRUZ - LA LAGUNA  DIRECTA,http://www.titsa.com/linea-015\n"
        "238,INTERCAMBIADOR SANTA CRUZ - SAN MATIAS,http://www.titsa.com/linea-238\n"
    )
    lines = parse_lines_csv(raw)

    assert set(lines) == {"15", "238"}
    assert lines["15"].name == "SANTA CRUZ - LA LAGUNA DIRECTA"
    assert lines["238"].url == "http://www.titsa.com/linea-238"


def test_extract_stop_routes_from_gtfs_zip(tmp_path: Path) -> None:
    """Extract compact stop_id -> sorted route codes index from a GTFS ZIP archive."""
    zip_path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "routes.txt",
            "route_id,agency_id,route_short_name,route_long_name,route_type\n"
            "r1,2,015,SC - LL,3\n"
            "r2,2,238,SC - San Matias,3\n"
            "r3,2,936,Circular Anaza,3\n",
        )
        zf.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign\nr2,1,t1,SC\nr3,1,t2,SC\nr1,1,t3,LL\n",
        )
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t1,07:00:00,07:00:00,1580,1\n"
            "t2,07:10:00,07:10:00,1580,2\n"
            "t1,07:15:00,07:15:00,1580,3\n"
            "t3,07:20:00,07:20:00,2625,1\n",
        )

    mapping = extract_stop_routes_from_gtfs_zip(zip_path)
    assert mapping == {
        "1580": ["238", "936"],
        "2625": ["15"],
    }


def test_extract_gtfs_invalid_zip(tmp_path: Path) -> None:
    """Raise TitsaParseError on corrupt or incomplete GTFS ZIP."""
    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_bytes(b"not a zip")
    with pytest.raises(TitsaParseError):
        extract_stop_routes_from_gtfs_zip(bad_zip)

    incomplete_zip = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(incomplete_zip, "w") as zf:
        zf.writestr("routes.txt", "route_id,route_short_name\n")
    with pytest.raises(TitsaParseError):
        extract_stop_routes_from_gtfs_zip(incomplete_zip)


def test_search_stops_accent_insensitive_and_id_priority(sample_stops) -> None:
    """Search matches exact ID first, then ID prefix, then accent-insensitive name."""
    exact = search_stops(sample_stops, "1580")
    assert exact[0].stop_id == "1580"

    acoran = search_stops(sample_stops, "acoran")
    assert [s.stop_id for s in acoran] == ["1100"]

    guimar = search_stops(sample_stops, "guimar")
    assert [s.stop_id for s in guimar] == ["3200"]

    nuevo = search_stops(sample_stops, "nuevo obrero")
    assert [s.stop_id for s in nuevo] == ["1580", "1581"]


def test_utils_cleaning_and_sorting() -> None:
    """Verify stop-name cleaning, line normalization, and natural sorting."""
    assert clean_titsa_stop_name("ARICO EL VIEJO (T)") == "Arico el Viejo (T)"
    assert clean_titsa_stop_name("INTERCAMBIADOR STA.CRUZ") == "Intercambiador Sta. Cruz"
    assert clean_titsa_stop_name("AEROPUERTO TFS") == "Aeropuerto TFS"

    assert normalize_line_code("015") == "15"
    assert normalize_line_code("030a") == "30A"
    assert normalize_line_code("0") == "0"

    assert sorted(["936", "15", "238", "10", "126", "A1"], key=line_sort_key) == [
        "10",
        "15",
        "126",
        "238",
        "936",
        "A1",
    ]


def test_catalog_storage_roundtrip(sample_stops, sample_lines, sample_stop_routes) -> None:
    """Verify CatalogData serializes and restores cleanly."""
    original = CatalogData(
        stops=sample_stops,
        lines=sample_lines,
        stop_routes=sample_stop_routes,
        csv_updated_at="2026-11-20T00:00:00+00:00",
        gtfs_updated_at="2026-11-20T00:01:00+00:00",
        gtfs_modified="2026-11-20T00:00:46",
    )
    restored = CatalogData.from_storage_dict(original.to_storage_dict())

    assert restored.stops == original.stops
    assert restored.lines == original.lines
    assert restored.stop_routes == original.stop_routes
    assert restored.gtfs_modified == original.gtfs_modified
