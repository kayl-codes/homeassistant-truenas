"""Validate that all translation locale files stay in parity with ``en.json``.

For every ``custom_components/truenas_ce/translations/<lang>.json`` this checks,
against the English source ``en.json``:

  * no **missing** keys,
  * no **obsolete/extra** keys,
  * the same set of ``{placeholder}`` tokens per string (e.g. ``{count}``,
    ``{port}``) so translations can't silently drop a placeholder.

Exits non-zero with a per-file report on any drift, so CI fails fast when a
future translation update gets out of sync.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TRANSLATIONS_DIR = Path("custom_components/truenas_ce/translations")
REFERENCE = "en.json"
_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")


def _flatten(data: dict, prefix: str = "") -> dict[str, object]:
    """Flatten a nested dict into ``a.b.c`` -> value pairs."""
    items: dict[str, object] = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            items.update(_flatten(value, full))
        else:
            items[full] = value
    return items


def _placeholders(value: object) -> set[str]:
    """Return the set of ``{placeholder}`` tokens in a string value."""
    return set(_PLACEHOLDER_RE.findall(value)) if isinstance(value, str) else set()


def _validate(locale: dict[str, object], reference: dict[str, object]) -> list[str]:
    """Return a list of human-readable problems for one locale file."""
    ref_keys, loc_keys = set(reference), set(locale)
    errors = [f"missing key: {key}" for key in sorted(ref_keys - loc_keys)]
    errors += [f"obsolete key: {key}" for key in sorted(loc_keys - ref_keys)]
    for key in sorted(ref_keys & loc_keys):
        ref_ph, loc_ph = _placeholders(reference[key]), _placeholders(locale[key])
        if ref_ph != loc_ph:
            errors.append(
                f"placeholder mismatch in {key}: "
                f"expected {sorted(ref_ph)}, got {sorted(loc_ph)}"
            )
    return errors


def main() -> None:
    """Validate every locale file and exit non-zero on any drift."""
    reference = _flatten(
        json.loads((TRANSLATIONS_DIR / REFERENCE).read_text(encoding="utf-8"))
    )

    failed = False
    for path in sorted(TRANSLATIONS_DIR.glob("*.json")):
        if path.name == REFERENCE:
            continue
        locale = _flatten(json.loads(path.read_text(encoding="utf-8")))
        errors = _validate(locale, reference)
        if errors:
            failed = True
            print(f"FAIL {path.name}")
            for error in errors:
                print(f"    - {error}")
        else:
            print(f"OK   {path.name} ({len(locale)} keys)")

    if failed:
        print(f"\nTranslation validation failed against {REFERENCE}.", file=sys.stderr)
        sys.exit(1)
    print(f"\nAll locale files are in parity with {REFERENCE}.")


if __name__ == "__main__":
    main()
