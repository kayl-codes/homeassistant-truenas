"""Tests that every translation locale stays in parity with ``en.json``.

These mirror the ``.github/validate_translations.py`` CI check at the unit-test
level (no Home Assistant import needed), so a drifting locale fails fast both in
CI and locally.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TRANSLATIONS = _REPO / "custom_components" / "truenas_ce" / "translations"

_spec = importlib.util.spec_from_file_location(
    "validate_translations", _REPO / ".github" / "validate_translations.py"
)
assert _spec and _spec.loader
vt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vt)


def _flatten(name: str) -> dict[str, object]:
    return vt._flatten(json.loads((_TRANSLATIONS / name).read_text(encoding="utf-8")))


def test_all_locales_in_parity_with_en() -> None:
    """No locale may have missing/obsolete keys or placeholder drift vs en.json."""
    reference = _flatten("en.json")
    locales = [
        p.name for p in sorted(_TRANSLATIONS.glob("*.json")) if p.name != "en.json"
    ]
    assert locales, "expected at least one non-English locale file"
    for name in locales:
        assert vt._validate(_flatten(name), reference) == [], (
            f"{name} is out of parity with en.json"
        )


def test_validator_detects_drift() -> None:
    """The validator must flag missing keys, obsolete keys and placeholder drift."""
    reference = {"a": "x", "b": "{count} items"}
    broken = {"b": "keine Platzhalter", "c": "extra"}
    errors = vt._validate(broken, reference)
    assert any("missing key: a" in e for e in errors)
    assert any("obsolete key: c" in e for e in errors)
    assert any("placeholder mismatch in b" in e for e in errors)
