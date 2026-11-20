"""Validation tests for manifest.json, hacs.json, translations, and brand assets."""

from __future__ import annotations

import json
from pathlib import Path
import struct
import tomllib

from custom_components.titsa.api.provider import DEFAULT_USER_AGENT
from custom_components.titsa.const import DOMAIN, VERSION

REPO_ROOT = Path(__file__).parent.parent
INTEGRATION_ROOT = REPO_ROOT / "custom_components" / DOMAIN
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _flatten_keys(data: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys |= _flatten_keys(value, path)
        else:
            keys.add(path)
    return keys


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == PNG_SIGNATURE
    assert header[12:16] == b"IHDR"
    width, height = struct.unpack(">II", header[16:24])
    return (width, height)


def test_manifest_keys_order_and_version_parity() -> None:
    """Ensure manifest.json follows hassfest ordering and version matches across project."""
    manifest = json.loads((INTEGRATION_ROOT / "manifest.json").read_text(encoding="utf-8"))
    keys = list(manifest.keys())

    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
    assert manifest["domain"] == DOMAIN
    assert manifest["version"] == VERSION
    assert VERSION in DEFAULT_USER_AGENT

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == VERSION


def test_translations_parity_and_no_core_key_refs() -> None:
    """Ensure en.json and es.json match 1:1 with zero Home Assistant Core [%key:...] refs."""
    en_raw = (INTEGRATION_ROOT / "translations" / "en.json").read_text(encoding="utf-8")
    es_raw = (INTEGRATION_ROOT / "translations" / "es.json").read_text(encoding="utf-8")

    assert "[%key:" not in en_raw
    assert "[%key:" not in es_raw

    en = json.loads(en_raw)
    es = json.loads(es_raw)
    assert _flatten_keys(en) == _flatten_keys(es)


def test_brand_assets_meet_hassfest_rules() -> None:
    """Ensure brand icons and logos satisfy hassfest dimension and fallback rules."""
    brand_dir = INTEGRATION_ROOT / "brand"
    expected = {
        "icon.png": (256, 256),
        "icon@2x.png": (512, 512),
        "logo.png": (512, 192),
        "logo@2x.png": (1024, 384),
        "dark_logo.png": (512, 192),
        "dark_logo@2x.png": (1024, 384),
    }
    for filename, expected_size in expected.items():
        path = brand_dir / filename
        assert path.is_file(), f"Missing brand asset: {filename}"
        assert _png_dimensions(path) == expected_size

    # Dark variants must differ from light variants (hassfest forbids duplicate fallbacks)
    assert (brand_dir / "logo.png").read_bytes() != (brand_dir / "dark_logo.png").read_bytes()
    assert (brand_dir / "logo@2x.png").read_bytes() != (brand_dir / "dark_logo@2x.png").read_bytes()


def test_hacs_json_valid() -> None:
    """Verify hacs.json contains required HACS fields."""
    hacs = json.loads((REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == "TITSA"
    assert hacs["country"] == ["ES"]
    assert hacs["render_readme"] is True
