"""Every entity description's translation_key must resolve to a name and an icon.

For each ``SENSOR_TYPES`` entry of every ``<platform>_types.py`` module, the
description's ``translation_key`` must exist under ``entity.<platform>.<key>``
with a ``name`` in ``strings.json`` and ``translations/en.json`` and with a
``default`` icon in ``icons.json``. Entity classes that set a fixed
``_attr_translation_key`` in ``<platform>.py`` are checked the same way. A
missing entry silently degrades the entity name/icon at runtime, so this local
guard catches it before review (origin: PR #172 test-analyzer finding).

Known, intentional gaps live in the explicit allowlists below, each with a
reason. An allowlisted entry that is no longer missing fails the test, so the
allowlists cannot rot.
"""

from __future__ import annotations

import importlib
import inspect
import json
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from custom_components.truenas_ce import sensor_types
from custom_components.truenas_ce.const import PLATFORMS

_PACKAGE_DIR = Path(sensor_types.__file__).parent
_PACKAGE = "custom_components.truenas_ce"
_TYPES_SUFFIX = "_types"

_STRINGS = "strings.json"
_EN = "translations/en.json"
_ICONS = "icons.json"

_SENSOR = "sensor"
_BINARY_SENSOR = "binary_sensor"
_UPDATE = "update"
# Every platform the integration forwards must contribute descriptions.
_PLATFORMS = {platform.value for platform in PLATFORMS}

# Reasons shared by several allowlist entries.
_DATA_NAMED = (
    'name="" + data_name: named from the API object; translation_key only '
    "selects the icon"
)
_DEVICE_NAMED = "name=None: takes the device name, no translation_key"
_DEVICE_CLASS_ICON = "no translation_key: icon comes from the device class"

# (platform, description key) -> reason the name translation is absent from
# both strings.json and en.json.
_NAME_ALLOWLIST: dict[tuple[str, str], str] = {
    (_SENSOR, "dataset"): _DATA_NAMED,
    (_SENSOR, "disk"): 'name="" + data_name: named from the disk, no translation_key',
    (_SENSOR, "cloudsync"): _DATA_NAMED,
    (_SENSOR, "replication"): _DATA_NAMED,
    (_SENSOR, "rsynctask"): _DATA_NAMED,
    (_SENSOR, "snapshottask"): _DATA_NAMED,
    (_BINARY_SENSOR, "vm"): _DATA_NAMED,
    (_BINARY_SENSOR, "container"): _DATA_NAMED,
    (_BINARY_SENSOR, "service"): _DATA_NAMED,
    (_BINARY_SENSOR, "app"): _DATA_NAMED,
    (_BINARY_SENSOR, "directoryservices"): _DATA_NAMED,
    ("switch", "service_switch"): _DATA_NAMED,
    (_UPDATE, "system_update"): _DEVICE_NAMED,
    (_UPDATE, "app_update"): _DEVICE_NAMED,
}

# (platform, description key) -> reason the icons.json entry is absent.
_ICON_ALLOWLIST: dict[tuple[str, str], str] = {
    (_SENSOR, "disk"): _DEVICE_CLASS_ICON + " (temperature)",
    (_BINARY_SENSOR, "disk_issues"): "device class PROBLEM supplies the icon",
    (_BINARY_SENSOR, "interface"): "device class CONNECTIVITY supplies the icon",
    (_UPDATE, "system_update"): _DEVICE_CLASS_ICON + " (update platform)",
    (_UPDATE, "app_update"): _DEVICE_CLASS_ICON + " (update platform)",
}


@cache
def _load_entity_section(relative: str) -> dict[str, Any]:
    data = json.loads((_PACKAGE_DIR / relative).read_text(encoding="utf-8"))
    return data.get("entity", {})


def _types_modules() -> list[tuple[str, ModuleType]]:
    modules = []
    for path in sorted(_PACKAGE_DIR.glob(f"*{_TYPES_SUFFIX}.py")):
        platform = path.stem.removesuffix(_TYPES_SUFFIX)
        modules.append((platform, importlib.import_module(f"{_PACKAGE}.{path.stem}")))
    return modules


_DESCRIPTIONS: list[tuple[str, Any]] = [
    (platform, desc)
    for platform, module in _types_modules()
    for desc in module.SENSOR_TYPES
]
_DESCRIPTION_IDS = [f"{platform}.{desc.key}" for platform, desc in _DESCRIPTIONS]
_KNOWN_KEYS = {(platform, desc.key) for platform, desc in _DESCRIPTIONS}


