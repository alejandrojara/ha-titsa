"""Zero-dependency HTML parser for titsa.com/correspondencias.php."""

from __future__ import annotations

from datetime import datetime, timedelta
from html.parser import HTMLParser
import re
from zoneinfo import ZoneInfo

from .exceptions import TitsaParseError, TitsaStopNotFoundError
from .models import BusArrival, StopArrivals
from .utils import clean_titsa_stop_name, normalize_line_code, normalize_text

CANARY_TZ = ZoneInfo("Atlantic/Canary")

_STOP_HEADER_RE = re.compile(
    r"informaci[oó]n\s+parada\s+(.+?)\s*\(\s*(\d+)\s*\)",
    re.IGNORECASE,
)
_CORRESP_HEADER_RE = re.compile(
    r"correspondencias\s+en\s+(.+)",
    re.IGNORECASE,
)
_ESTIMATION_RE = re.compile(
    r"estimaci[oó]n\s*:\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?",
    re.IGNORECASE,
)
_HOURS_MINUTES_RE = re.compile(
    r"(\d+)\s*(?:h(?:oras?)?)\s*(?:(\d+)\s*(?:m(?:in(?:utos?)?)?))?",
    re.IGNORECASE,
)
_MINUTES_RE = re.compile(r"([<>+])?\s*(\d+)\s*(?:min(?:utos?)?|m\b)?", re.IGNORECASE)
_NOT_FOUND_MARKERS = (
    "no existe la parada",
    "parada no encontrada",
    "parada inexistente",
    "codigo de parada incorrecto",
    "código de parada incorrecto",
)
_NO_BUSES_MARKERS = (
    "no hay guaguas",
    "no hay correspondencias",
    "no hay informaci",
    "sin llegadas",
    "sin estimaci",
    "no se han encontrado",
)


