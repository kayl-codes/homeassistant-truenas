"""Unit tests for the pure/self-contained helpers in helper.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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


async def test_legacy_code_installed_true_when_integration_resolves() -> None:
    hass = object()
    with patch.object(
        helper_module, "async_get_integration", AsyncMock(return_value=object())
    ) as get_mock:
        assert await async_legacy_code_installed(hass) is True
    get_mock.assert_awaited_once_with(hass, "truenas")


async def test_legacy_code_installed_false_when_integration_missing() -> None:
    """HACS-only uninstall: the files are gone, the legacy entry remains (#201)."""
    with patch.object(
        helper_module,
        "async_get_integration",
        AsyncMock(side_effect=IntegrationNotFound("truenas")),
    ):
        assert await async_legacy_code_installed(object()) is False
