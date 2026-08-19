"""Shared pytest fixtures for the TrueNAS integration test suite.

Several test modules exercise standalone files (``apiparser.py``, the CI
translation validator) that are not installable packages and would otherwise
require ``homeassistant``/``aiotruenas`` to be importable. Loading them by
file path is centralized here so the repo layout is only encoded once, and
failures surface as an explicit ``ImportError`` at fixture setup rather than
a bare ``assert`` at module-import time.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# test_config_flow_flows.py needs pytest-homeassistant-custom-component (the
# hass/enable_custom_integrations fixtures, MockConfigEntry), which is only
# installed in CI -- its pytest11 plugin unconditionally imports the
# POSIX-only fcntl module and crashes every local pytest run on this repo's
# Windows dev machine, so it must never be installed there.
collect_ignore_glob: list[str] = (
    ["test_config_flow_flows.py", "test_services.py", "test_entity_setup.py"]
    if importlib.util.find_spec("pytest_homeassistant_custom_component") is None
    else []
)


def load_module_from_path(name: str, path: Path) -> ModuleType:
    """Load a standalone module by file path.

    Registers the module in ``sys.modules`` before executing it, since
    ``dataclasses`` (used with ``from __future__ import annotations``) resolves
    string annotations via a ``sys.modules`` lookup of the class's own module
    and raises ``AttributeError`` otherwise.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def ap() -> ModuleType:
    """The ``custom_components.truenas_ce.apiparser`` module under test."""
    path = REPO_ROOT / "custom_components" / "truenas_ce" / "apiparser.py"
    return load_module_from_path("apiparser", path)


@pytest.fixture(scope="session")
def vt() -> ModuleType:
    """The ``.github/validate_translations.py`` script under test."""
    path = REPO_ROOT / ".github" / "validate_translations.py"
    return load_module_from_path("validate_translations", path)


@pytest.fixture(scope="session")
def ep() -> ModuleType:
    """The ``custom_components.truenas_ce.event_push`` module under test."""
    path = REPO_ROOT / "custom_components" / "truenas_ce" / "event_push.py"
    return load_module_from_path("event_push", path)
