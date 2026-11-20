"""Text, line-code, and HTTP header utilities for TITSA."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import re
import unicodedata

_SPANISH_PARTICLES = frozenset(
    {"de", "del", "la", "las", "los", "el", "y", "e", "a", "al", "en", "con", "por", "lo"}
)

_PRESERVE_UPPER = frozenset({"TFS", "TFN", "ULL", "IES", "CEIP", "S/C", "TITSA", "GC", "TF", "(T)"})

_LINE_NUMERIC_PREFIX_RE = re.compile(r"^0*(\d+)([A-Za-z]*)$")
_WORD_LEAD_RE = re.compile(r'^([("\'\-/]*)([^\W\d_])(.*)$', re.UNICODE)


def normalize_text(text: str) -> str:
    """Return lowercase, accent-free, single-spaced text for fuzzy search."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def normalize_line_code(raw: str) -> str:
    """Normalize a TITSA line code (e.g. '015' -> '15', '238' -> '238', '030a' -> '30A')."""
    cleaned = raw.strip().upper()
    if not cleaned:
        return ""
    match = _LINE_NUMERIC_PREFIX_RE.match(cleaned)
    if match:
        digits, suffix = match.groups()
        # Keep '0' if the line were literally '0' or '000'
        num_part = digits.lstrip("0") or "0"
        return f"{num_part}{suffix.upper()}"
    return cleaned


def line_sort_key(line: str) -> tuple[int, int, str]:
    """Natural sort key for TITSA line codes (10 < 14 < 126 < 238 < 936 < 'A1')."""
    normalized = normalize_line_code(line)
    match = re.match(r"^(\d+)(.*)$", normalized)
    if match:
        return (0, int(match.group(1)), match.group(2))
    return (1, 0, normalized)


def clean_titsa_stop_name(raw: str) -> str:
    """Clean ALL-CAPS TITSA stop names into readable Title Case while preserving clarity."""
    collapsed = " ".join(raw.split()).strip()
    if not collapsed:
        return ""
    # Normalize dotted abbreviations without space like "STA.CRUZ" -> "STA. CRUZ"
    collapsed = re.sub(r"\.(?=[^\s.)])", ". ", collapsed)
    if collapsed != collapsed.upper():
        return collapsed

    words = collapsed.split(" ")
    result: list[str] = []
    for idx, token in enumerate(words):
        if token in _PRESERVE_UPPER:
            result.append(token)
            continue
        lower = token.lower()
        match = _WORD_LEAD_RE.match(lower)
        if not match:
            result.append(token)
            continue
        prefix, first_char, rest = match.groups()
        word_core = first_char + rest
        if idx > 0 and not prefix and word_core in _SPANISH_PARTICLES:
            result.append(word_core)
        else:
            result.append(f"{prefix}{first_char.upper()}{rest}")
    return " ".join(result)


def parse_retry_after(header_value: str | None) -> float | None:
    """Parse an HTTP Retry-After header (seconds or HTTP-date) into delay seconds."""
    if not header_value:
        return None
    stripped = header_value.strip()
    if not stripped:
        return None
    try:
        seconds = float(stripped)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(stripped)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        delta = (target - datetime.now(UTC)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None
