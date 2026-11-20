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
    result = parse_correspondencias_html(
        html_1580,
        "1580",
        now=datetime(2026, 11, 20, 9, 56, 35, tzinfo=CANARY_TZ),
    )

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
    result = parse_correspondencias_html(
        html_empty,
        "1106",
        now=datetime(2026, 11, 20, 3, 15, 5, tzinfo=CANARY_TZ),
    )

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


def test_parse_12h_php_estimation_disambiguation() -> None:
    """Resolve TITSA PHP's 12-hour Estimación clock bug for afternoon and midnight."""
    # Real case from stop 9386 at 13:55:23: TITSA prints '01:55:23'
    pm_html = """
    <html><body>
      <h1>INFORMACIÓN PARADA TRES DE MAYO (9386)</h1>
      <table>
        <tr><th>Tiempo llegada</th><th>Línea</th><th>Destino</th></tr>
        <tr><td>0 minutos</td><td>937 - INTERCAMBIADOR</td><td>CRUCE DEL SOBRADILLO</td></tr>
        <tr><td>1 minutos</td><td>15 - SANTA CRUZ - LA LAGUNA</td><td>INTERCAMBIADOR LAGUNA</td></tr>
        <tr><td>27 minutos</td><td>238 - INTERCAMBIADOR</td><td>SAN MATIAS</td></tr>
      </table>
      <p>Estimación: 20/11/2026 01:55:23</p>
    </body></html>
    """
    pm_now = datetime(2026, 11, 20, 13, 55, 24, tzinfo=CANARY_TZ)
    pm_res = parse_correspondencias_html(pm_html, "9386", now=pm_now)
    assert pm_res.estimation_time == datetime(2026, 11, 20, 13, 55, 23, tzinfo=CANARY_TZ)
    assert pm_res.arrivals[0].estimated_arrival == datetime(
        2026, 11, 20, 13, 55, 23, tzinfo=CANARY_TZ
    )
    assert pm_res.arrivals[1].minutes == 1
    assert pm_res.arrivals[2].estimated_arrival == datetime(
        2026, 11, 20, 14, 22, 23, tzinfo=CANARY_TZ
    )

    # Midnight case at 00:15:10: PHP 'h' prints '12:15:10'
    midnight_html = pm_html.replace("20/11/2026 01:55:23", "21/11/2026 12:15:10")
    midnight_now = datetime(2026, 11, 21, 0, 15, 12, tzinfo=CANARY_TZ)
    midnight_res = parse_correspondencias_html(midnight_html, "9386", now=midnight_now)
    assert midnight_res.estimation_time == datetime(2026, 11, 21, 0, 15, 10, tzinfo=CANARY_TZ)

    # Noon 12:15 stays 12:15
    noon_now = datetime(2026, 11, 21, 12, 15, 12, tzinfo=CANARY_TZ)
    noon_res = parse_correspondencias_html(midnight_html, "9386", now=noon_now)
    assert noon_res.estimation_time == datetime(2026, 11, 21, 12, 15, 10, tzinfo=CANARY_TZ)

    # True 24h timestamp (if TITSA fixes PHP in the future) stays untouched
    h24_html = pm_html.replace("20/11/2026 01:55:23", "20/11/2026 15:40:00")
    h24_now = datetime(2026, 11, 20, 15, 40, 2, tzinfo=CANARY_TZ)
    h24_res = parse_correspondencias_html(h24_html, "9386", now=h24_now)
    assert h24_res.estimation_time == datetime(2026, 11, 20, 15, 40, 0, tzinfo=CANARY_TZ)


def test_parse_unclosed_td_tags_preserves_column_order() -> None:
    """Handle old PHP HTML with missing </td> closing tags without inverting columns."""
    html = """
    <html><body>
      <h1>INFORMACIÓN PARADA TRES DE MAYO (9386)</h1>
      <table>
        <tr><th>Tiempo llegada</th><th>Línea</th><th>Destino</th></tr>
        <tr>
          <td>0 minutos</td>
          <td><b>15</b> - SANTA CRUZ - LA LAGUNA (DIRECTA)
          <td><b>INTERCAMBIADOR LAGUNA</b></td></td>
        </tr>
        <tr>
          <td>1 minutos
          <td><b>231</b> - INTERCAMBIADOR SANTA CRUZ - LA GALLEGA (DIRECTO)
          <td><b>EL CARDONAL - LA GALLEGA</b>
        </tr>
      </table>
      <p>Estimación: 20/11/2026 01:55:23</p>
    </body></html>
    """
    result = parse_correspondencias_html(
        html, "9386", now=datetime(2026, 11, 20, 13, 55, 25, tzinfo=CANARY_TZ)
    )
    assert len(result.arrivals) == 2
    assert result.arrivals[0].line == "15"
    assert result.arrivals[0].line_name == "SANTA CRUZ - LA LAGUNA (DIRECTA)"
    assert result.arrivals[0].destination == "INTERCAMBIADOR LAGUNA"
    assert result.arrivals[1].line == "231"
    assert result.arrivals[1].destination == "EL CARDONAL - LA GALLEGA"


def test_parse_swapped_destino_and_linea_columns() -> None:
    """Correctly parse arrivals even if HTML columns list Destino before Línea."""
    html = """
    <html><body>
      <h1>INFORMACIÓN PARADA TRES DE MAYO (9386)</h1>
      <table>
        <tr><th>Tiempo llegada</th><th>Destino</th><th>Línea</th></tr>
        <tr>
          <td>2 minutos</td>
          <td>PUNTA DEL HIDALGO</td>
          <td>105 - SANTA CRUZ - PUNTA DEL HIDALGO - POR LA LAGUNA</td>
        </tr>
      </table>
      <p>Estimación: 20/11/2026 01:55:23</p>
    </body></html>
    """
    result = parse_correspondencias_html(
        html, "9386", now=datetime(2026, 11, 20, 13, 55, 25, tzinfo=CANARY_TZ)
    )
    assert len(result.arrivals) == 1
    assert result.arrivals[0].line == "105"
    assert result.arrivals[0].destination == "PUNTA DEL HIDALGO"
    assert result.arrivals[0].minutes == 2