class _CorrespondenciasHTMLParser(HTMLParser):
    """Collect tables and visible text blocks from correspondencias.php HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cell_is_header = False
        self._current_cell: list[str] = []
        self._current_row: list[tuple[bool, str]] = []
        self._current_table: list[list[tuple[bool, str]]] = []
        self.tables: list[list[list[tuple[bool, str]]]] = []
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if tag_lower == "table":
            self._in_table = True
            self._current_table = []
        elif tag_lower == "tr" and self._in_table:
            self._in_row = True
            self._current_row = []
        elif tag_lower in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cell_is_header = tag_lower == "th"
            self._current_cell = []
        elif tag_lower in ("br", "p", "div", "h1", "h2", "h3", "h4", "li", "tr"):
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("script", "style", "noscript"):
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if tag_lower in ("td", "th") and self._in_cell:
            cell_text = " ".join("".join(self._current_cell).split())
            self._current_row.append((self._cell_is_header, cell_text))
            self._in_cell = False
            self._current_cell = []
        elif tag_lower == "tr" and self._in_row:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._in_row = False
            self._current_row = []
        elif tag_lower == "table" and self._in_table:
            if self._current_table:
                self.tables.append(self._current_table)
            self._in_table = False
            self._current_table = []
        elif tag_lower in ("p", "div", "h1", "h2", "h3", "h4", "li"):
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_cell:
            self._current_cell.append(data)
        self._text_parts.append(data)

    @property
    def plain_text(self) -> str:
        """Return newline-separated visible text."""
        raw = "".join(self._text_parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _parse_minutes(raw_time: str) -> tuple[int, bool] | None:
    """Parse arrival ETA text into (minutes, is_approximate)."""
    text = " ".join(raw_time.split()).strip()
    if not text:
        return None
    norm = normalize_text(text)

    if any(marker in norm for marker in ("en parada", "llegando", "inminente", "< 1", "<1")):
        return (0, False)

    hm_match = _HOURS_MINUTES_RE.search(norm)
    if hm_match and "h" in norm:
        hours = int(hm_match.group(1))
        mins = int(hm_match.group(2)) if hm_match.group(2) else 0
        is_approx = ">" in text or "+" in text or "mas de" in norm
        return (hours * 60 + mins, is_approx)

    m_match = _MINUTES_RE.search(text)
    if m_match:
        prefix, value = m_match.groups()
        is_approx = prefix in (">", "+") or "mas de" in norm
        return (int(value), is_approx)

    return None


def _parse_line_cell(raw_line: str) -> tuple[str, str]:
    """Split '238 - INTERCAMBIADOR SANTA CRUZ - SAN MATIAS' into ('238', 'INTERCAMBIADOR...')."""
    cleaned = " ".join(raw_line.split()).strip()
    if not cleaned:
        return ("", "")

    # Common separator: "238 - ROUTE NAME" or "238-ROUTE NAME"
    sep_match = re.match(r"^([A-Za-z0-9]+)\s*[-–—:]\s*(.+)$", cleaned)
    if sep_match:
        code_raw, name = sep_match.groups()
        return (normalize_line_code(code_raw), name.strip())

    # Fallback: leading token followed by whitespace
    space_match = re.match(r"^([A-Za-z0-9]+)\s+(.+)$", cleaned)
    if space_match:
        code_raw, name = space_match.groups()
        return (normalize_line_code(code_raw), name.strip())

    return (normalize_line_code(cleaned), "")


def _is_header_row(row: list[tuple[bool, str]]) -> bool:
    """Return True if a table row represents the column header."""
    if all(is_header for is_header, _ in row):
        return True
    joined = " ".join(normalize_text(text) for _, text in row)
    return "tiempo" in joined and ("linea" in joined or "destino" in joined)


def _extract_arrivals_table(
    tables: list[list[list[tuple[bool, str]]]],
    estimation_time: datetime,
) -> tuple[bool, list[BusArrival]]:
    """Find the arrivals table and parse BusArrival items."""
    found_correspondencias_table = False
    arrivals: list[BusArrival] = []

    for table in tables:
        has_expected_header = any(_is_header_row(row) for row in table)
        table_arrivals: list[BusArrival] = []

        for row in table:
            if _is_header_row(row):
                found_correspondencias_table = True
                continue
            texts = [text for _, text in row]
            if len(texts) < 3:
                continue
            parsed_time = _parse_minutes(texts[0])
            if parsed_time is None:
                continue
            minutes, is_approx = parsed_time
            line_code, line_name = _parse_line_cell(texts[1])
            if not line_code:
                continue
            destination = " ".join(texts[2].split()).strip()
            est_arrival = estimation_time + timedelta(minutes=minutes)
            table_arrivals.append(
                BusArrival(
                    line=line_code,
                    line_name=line_name,
                    destination=destination,
                    minutes=minutes,
                    estimated_arrival=est_arrival,
                    is_approximate=is_approx,
                )
            )

        if has_expected_header or table_arrivals:
            found_correspondencias_table = True
            arrivals.extend(table_arrivals)
            break

    arrivals.sort(key=lambda item: item.minutes)
    return found_correspondencias_table, arrivals


def parse_correspondencias_html(
    html: str,
    stop_id: str,
    *,
    now: datetime | None = None,
) -> StopArrivals:
    """Parse HTML returned by titsa.com/correspondencias.php?idc=<stop_id>."""
    if not html or not html.strip():
        raise TitsaParseError(f"Empty HTML response for stop {stop_id}")

    parser = _CorrespondenciasHTMLParser()
    parser.feed(html)
    parser.close()

    plain_text = parser.plain_text
    norm_text = normalize_text(plain_text)

    if any(marker in norm_text for marker in _NOT_FOUND_MARKERS):
        raise TitsaStopNotFoundError(stop_id)

    # Extract stop name & optional verified stop ID from page heading
    stop_name_raw: str | None = None
    header_match = _STOP_HEADER_RE.search(plain_text)
    if header_match:
        stop_name_raw = header_match.group(1).strip()
    else:
        corresp_match = _CORRESP_HEADER_RE.search(plain_text)
        if corresp_match:
            stop_name_raw = corresp_match.group(1).strip()

    # Extract exact server estimation timestamp in Atlantic/Canary timezone
    estimation_from_server = False
    est_match = _ESTIMATION_RE.search(plain_text)
    if est_match:
        day, month, year, hour, minute, second = (
            int(est_match.group(1)),
            int(est_match.group(2)),
            int(est_match.group(3)),
            int(est_match.group(4)),
            int(est_match.group(5)),
            int(est_match.group(6) or 0),
        )
        try:
            estimation_time = datetime(year, month, day, hour, minute, second, tzinfo=CANARY_TZ)
            estimation_from_server = True
        except ValueError as err:
            raise TitsaParseError(
                f"Invalid estimation timestamp for stop {stop_id}: {err}"
            ) from err
    else:
        ref = now if now is not None else datetime.now(CANARY_TZ)
        estimation_time = ref.astimezone(CANARY_TZ) if ref.tzinfo else ref.replace(tzinfo=CANARY_TZ)

    found_table, arrivals = _extract_arrivals_table(parser.tables, estimation_time)

    # Validate that this page is actually a TITSA stop page
    has_no_buses_notice = any(marker in norm_text for marker in _NO_BUSES_MARKERS)
    if not found_table and not estimation_from_server and not stop_name_raw:
        raise TitsaParseError(
            f"Unexpected HTML structure for stop {stop_id}: missing header, table, and estimation"
        )

    # If stop heading literally had an empty name like "INFORMACIÓN PARADA  (99999)" with no buses
    if (
        stop_name_raw is None
        and not arrivals
        and not found_table
        and not has_no_buses_notice
        and f"({stop_id})" not in plain_text
    ):
        raise TitsaStopNotFoundError(stop_id)

    stop_name = clean_titsa_stop_name(stop_name_raw) if stop_name_raw else f"Stop {stop_id}"
    return StopArrivals(
        stop_id=str(stop_id).strip(),
        stop_name=stop_name,
        estimation_time=estimation_time,
        arrivals=tuple(arrivals),
        estimation_from_server=estimation_from_server,
    )
