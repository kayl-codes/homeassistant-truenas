"""Tests that the translation files stay in parity.

* ``strings.json`` (the source) and ``translations/en.json`` must be identical:
  a custom integration loads ``translations/<lang>.json`` at runtime and never
  reads ``strings.json``, so a key added only to ``strings.json`` shows up as
  missing UI text. HA does not resolve ``[%key:...%]`` references for custom
  integrations at runtime either, so ``en.json`` must not contain any.
* Every other locale must stay in parity with ``en.json``.

The locale checks mirror the ``.github/validate_translations.py`` CI check at
the unit-test level (no Home Assistant import needed), so a drifting locale
fails fast both in CI and locally.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

_INTEGRATION_DIR = ("custom_components", "truenas_ce")
_KEY_REFERENCE_MARKER = "[%key:"
_EN_JSON = "en.json"


def _load(translations_dir: Path, name: str) -> dict[str, object]:
    return json.loads((translations_dir / name).read_text(encoding="utf-8"))


def _flatten(translations_dir: Path, vt: ModuleType, name: str) -> dict[str, object]:
    return vt._flatten(_load(translations_dir, name))


def test_all_locales_in_parity_with_en(repo_root: Path, vt: ModuleType) -> None:
    """No locale may have missing/obsolete keys or placeholder drift vs en.json."""
    translations_dir = repo_root.joinpath(*_INTEGRATION_DIR, "translations")
    reference = _flatten(translations_dir, vt, _EN_JSON)
    locales = [
        p.name for p in sorted(translations_dir.glob("*.json")) if p.name != _EN_JSON
    ]
    assert locales, "expected at least one non-English locale file"
    for name in locales:
        errors = vt._validate(_flatten(translations_dir, vt, name), reference)
        assert errors == [], f"{name} is out of parity with en.json: {errors}"


def _canonical(value: object) -> str:
    """Serialize JSON canonically, so True/1 and dotted/nested keys differ.

    Python treats ``True == 1`` (also inside lists), and flattening maps a
    literal ``"a.b"`` key and a nested ``{"a": {"b": ...}}`` to the same path.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _same_json_value(left: object, right: object) -> bool:
    return _canonical(left) == _canonical(right)


def _strings_vs_en_diff(
    strings: dict[str, object], english: dict[str, object]
) -> list[str]:
    """List every key path that differs between strings.json and en.json."""
    errors = [f"missing in en.json: {key}" for key in sorted(strings.keys() - english)]
    errors += [f"only in en.json: {key}" for key in sorted(english.keys() - strings)]
    errors += [
        f"value differs: {key}: strings.json={strings[key]!r}, en.json={english[key]!r}"
        for key in sorted(strings.keys() & english)
        if not _same_json_value(strings[key], english[key])
    ]
    return errors


def test_en_json_matches_strings_json(repo_root: Path, vt: ModuleType) -> None:
    """en.json must equal strings.json key-for-key and value-for-value."""
    integration_dir = repo_root.joinpath(*_INTEGRATION_DIR)
    strings = _flatten(integration_dir, vt, "strings.json")
    english = _flatten(integration_dir / "translations", vt, _EN_JSON)
    errors = _strings_vs_en_diff(strings, english)
    assert errors == [], "en.json drifted from strings.json:\n" + "\n".join(errors)
    assert _same_json_value(
        _load(integration_dir, "strings.json"),
        _load(integration_dir / "translations", _EN_JSON),
    ), "en.json nests keys differently from strings.json (dotted key vs object)"
    unresolved = sorted(
        key
        for key, value in english.items()
        if isinstance(value, str) and _KEY_REFERENCE_MARKER in value
    )
    assert unresolved == [], (
        f"unresolved [%key:...%] references in en.json: {unresolved}"
    )


def test_strings_vs_en_diff_reports_key_paths() -> None:
    """The strings.json/en.json diff must name every differing key path."""
    title = "config.step.user.title"
    strings = {
        title: "Setup",
        "config.abort.gone": "Gone",
        "x.flag": True,
        "x.list": [True],
    }
    english = {
        title: "Set up",
        "options.extra": "Extra",
        "x.flag": 1,
        "x.list": [1],
    }
    assert _strings_vs_en_diff(strings, english) == [
        "missing in en.json: config.abort.gone",
        "only in en.json: options.extra",
        f"value differs: {title}: strings.json='Setup', en.json='Set up'",
        "value differs: x.flag: strings.json=True, en.json=1",
        "value differs: x.list: strings.json=[True], en.json=[1]",
    ]


def test_canonical_json_distinguishes_dotted_and_nested_keys() -> None:
    """A literal dotted key must not compare equal to the nested object."""
    assert not _same_json_value({"a.b": "x"}, {"a": {"b": "x"}})
    assert _same_json_value({"a": {"b": "x"}, "c": 1}, {"c": 1, "a": {"b": "x"}})


def test_validator_detects_drift(vt: ModuleType) -> None:
    """The validator must flag missing keys, obsolete keys and placeholder drift."""
    reference = {"a": "x", "b": "{count} items"}
    broken = {"b": "keine Platzhalter", "c": "extra"}
    errors = vt._validate(broken, reference)
    assert any("missing key: a" in e for e in errors)
    assert any("obsolete key: c" in e for e in errors)
    assert any("placeholder mismatch in b" in e for e in errors)
