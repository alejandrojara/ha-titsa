"""Zero-dependency HTML parser for titsa.com/correspondencias.php."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
import re
from zoneinfo import ZoneInfo

from .exceptions import TitsaParseError, TitsaStopNotFoundError
from .models import BusArrival, StopArrivals
from .utils import clean_titsa_stop_name, normalize_line_code, normalize_text

CANARY_TZ = ZoneInfo("Atlantic/Canary")

_STOP_HEADER_RE = re.compile(
    r"informaci[oó]n\s+parada\s+([^\n()]+?)\s*\(\s*(\d+)\s*\)",
    re.IGNORECASE,
)
_EMPTY_STOP_HEADER_RE = re.compile(
    r"informaci[oó]n\s+parada\s*\(\s*(\d+)\s*\)",
    re.IGNORECASE,
)
_CORRESP_HEADER_RE = re.compile(
    r"correspondencias\s+en\s+([^\n]+)",
    re.IGNORECASE,
)
_ESTIMATION_RE = re.compile(
    r"estimaci[oó]n\s*:\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?",
    re.IGNORECASE,
)
_CLOCK_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_HOURS_MINUTES_RE = re.compile(
    r"(\d+)\s*(?:h(?:oras?)?)\s*(?:(\d+)\s*(?:m(?:in(?:utos?)?)?))?",
    re.IGNORECASE,
)
_MINUTES_RE = re.compile(r"([<>+])?\s*(\d+)\s*(?:min(?:utos?)?|m\b)?", re.IGNORECASE)
_LINE_CELL_HINT_RE = re.compile(
    r"^(?:l[ií]nea\s+)?\d{1,4}[A-Za-z]?(?:\s*[-–—:]|\s*$)", re.IGNORECASE
)
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


@dataclass(slots=True)
class _TableState:
    """Mutable parse state for a single HTML table (supports nested tables and unclosed tags)."""

    rows: list[list[tuple[bool, str]]] = field(default_factory=list)
    current_row: list[tuple[bool, str]] | None = None
    current_cell_header: bool = False
    current_cell_chunks: list[str] | None = None

    def flush_cell(self) -> None:
        """Close and append the currently open cell, if any."""
        if self.current_cell_chunks is None:
            return
        if self.current_row is None:
            self.current_row = []
        cell_text = " ".join("".join(self.current_cell_chunks).split())
        self.current_row.append((self.current_cell_header, cell_text))
        self.current_cell_chunks = None

    def flush_row(self) -> None:
        """Close any open cell and append the current row, if non-empty."""
        self.flush_cell()
        if self.current_row:
            self.rows.append(self.current_row)
        self.current_row = None


class _CorrespondenciasHTMLParser(HTMLParser):
    """Collect HTML tables (handling nested tables and unclosed td/th/tr) and visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._table_stack: list[_TableState] = []
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
            self._table_stack.append(_TableState())
        elif tag_lower == "tr" and self._table_stack:
            table = self._table_stack[-1]
            # HTML5 implicit close of previous <tr> if unclosed
            table.flush_row()
            table.current_row = []
            self._text_parts.append("\n")
        elif tag_lower in ("td", "th") and self._table_stack:
            table = self._table_stack[-1]
            if table.current_row is None:
                table.current_row = []
            # HTML5 implicit close of previous <td>/<th> in same row (prevents LIFO inversion!)
            table.flush_cell()
            table.current_cell_header = tag_lower == "th"
            table.current_cell_chunks = []
        elif tag_lower in ("br", "p", "div", "h1", "h2", "h3", "h4", "li"):
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("script", "style", "noscript"):
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if tag_lower in ("td", "th") and self._table_stack:
            self._table_stack[-1].flush_cell()
        elif tag_lower == "tr" and self._table_stack:
            self._table_stack[-1].flush_row()
        elif tag_lower == "table" and self._table_stack:
            table = self._table_stack.pop()
            table.flush_row()
            if table.rows:
                self.tables.append(table.rows)
        elif tag_lower in ("p", "div", "h1", "h2", "h3", "h4", "li"):
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._table_stack and self._table_stack[-1].current_cell_chunks is not None:
            self._table_stack[-1].current_cell_chunks.append(data)
        self._text_parts.append(data)

    def close(self) -> None:
        """Flush any unclosed tables at EOF."""
        super().close()
        while self._table_stack:
            table = self._table_stack.pop()
            table.flush_row()
            if table.rows:
                self.tables.append(table.rows)

    @property
    def plain_text(self) -> str:
        """Return newline-separated visible text."""
        raw = "".join(self._text_parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _parse_minutes(
    raw_time: str,
    estimation_time: datetime | None = None,
) -> tuple[int, bool] | None:
    """Parse arrival ETA text into (minutes, is_approximate)."""
    text = " ".join(raw_time.split()).strip()
    if not text:
        return None
    norm = normalize_text(text)

    if any(marker in norm for marker in ("en parada", "llegando", "inminente", "< 1", "<1")):
        return (0, False)

    # Handle explicit HH:MM scheduled clock time without misreading HH as minutes
    clock_match = _CLOCK_TIME_RE.match(text)
    if clock_match:
        if estimation_time is None:
            return None
        hour, minute = int(clock_match.group(1)), int(clock_match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        candidate = estimation_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate < estimation_time - timedelta(minutes=2):
            candidate += timedelta(days=1)
        delta_minutes = max(0, int((candidate - estimation_time).total_seconds() // 60))
        return (delta_minutes, False)

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

    cleaned = re.sub(r"^l[ií]nea\s+", "", cleaned, flags=re.IGNORECASE)

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


def _detect_column_indices(row: list[tuple[bool, str]]) -> tuple[int, int, int]:
    """Infer (time_idx, line_idx, dest_idx) from a header row."""
    time_idx, line_idx, dest_idx = 0, 1, 2
    for idx, (_, text) in enumerate(row):
        norm = normalize_text(text)
        if "tiempo" in norm or "llegada" in norm:
            time_idx = idx
        elif "linea" in norm:
            line_idx = idx
        elif "destino" in norm:
            dest_idx = idx
    return (time_idx, line_idx, dest_idx)


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
        time_idx, line_idx, dest_idx = 0, 1, 2

        for row in table:
            if _is_header_row(row):
                found_correspondencias_table = True
                time_idx, line_idx, dest_idx = _detect_column_indices(row)
                continue
            texts = [text for _, text in row]
            if len(texts) < 3 or max(time_idx, line_idx, dest_idx) >= len(texts):
                continue
            parsed_time = _parse_minutes(texts[time_idx], estimation_time)
            if parsed_time is None:
                continue
            minutes, is_approx = parsed_time

            line_raw = texts[line_idx]
            dest_raw = texts[dest_idx]
            # Defense in depth: if line_raw doesn't start with a line code while dest_raw does, swap
            if not _LINE_CELL_HINT_RE.match(line_raw.strip()) and _LINE_CELL_HINT_RE.match(
                dest_raw.strip()
            ):
                line_raw, dest_raw = dest_raw, line_raw

            line_code, line_name = _parse_line_cell(line_raw)
            if not line_code:
                continue
            destination = " ".join(dest_raw.split()).strip()
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


def _resolve_12h_estimation_time(parsed: datetime, ref_now: datetime) -> datetime:
    """Resolve TITSA PHP's 12-hour clock bug ('h' instead of 'H' without AM/PM).

    titsa.com/correspondencias.php prints 01:00..12:59 for both AM and PM
    (e.g. '01:55:23' at 13:55:23, or '12:15:00' at 00:15:00). If the parsed
    hour is in 1..12 and is >6h away from ref_now while a ±12h shift lands
    within ±30 min of ref_now, apply the shift. Historical fixtures or future
    24h server timestamps remain untouched.
    """
    if not (1 <= parsed.hour <= 12):
        return parsed

    base_delta = abs((parsed - ref_now).total_seconds())
    if base_delta <= 6 * 3600:
        return parsed

    candidates = (
        parsed + timedelta(hours=12),
        parsed - timedelta(hours=12),
    )
    best = min(candidates, key=lambda dt: abs((dt - ref_now).total_seconds()))
    if abs((best - ref_now).total_seconds()) <= 30 * 60:
        return best
    return parsed


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
    has_empty_stop_header = False
    header_match = _STOP_HEADER_RE.search(plain_text)
    if header_match:
        stop_name_raw = header_match.group(1).strip()
    elif _EMPTY_STOP_HEADER_RE.search(plain_text):
        has_empty_stop_header = True
    else:
        corresp_match = _CORRESP_HEADER_RE.search(plain_text)
        if corresp_match:
            stop_name_raw = corresp_match.group(1).strip()

    ref = now if now is not None else datetime.now(CANARY_TZ)
    ref_now = ref.astimezone(CANARY_TZ) if ref.tzinfo else ref.replace(tzinfo=CANARY_TZ)

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
            raw_estimation = datetime(year, month, day, hour, minute, second, tzinfo=CANARY_TZ)
            estimation_time = _resolve_12h_estimation_time(raw_estimation, ref_now)
            estimation_from_server = True
        except ValueError as err:
            raise TitsaParseError(
                f"Invalid estimation timestamp for stop {stop_id}: {err}"
            ) from err
    else:
        estimation_time = ref_now

    found_table, arrivals = _extract_arrivals_table(parser.tables, estimation_time)

    # Detect TITSA PHP response for unknown stop IDs ("INFORMACIÓN PARADA  (99999)" with no buses)
    if has_empty_stop_header and not arrivals:
        raise TitsaStopNotFoundError(stop_id)

    # Validate that this page is actually a TITSA stop page
    has_no_buses_notice = any(marker in norm_text for marker in _NO_BUSES_MARKERS)
    if not found_table and not estimation_from_server and not stop_name_raw:
        raise TitsaParseError(
            f"Unexpected HTML structure for stop {stop_id}: missing header, table, and estimation"
        )

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
