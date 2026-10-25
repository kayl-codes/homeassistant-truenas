"""Unit tests for the pure/self-contained helpers in helper.py."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.loader import IntegrationNotFound

from custom_components.truenas_ce import helper as helper_module
from custom_components.truenas_ce.helper import (
    async_legacy_code_installed,
    sanitize_host,
)


# ---------------------------
#   sanitize_host
# ---------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("truenas.local", "truenas.local"),
        ("  truenas.local  ", "truenas.local"),
        ("https://nas.example.com", "nas.example.com"),
        ("http://nas.example.com/ui?tab=1", "nas.example.com"),
        ("nas.example.com/ui", "nas.example.com"),
        ("nas.example.com?query=1", "nas.example.com"),
        ("nas.example.com#frag", "nas.example.com"),
        ("192.168.1.10", "192.168.1.10"),
        ("", ""),
        ("   ", ""),
        ("https://nas.example.com:8443/", "nas.example.com:8443"),
        ("NAS.Local", "nas.local"),
        ("HTTPS://NAS.Example.COM:8443/UI", "nas.example.com:8443"),
        ("2001:db8::1", "[2001:db8::1]"),
        ("2001:DB8::1", "[2001:db8::1]"),
        ("::1", "[::1]"),
        ("[2001:db8::1]", "[2001:db8::1]"),
        ("[2001:db8::1]:8443", "[2001:db8::1]:8443"),
    ],
)
def test_sanitize_host(raw: str, expected: str) -> None:
    assert sanitize_host(raw) == expected


def _executor_hass() -> MagicMock:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    return hass


async def test_legacy_code_installed_true_when_integration_resolves(
    tmp_path: Path,
) -> None:
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    hass = _executor_hass()
    with patch.object(
        helper_module,
        "async_get_integration",
        AsyncMock(return_value=SimpleNamespace(file_path=tmp_path)),
    ) as get_mock:
        assert await async_legacy_code_installed(hass) is True
    get_mock.assert_awaited_once_with(hass, "truenas")
    # The file check must not block the event loop.
    hass.async_add_executor_job.assert_awaited_once()


async def test_legacy_code_installed_false_when_files_removed_since_startup(
    tmp_path: Path,
) -> None:
    """The loader still resolves the cached integration after a live uninstall."""
    with patch.object(
        helper_module,
        "async_get_integration",
        AsyncMock(return_value=SimpleNamespace(file_path=tmp_path / "truenas")),
    ):
        assert await async_legacy_code_installed(_executor_hass()) is False


async def test_legacy_code_installed_false_when_integration_missing() -> None:
    """HACS-only uninstall: the files are gone, the legacy entry remains (#201)."""
    with patch.object(
        helper_module,
        "async_get_integration",
        AsyncMock(side_effect=IntegrationNotFound("truenas")),
    ):
        assert await async_legacy_code_installed(object()) is False
