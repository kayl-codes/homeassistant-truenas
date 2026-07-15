"""Tests that every translation locale stays in parity with ``en.json``.

These mirror the ``.github/validate_translations.py`` CI check at the unit-test
level (no Home Assistant import needed), so a drifting locale fails fast both in
CI and locally.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType


def _flatten(translations_dir: Path, vt: ModuleType, name: str) -> dict[str, object]:
    text = (translations_dir / name).read_text(encoding="utf-8")
    return vt._flatten(json.loads(text))


def test_all_locales_in_parity_with_en(repo_root: Path, vt: ModuleType) -> None:
    """No locale may have missing/obsolete keys or placeholder drift vs en.json."""
    translations_dir = repo_root / "custom_components" / "truenas_ce" / "translations"
    reference = _flatten(translations_dir, vt, "en.json")
    locales = [
        p.name for p in sorted(translations_dir.glob("*.json")) if p.name != "en.json"
    ]
    assert locales, "expected at least one non-English locale file"
    for name in locales:
        assert vt._validate(_flatten(translations_dir, vt, name), reference) == [], (
            f"{name} is out of parity with en.json"
        )


def test_validator_detects_drift(vt: ModuleType) -> None:
    """The validator must flag missing keys, obsolete keys and placeholder drift."""
    reference = {"a": "x", "b": "{count} items"}
    broken = {"b": "keine Platzhalter", "c": "extra"}
    errors = vt._validate(broken, reference)
    assert any("missing key: a" in e for e in errors)
    assert any("obsolete key: c" in e for e in errors)
    assert any("placeholder mismatch in b" in e for e in errors)