def _class_translation_keys() -> list[tuple[str, str]]:
    """(platform, key) for entity classes with a fixed _attr_translation_key."""
    keys = set()
    for platform, _ in _types_modules():
        module = importlib.import_module(f"{_PACKAGE}.{platform}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            # HA's CachedProperties metaclass turns _attr_* into a property and
            # keeps the class value under __attr_*.
            attrs = vars(cls)
            key = attrs.get(
                "__attr_translation_key", attrs.get("_attr_translation_key")
            )
            if cls.__module__ == module.__name__ and isinstance(key, str):
                keys.add((platform, key))
    return sorted(keys)


_CLASS_KEYS = _class_translation_keys()


def _field(relative: str) -> str:
    return "default" if relative == _ICONS else "name"


def _has_entry(relative: str, platform: str, key: str | None) -> bool:
    """True if the entry's name/default icon is a non-blank string."""
    entry = _load_entity_section(relative).get(platform, {}).get(key)
    if not isinstance(entry, dict):
        return False
    value = entry.get(_field(relative))
    return isinstance(value, str) and bool(value.strip())


def _check(
    relative: str, allowlist: dict[tuple[str, str], str], platform: str, desc: Any
) -> None:
    present = _has_entry(relative, platform, desc.translation_key)
    entry = f"entity.{platform}.{desc.translation_key}.{_field(relative)} in {relative}"
    reason = allowlist.get((platform, desc.key))
    if reason is None:
        assert present, f"{platform}.{desc.key}: missing {entry}"
    else:
        assert not present, (
            f"{platform}.{desc.key}: stale allowlist entry ({reason}) -- "
            f"{entry} now exists, remove it from the allowlist"
        )


def test_every_platform_contributes_descriptions() -> None:
    platforms = {platform for platform, _ in _DESCRIPTIONS}
    assert _PLATFORMS <= platforms


def test_class_translation_keys_discovered() -> None:
    assert _CLASS_KEYS, "no _attr_translation_key found in any platform module"


@pytest.mark.parametrize("relative", [_STRINGS, _EN])
@pytest.mark.parametrize(("platform", "desc"), _DESCRIPTIONS, ids=_DESCRIPTION_IDS)
def test_translation_key_has_name(relative: str, platform: str, desc: Any) -> None:
    _check(relative, _NAME_ALLOWLIST, platform, desc)


@pytest.mark.parametrize(("platform", "desc"), _DESCRIPTIONS, ids=_DESCRIPTION_IDS)
def test_translation_key_has_icon(platform: str, desc: Any) -> None:
    _check(_ICONS, _ICON_ALLOWLIST, platform, desc)


@pytest.mark.parametrize(
    "allowlist", [_NAME_ALLOWLIST, _ICON_ALLOWLIST], ids=["name", "icon"]
)
def test_allowlist_entries_reference_existing_descriptions(
    allowlist: dict[tuple[str, str], str],
) -> None:
    unknown = sorted(set(allowlist) - _KNOWN_KEYS)
    assert not unknown, f"allowlist entries for removed descriptions: {unknown}"


@pytest.mark.parametrize("relative", [_STRINGS, _EN, _ICONS])
@pytest.mark.parametrize(
    ("platform", "key"), _CLASS_KEYS, ids=[f"{p}.{k}" for p, k in _CLASS_KEYS]
)
def test_class_translation_key_resolves(relative: str, platform: str, key: str) -> None:
    assert _has_entry(relative, platform, key), (
        f"entity.{platform}.{key}.{_field(relative)} missing in {relative}"
    )


@pytest.mark.parametrize("value", ["", "   ", None, 1, ["x"]])
def test_has_entry_rejects_blank_or_non_string_values(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    """A present but unusable name/icon must not count as an entry."""
    section = {_SENSOR: {"k": {"name": value, "default": value}}}
    monkeypatch.setattr(f"{__name__}._load_entity_section", lambda _relative: section)
    assert not _has_entry(_STRINGS, _SENSOR, "k")
    assert not _has_entry(_ICONS, _SENSOR, "k")


def test_has_entry_accepts_non_blank_string(monkeypatch: pytest.MonkeyPatch) -> None:
    section = {_SENSOR: {"k": {"name": "Name", "default": "mdi:nas"}}}
    monkeypatch.setattr(f"{__name__}._load_entity_section", lambda _relative: section)
    assert _has_entry(_STRINGS, _SENSOR, "k")
    assert _has_entry(_ICONS, _SENSOR, "k")
