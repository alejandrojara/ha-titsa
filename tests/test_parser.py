"""Unit tests for the zero-dependency correspondencias.php HTML parser and API isolation."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from custom_components.titsa.api import (
    CANARY_TZ,
    TitsaParseError,
    TitsaStopNotFoundError,
    parse_correspondencias_html,
)


def test_api_package_has_no_homeassistant_imports() -> None:
    """Verify the decoupled api/ package has zero homeassistant imports."""
    api_dir = Path(__file__).parent.parent / "custom_components" / "titsa" / "api"
    for py_file in api_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("homeassistant"), (
                        f"Forbidden homeassistant import in {py_file.name}: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("homeassistant"), (
                    f"Forbidden homeassistant import in {py_file.name}: {node.module}"
                )


def test_parse_correspondencias_1580(html_1580: str) -> None:
    """Parse real-world Nuevo Obrero (1580) HTML fixture."""
    result = parse_correspondencias_html(html_1580, "1580")

    assert result.stop_id == "1580"
    assert result.stop_name == "Nuevo Obrero"
    assert result.estimation_from_server is True
    assert result.estimation_time == datetime(2026, 11, 20, 9, 56, 32, tzinfo=CANARY_TZ)
    assert len(result.arrivals) == 4

    first = result.arrivals[0]
    assert first.line == "238"
    assert first.line_name == "INTERCAMBIADOR SANTA CRUZ - SAN MATIAS - POR CHAMBERÍ"
    assert first.destination == "INTERCAMBIADOR STA.CRUZ"
    assert first.minutes == 0
    assert first.estimated_arrival == result.estimation_time
    assert first.is_approximate is False

    second = result.arrivals[1]
    assert second.line == "936"
    assert second.minutes == 13
    assert second.estimated_arrival == result.estimation_time + timedelta(minutes=13)

    assert [a.line for a in result.arrivals] == ["238", "936", "232", "937"]
    assert [a.minutes for a in result.arrivals] == [0, 13, 29, 45]


def test_parse_correspondencias_empty(html_empty: str) -> None:
    """Parse valid stop page with no arriving buses."""
    result = parse_correspondencias_html(html_empty, "1106")

    assert result.stop_id == "1106"
    assert result.stop_name == "Boca Cangrejo"
    assert result.arrivals == ()
    assert result.estimation_time == datetime(2026, 11, 20, 3, 15, 0, tzinfo=CANARY_TZ)


def test_parse_correspondencias_edge_cases(html_edge: str) -> None:
    """Handle summer DST (WEST +01:00), <1 min, singular 1 minuto, >60 min, and leading zero 015."""
    result = parse_correspondencias_html(html_edge, "2625")

    assert result.stop_id == "2625"
    assert result.stop_name == "Intercambiador Laguna"
    # July in Atlantic/Canary is UTC+1 (WEST)
    assert result.estimation_time.utcoffset() == timedelta(hours=1)
    assert len(result.arrivals) == 3

    assert result.arrivals[0].line == "15"
    assert result.arrivals[0].minutes == 0
    assert result.arrivals[0].is_approximate is False

    assert result.arrivals[1].line == "15"
    assert result.arrivals[1].minutes == 1

    assert result.arrivals[2].line == "102"
    assert result.arrivals[2].minutes == 60
    assert result.arrivals[2].is_approximate is True
    assert result.arrivals[2].estimated_arrival == result.estimation_time + timedelta(minutes=60)


def test_parse_correspondencias_filter_lines(html_1580: str) -> None:
    """Filter parsed arrivals by normalized line codes."""
    result = parse_correspondencias_html(html_1580, "1580")

    filtered = result.filter_lines({"936", "937"})
    assert [a.line for a in filtered] == ["936", "937"]
    assert result.filter_lines(set()) == result.arrivals


def test_parse_correspondencias_invalid_and_empty_inputs() -> None:
    """Raise appropriate exceptions on empty, unrelated, or non-existent stop HTML."""
    with pytest.raises(TitsaParseError):
        parse_correspondencias_html("", "1580")

    with pytest.raises(TitsaParseError):
        parse_correspondencias_html("<html><body><p>Maintenance page</p></body></html>", "1580")

    with pytest.raises(TitsaStopNotFoundError):
        parse_correspondencias_html(
            "<html><body><p>Error: No existe la parada solicitada.</p></body></html>",
            "99999",
        )

    # PHP page with empty stop name before (99999) and no arrivals -> unknown stop
    with pytest.raises(TitsaStopNotFoundError):
        parse_correspondencias_html(
            "<html><body><h1>INFORMACIÓN PARADA  (99999)</h1>"
            "<p>Estimación: 20/11/2026 10:00:00</p></body></html>",
            "99999",
        )


def test_parse_nested_tables_and_clock_time_and_linea_prefix() -> None:
    """Handle layout tables wrapping the arrivals table, HH:MM times, and 'Línea ' prefix."""
    html = """
    <html><body>
      <table class="layout">
        <tr><td>
          <h1>INFORMACIÓN PARADA NUEVO OBRERO (1580)</h1>
          <table>
            <tr><th>Tiempo llegada</th><th>Línea</th><th>Destino</th></tr>
            <tr><td>10:15</td><td>Línea 238 - INTERCAMBIADOR</td><td>INTERCAMBIADOR STA.CRUZ</td></tr>
          </table>
          <p>Estimación: 20/11/2026 09:56:32</p>
        </td></tr>
      </table>
    </body></html>
    """
    result = parse_correspondencias_html(html, "1580")
    assert result.stop_name == "Nuevo Obrero"
    assert len(result.arrivals) == 1
    assert result.arrivals[0].line == "238"
    assert result.arrivals[0].minutes == 18
