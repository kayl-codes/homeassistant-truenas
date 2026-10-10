"""Unit tests for sensor_types.py's HA-version compatibility switch."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import homeassistant.const as ha_const
import pytest

from custom_components.truenas_ce import sensor_types

_PATH = Path(sensor_types.__file__)
_COMPAT_NAME = "custom_components.truenas_ce._sensor_types_compat"


def test_percentage_unit_uses_unit_of_ratio_when_available() -> None:
    assert sensor_types.UNIT_PERCENTAGE is ha_const.UnitOfRatio.PERCENTAGE


def test_percentage_unit_falls_back_before_unit_of_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA < 2026.7 has no ``UnitOfRatio``: the module must still import and
    use the plain "%" unit (loaded as a separate copy, so the real module and
    its description classes stay untouched)."""
    expected_keys = _percent_keys(sensor_types.SENSOR_TYPES)
    monkeypatch.delattr(ha_const, "UnitOfRatio")
    spec = importlib.util.spec_from_file_location(_COMPAT_NAME, _PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, _COMPAT_NAME, module)
    spec.loader.exec_module(module)

    # UnitOfRatio.PERCENTAGE is a StrEnum equal to "%", so only the exact type
    # proves the except branch ran.
    assert type(module.UNIT_PERCENTAGE) is str
    assert module.UNIT_PERCENTAGE == "%"
    assert expected_keys
    assert _percent_keys(module.SENSOR_TYPES) == expected_keys


def _percent_keys(descriptions: tuple[Any, ...]) -> set[str]:
    return {desc.key for desc in descriptions if desc.native_unit_of_measurement == "%"}
