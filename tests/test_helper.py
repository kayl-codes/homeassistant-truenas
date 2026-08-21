"""Unit tests for the pure/self-contained helpers in helper.py."""

from __future__ import annotations

import pytest

from custom_components.truenas_ce.helper import sanitize_host


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
    ],
)
def test_sanitize_host(raw: str, expected: str) -> None:
    assert sanitize_host(raw) == expected
