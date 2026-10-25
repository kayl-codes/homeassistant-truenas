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
    async_legacy_integration_resolvable,
    pool_scrub_progress,
    pool_scrub_state,
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


async def test_legacy_integration_resolvable_while_cached_since_startup() -> None:
    """Uninstalled since startup: the loader still resolves it until a restart."""
    hass = object()
    with patch.object(
        helper_module,
        "async_get_integration",
        AsyncMock(return_value=SimpleNamespace(file_path=Path("gone"))),
    ) as get_mock:
        assert await async_legacy_integration_resolvable(hass) is True
    get_mock.assert_awaited_once_with(hass, "truenas")


async def test_legacy_integration_not_resolvable_when_missing_at_startup() -> None:
    with patch.object(
        helper_module,
        "async_get_integration",
        AsyncMock(side_effect=IntegrationNotFound("truenas")),
    ):
        assert await async_legacy_integration_resolvable(object()) is False


# ---------------------------
#   pool_scrub_state / pool_scrub_progress (#204)
# ---------------------------
_PAUSE_TIME = "2026-10-25T21:00:00Z"


@pytest.mark.parametrize(
    ("pool", "expected"),
    [
        ({"scrub_state": "SCANNING", "scrub_pause": None}, "SCANNING"),
        ({"scrub_state": "SCANNING", "scrub_pause": _PAUSE_TIME}, "PAUSED"),
        ({"scrub_state": "FINISHED", "scrub_pause": None}, "FINISHED"),
        # A stop clears the pause; a stale one must not turn CANCELED into PAUSED.
        ({"scrub_state": "CANCELED", "scrub_pause": _PAUSE_TIME}, "CANCELED"),
        ({"scrub_state": None}, None),
        ({}, None),
    ],
)
def test_pool_scrub_state(pool: dict, expected: str | None) -> None:
    assert pool_scrub_state(pool) == expected


@pytest.mark.parametrize(
    ("pool", "expected"),
    [
        ({"scrub_state": "SCANNING", "scrub_percentage": 11.300981044769287}, 11.3),
        (
            {
                "scrub_state": "SCANNING",
                "scrub_pause": _PAUSE_TIME,
                "scrub_percentage": 1.44,
            },
            1.4,
        ),
        # TrueNAS reports a finished scrub just below 100.
        ({"scrub_state": "FINISHED", "scrub_percentage": 99.91}, 100.0),
        ({"scrub_state": "FINISHED"}, 100.0),
        ({"scrub_state": "CANCELED", "scrub_percentage": 56.94}, 56.9),
        ({"scrub_state": "NONE", "scrub_percentage": 0}, None),
        ({"scrub_state": "SCANNING", "scrub_percentage": None}, None),
        ({"scrub_state": "SCANNING", "scrub_percentage": True}, None),
        ({}, None),
    ],
)
def test_pool_scrub_progress(pool: dict, expected: float | None) -> None:
    assert pool_scrub_progress({"scan_function": "SCRUB", **pool}) == expected


# aiotruenas reports "unknown" when a pool has no scan.
@pytest.mark.parametrize("scan_function", ["RESILVER", "unknown", None])
@pytest.mark.parametrize("scrub_state", ["SCANNING", "FINISHED"])
def test_pool_scrub_progress_ignores_other_scans(
    scan_function: str | None, scrub_state: str
) -> None:
    """A resilver's progress is not shown as scrub progress."""
    pool = {
        "scan_function": scan_function,
        "scrub_state": scrub_state,
        "scrub_percentage": 42.0,
    }
    assert pool_scrub_progress(pool) is None
