"""Unit tests for custom_components/truenas_ce/api.py.

Unlike config_flow.py/coordinator.py, TrueNASAPI has no Home Assistant
dependency at all -- it only wraps ``aiotruenas.TrueNASClient`` -- so it can
be imported and instantiated directly as a real package module. The
underlying ``aiotruenas`` client is replaced with a Mock/AsyncMock so no
network I/O happens.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiotruenas.exceptions import (
    TrueNASAuthenticationError,
    TrueNASCallError,
    TrueNASCallTimeoutError,
    TrueNASCertificateVerificationError,
    TrueNASConnectionClosedError,
    TrueNASConnectionRefusedError,
    TrueNASHostUnknownError,
    TrueNASMalformedResponseError,
)

from custom_components.truenas_ce import api as api_module
from custom_components.truenas_ce.api import (
    TrueNASAPI,
    _classify_exception,
    _summarize_payload,
)
from custom_components.truenas_ce.const import (
    ERR_CERT_VERIFY_FAILED,
    ERR_CONNECTION_REFUSED,
    ERR_INVALID_KEY,
    ERR_LOST_LOGIN,
    ERR_LOST_QUERY,
    ERR_MALFORMED_RESULT,
    ERR_TIMEOUT,
    ERR_UNKNOWN,
    ERR_UNKNOWN_HOSTNAME,
)


def _api_with_mock_client() -> TrueNASAPI:
    """Build a TrueNASAPI whose underlying aiotruenas client is a mock.

    ``connected`` defaults to False so tests that don't touch it (e.g. the
    permanently-closed cases) see the same falsy state a fresh, never-logged-in
    aiotruenas client would report.
    """
    mock_client = MagicMock()
    mock_client.connected = False
    with patch.object(api_module, "TrueNASClient", return_value=mock_client):
        api = TrueNASAPI("truenas.local", "api-key")
    return api


# ---------------------------
#   _summarize_payload
# ---------------------------
def test_summarize_payload_lists_shape() -> None:
    assert _summarize_payload([1, 2, 3]).startswith("list[3]")


def test_summarize_payload_dict_shape() -> None:
    assert _summarize_payload({"a": 1, "b": 2}).startswith("dict[2 keys]")


def test_summarize_payload_other_type_shape() -> None:
    assert _summarize_payload("hello").startswith("str ")


def test_summarize_payload_truncates_long_text() -> None:
    result = _summarize_payload(list(range(1000)), limit=20)
    assert "truncated" in result
    assert result.index("truncated") < len(result)


def test_summarize_payload_does_not_truncate_short_text() -> None:
    result = _summarize_payload([1, 2], limit=500)
    assert "truncated" not in result


# ---------------------------
#   _classify_exception
# ---------------------------
def test_classify_exception_connection_closed_during_call() -> None:
    exc = TrueNASConnectionClosedError("closed", phase="call")
    assert _classify_exception(exc, during_call=True) == ERR_LOST_QUERY


def test_classify_exception_connection_closed_not_during_call() -> None:
    exc = TrueNASConnectionClosedError("closed", phase="login")
    assert _classify_exception(exc, during_call=False) == ERR_LOST_LOGIN


def test_classify_exception_maps_known_types() -> None:
    assert (
        _classify_exception(TrueNASCertificateVerificationError(), during_call=False)
        == ERR_CERT_VERIFY_FAILED
    )
    assert (
        _classify_exception(TrueNASHostUnknownError("bad host"), during_call=False)
        == ERR_UNKNOWN_HOSTNAME
    )
    assert (
        _classify_exception(TrueNASConnectionRefusedError("refused"), during_call=False)
        == ERR_CONNECTION_REFUSED
    )
    assert (
        _classify_exception(TrueNASCallTimeoutError("timeout"), during_call=True)
        == ERR_TIMEOUT
    )
    assert (
        _classify_exception(TrueNASAuthenticationError(), during_call=False)
        == ERR_INVALID_KEY
    )
    assert (
        _classify_exception(TrueNASMalformedResponseError("bad"), during_call=True)
        == ERR_MALFORMED_RESULT
    )


def test_classify_exception_falls_back_to_unknown() -> None:
    exc = TrueNASCallError("boom")
    assert _classify_exception(exc, during_call=True) == ERR_UNKNOWN


# ---------------------------
#   TrueNASAPI.__init__
# ---------------------------
def test_init_defaults_to_wss() -> None:
    api = _api_with_mock_client()
    assert api.scheme == "wss"


def test_init_accepts_ws_scheme() -> None:
    with patch.object(api_module, "TrueNASClient", return_value=MagicMock()):
        api = TrueNASAPI("truenas.local", "key", scheme="WS")
    assert api.scheme == "ws"


def test_init_rejects_invalid_scheme() -> None:
    with pytest.raises(ValueError, match="Invalid WebSocket scheme"):
        TrueNASAPI("truenas.local", "key", scheme="http")


# ---------------------------
#   connect
# ---------------------------
async def test_connect_fails_when_permanently_closed() -> None:
    api = _api_with_mock_client()
    api._closed = True
    assert await api.connect() is False
    assert api.error == ERR_UNKNOWN


async def test_connect_returns_true_when_already_connected() -> None:
    api = _api_with_mock_client()
    api._client.connected = True
    assert await api.connect() is True


async def test_connect_success_clears_error() -> None:
    api = _api_with_mock_client()
    api._client.connected = False
    api._client.connect = AsyncMock()
    api._error = "stale error"
    assert await api.connect() is True
    assert api.error == ""


async def test_connect_maps_exception_and_returns_false() -> None:
    api = _api_with_mock_client()
    api._client.connected = False
    api._client.connect = AsyncMock(side_effect=TrueNASHostUnknownError("nope"))
    assert await api.connect() is False
    assert api.error == ERR_UNKNOWN_HOSTNAME


# ---------------------------
#   disconnect / close
# ---------------------------
async def test_disconnect_closes_client_but_stays_reconnectable() -> None:
    api = _api_with_mock_client()
    api._client.close = AsyncMock()
    await api.disconnect()
    api._client.close.assert_awaited_once()
    assert api._closed is False


async def test_close_marks_permanently_closed() -> None:
    api = _api_with_mock_client()
    api._client.close = AsyncMock()
    await api.close()
    api._client.close.assert_awaited_once()
    assert api._closed is True


# ---------------------------
#   connected
# ---------------------------
def test_connected_reflects_client_state() -> None:
    api = _api_with_mock_client()
    api._client.connected = True
    assert api.connected() is True
    api._client.connected = False
    assert api.connected() is False


# ---------------------------
#   connection_test
# ---------------------------
async def test_connection_test_fails_when_connect_fails() -> None:
    api = _api_with_mock_client()
    api._closed = True
    ok, error = await api.connection_test()
    assert ok is False
    assert error == ERR_UNKNOWN


async def test_connection_test_fails_when_query_returns_none() -> None:
    api = _api_with_mock_client()
    api._client.connected = True
    api._client.call = AsyncMock(return_value=None)
    ok, error = await api.connection_test()
    assert ok is False
    assert error == ERR_MALFORMED_RESULT


async def test_connection_test_succeeds() -> None:
    api = _api_with_mock_client()
    api._client.connected = True
    api._client.call = AsyncMock(return_value={"version": "25.04"})
    ok, error = await api.connection_test()
    assert ok is True
    assert error == ""


# ---------------------------
#   query
# ---------------------------
async def test_query_returns_none_when_connect_fails() -> None:
    api = _api_with_mock_client()
    api._closed = True
    assert await api.query("system.info") is None


async def test_query_returns_data_on_success() -> None:
    api = _api_with_mock_client()
    api._client.connected = True
    api._client.call = AsyncMock(return_value={"ok": True})
    assert await api.query("system.info") == {"ok": True}


async def test_query_call_error_uses_reason() -> None:
    api = _api_with_mock_client()
    api._client.connected = True
    api._client.call = AsyncMock(
        side_effect=TrueNASCallError("boom", reason="invalid params")
    )
    assert await api.query("system.info") is None
    assert api.error == "invalid params"


async def test_query_call_error_falls_back_to_str_then_unknown() -> None:
    api = _api_with_mock_client()
    api._client.connected = True
    api._client.call = AsyncMock(side_effect=TrueNASCallError("boom"))
    assert await api.query("system.info") is None
    assert api.error == "boom"


async def test_query_other_truenas_error_classifies_during_call() -> None:
    api = _api_with_mock_client()
    api._client.connected = True
    api._client.call = AsyncMock(side_effect=TrueNASCallTimeoutError("timeout"))
    assert await api.query("system.info") is None
    assert api.error == ERR_TIMEOUT


async def test_query_logs_summarized_payload_when_debug_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = _api_with_mock_client()
    api._client.connected = True
    api._client.call = AsyncMock(return_value={"ok": True})
    with caplog.at_level("DEBUG", logger=api_module.__name__):
        assert await api.query("system.info") == {"ok": True}
    assert "dict[1 keys]" in caplog.text


# ---------------------------
#   error / scheme properties
# ---------------------------
def test_error_property_defaults_empty() -> None:
    api = _api_with_mock_client()
    assert api.error == ""
