"""Unit tests for the pure/self-contained helpers in config_flow.py.

``config_flow.py`` uses relative imports (``from .api import ...``,
``from .const import ...``), so it must be imported as a real package module
(``custom_components.truenas_ce.config_flow``) rather than loaded standalone
via ``importlib`` like ``apiparser.py``. This only exercises functions that
do not require a running Home Assistant instance -- ``pytest-homeassistant-
custom-component`` cannot be used on this repo's Windows dev machine (its
pytest plugin unconditionally imports the POSIX-only ``fcntl`` module).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from aiotruenas.exceptions import (
    TrueNASAuthenticationError,
    TrueNASConnectionRefusedError,
)
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_VERIFY_SSL

from custom_components.truenas_ce import config_flow, const
from custom_components.truenas_ce.config_flow import (
    TrueNASConfigFlow,
    _base_schema,
    _map_error_to_ha,
    _text_to_passphrases,
)
from custom_components.truenas_ce.const import (
    CONF_SYSTEM_ID,
    ERR_CERT_VERIFY_FAILED,
    ERR_CONNECTION_REFUSED,
    ERR_INVALID_KEY,
    ERR_LOST_LOGIN,
    ERR_LOST_QUERY,
    ERR_MALFORMED_RESULT,
    ERR_PERMISSION_DENIED,
    ERR_TIMEOUT,
    LEGACY_DOMAIN,
)


# ---------------------------
#   _base_schema
# ---------------------------
def test_base_schema_api_key_is_optional_not_required() -> None:
    """Regression guard for #158: a ``vol.Required`` API key field blocks
    blank submission client-side regardless of its default, which made the
    legacy-takeover blank-to-keep-the-key flow unusable in the HA frontend
    (invisible to this backend-only test suite, since voluptuous itself only
    checks presence -- see test_migrate_import_blank_api_key_keeps_legacy_key
    in test_config_flow_flows.py, which passed even before this fix).
    """
    # Compared against config_flow's own ``vol`` (not a freshly imported
    # voluptuous): Home Assistant may shadow the real module with its
    # "probatio" fork at import time, which would make an independently
    # imported vol.Optional a different (and thus isinstance-mismatching)
    # class than the one config_flow.py actually built the schema with.
    schema = _base_schema({})
    (marker,) = [key for key in schema.schema if key == CONF_API_KEY]
    assert isinstance(marker, config_flow.vol.Optional)
    assert not isinstance(marker, config_flow.vol.Required)


# ---------------------------
#   _text_to_passphrases
# ---------------------------
def test_text_to_passphrases_parses_multiple_lines() -> None:
    text = "tank/data#secret1\ntank/backup#secret2"
    assert _text_to_passphrases(text) == {
        "tank/data": "secret1",
        "tank/backup": "secret2",
    }


def test_text_to_passphrases_duplicate_datasets_last_wins() -> None:
    text = "tank/data#first\ntank/data#second"
    assert _text_to_passphrases(text) == {"tank/data": "second"}


def test_text_to_passphrases_skips_blank_lines() -> None:
    text = "\n  \ntank/data#secret1\n\ntank/backup#secret2\n\n"
    assert _text_to_passphrases(text) == {
        "tank/data": "secret1",
        "tank/backup": "secret2",
    }


@pytest.mark.parametrize("text", ["", "   \n  "])
def test_text_to_passphrases_empty_or_whitespace_returns_empty_dict(
    text: str,
) -> None:
    assert _text_to_passphrases(text) == {}


def test_text_to_passphrases_preserves_extra_hashes_in_value() -> None:
    text = "tank/data#sec#ret"
    assert _text_to_passphrases(text) == {"tank/data": "sec#ret"}


def test_text_to_passphrases_missing_separator_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        _text_to_passphrases("tank/data-no-separator")
    assert exc_info.value.args[0] == (
        "passphrase_malformed_line",
        "tank/data-no-separator",
    )


def test_text_to_passphrases_empty_name_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        _text_to_passphrases("#secret1")
    assert exc_info.value.args[0] == ("passphrase_empty_name", "#secret1")


def test_text_to_passphrases_empty_value_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        _text_to_passphrases("tank/data#")
    assert exc_info.value.args[0] == ("passphrase_empty_value", "tank/data#")


# ---------------------------
#   _map_error_to_ha
# ---------------------------
def test_map_error_to_ha_known_error_passthrough() -> None:
    assert _map_error_to_ha(ERR_INVALID_KEY) == ERR_INVALID_KEY
    assert _map_error_to_ha(ERR_CERT_VERIFY_FAILED) == ERR_CERT_VERIFY_FAILED
    assert _map_error_to_ha(ERR_TIMEOUT) == ERR_TIMEOUT
    assert _map_error_to_ha(ERR_LOST_LOGIN) == ERR_LOST_LOGIN
    assert _map_error_to_ha(ERR_LOST_QUERY) == ERR_LOST_QUERY
    assert _map_error_to_ha(ERR_PERMISSION_DENIED) == ERR_PERMISSION_DENIED


# Codes that never reach _map_error_to_ha as an API error: ERR_UNKNOWN is the
# API's own catch-all (deliberately mapped to "unknown"), ERR_API_KEY_REQUIRED
# is set directly by the config flow on the API-key field.
_NOT_MAPPED_ERR_CODES = {const.ERR_UNKNOWN, const.ERR_API_KEY_REQUIRED}


def _config_error_strings(relative_path: str) -> dict[str, str]:
    path = Path(const.__file__).parent / relative_path
    return json.loads(path.read_text(encoding="utf-8"))["config"]["error"]


@pytest.mark.parametrize("strings_file", ["strings.json", "translations/en.json"])
def test_every_api_err_code_is_mapped_and_translated(strings_file: str) -> None:
    """Guard against a new ERR_* code silently degrading to "unknown".

    ERR_TIMEOUT (#128) and ERR_LOST_LOGIN/ERR_LOST_QUERY were each added to
    const.py without being added to _map_error_to_ha, so the config flow
    showed "Unknown error occurred." instead of the real cause. en.json is
    checked as well because a custom integration loads translations/*.json
    at runtime, not strings.json.
    """
    error_strings = _config_error_strings(strings_file)
    codes = {
        value
        for name, value in vars(const).items()
        if name.startswith("ERR_") and isinstance(value, str)
    }
    assert codes
    assert _map_error_to_ha(const.ERR_UNKNOWN) == "unknown"
    assert "unknown" in error_strings
    for code in sorted(codes - {const.ERR_UNKNOWN}):
        assert code in error_strings, code
        if code not in _NOT_MAPPED_ERR_CODES:
            assert _map_error_to_ha(code) == code, code


def test_map_error_to_ha_unknown_error_falls_back() -> None:
    assert _map_error_to_ha("some_never_seen_errorcode") == "unknown"


# ---------------------------
#   _async_guess_ip
# ---------------------------
GUESS_CANDIDATES = 1 + len(const.KNOWN_DOMAINS)


@pytest.fixture(autouse=True)
def _fresh_host_guess_executor() -> Iterator[None]:
    """Give each test its own lookup pool and join its threads afterwards.

    The module-level pool lives for the process; Home Assistant's test plugin
    rejects threads that outlive a test, and a shared pool would let one test's
    stalled lookups leak into the next. The default resolver fake stays active
    until the pool is joined: a worker that only reaches gethostbyname after a
    test's own patch is gone must never hit the real (possibly slow) resolver.
    """
    executor = ThreadPoolExecutor(
        max_workers=GUESS_CANDIDATES, thread_name_prefix="truenas_ce_host_guess"
    )
    with (
        patch.object(config_flow, "_HOST_GUESS_EXECUTOR", executor),
        patch.object(config_flow, "_host_guess", config_flow._HostGuessState()),
        patch.object(
            config_flow.socket, "gethostbyname", side_effect=OSError("not found")
        ),
    ):
        yield
        executor.shutdown(wait=True, cancel_futures=True)


async def test_guess_ip_returns_first_resolvable_host() -> None:
    with patch.object(config_flow.socket, "gethostbyname", return_value="10.0.0.5"):
        assert await config_flow._async_guess_ip() == "10.0.0.5"


async def test_guess_ip_prefers_candidate_priority_over_answer_order() -> None:
    # The bare "truenas" outranks every domain-suffixed name, even when its
    # answer arrives last.
    resolved = {"truenas": "10.0.0.1", "truenas.lan": "10.0.0.7"}
    lower_answered = threading.Event()

    def fake_gethostbyname(host: str) -> str:
        if host == "truenas":
            # Answer only after the lower-priority name has answered.
            assert lower_answered.wait(5)
        if host in resolved:
            if host != "truenas":
                lower_answered.set()
            return resolved[host]
        raise OSError("not found")

    with (
        # Generous: only the event decides the answer order, never the deadline.
        patch.object(config_flow, "GUESS_HOST_TIMEOUT", 30.0),
        patch.object(
            config_flow.socket, "gethostbyname", side_effect=fake_gethostbyname
        ),
    ):
        assert await config_flow._async_guess_ip() == "10.0.0.1"


async def test_guess_ip_uses_lower_priority_answer_when_higher_one_hangs() -> None:
    release = threading.Event()

    def fake_gethostbyname(host: str) -> str:
        if host == "truenas":
            release.wait(5)
            return "10.0.0.1"
        if host == "truenas.local":
            return "10.0.0.9"
        raise OSError("not found")

    try:
        with (
            # Generous: the lower-priority answer only has to land in time.
            patch.object(config_flow, "GUESS_HOST_TIMEOUT", 1.0),
            patch.object(
                config_flow.socket, "gethostbyname", side_effect=fake_gethostbyname
            ),
        ):
            result = await config_flow._async_guess_ip()
    finally:
        release.set()
    assert result == "10.0.0.9"


async def test_guess_ip_returns_best_answer_without_waiting_for_the_rest() -> None:
    release = threading.Event()

    def fake_gethostbyname(host: str) -> str:
        if host == "truenas":
            return "10.0.0.1"
        release.wait(5)
        raise OSError("timed out")

    try:
        with (
            patch.object(config_flow, "GUESS_HOST_TIMEOUT", 30.0),
            patch.object(
                config_flow.socket, "gethostbyname", side_effect=fake_gethostbyname
            ),
        ):
            result = await config_flow._async_guess_ip()
            # The top candidate answered, so nothing lower can win: the guess
            # returned while the others were still blocked in the resolver.
            still_running = [
                not lookup.done() for lookup in config_flow._host_guess.lookups[1:]
            ]
    finally:
        release.set()
    assert result == "10.0.0.1"
    assert len(still_running) == GUESS_CANDIDATES - 1
    assert all(still_running)


async def test_guess_ip_tries_every_candidate_on_its_own_executor() -> None:
    calls: list[tuple[str, str]] = []

    def fake_gethostbyname(host: str) -> str:
        calls.append((host, threading.current_thread().name))
        raise OSError("not found")

    with patch.object(
        config_flow.socket, "gethostbyname", side_effect=fake_gethostbyname
    ):
        await config_flow._async_guess_ip()
    assert sorted(host for host, _ in calls) == sorted(
        ["truenas"] + [f"truenas.{d}" for d in const.KNOWN_DOMAINS]
    )
    # Never on Home Assistant's shared executor: a stalled resolver call can't
    # be cancelled and would otherwise tie up threads other jobs need.
    assert all(name.startswith("truenas_ce_host_guess") for _, name in calls)


async def test_guess_ip_returns_default_host_when_none_resolve() -> None:
    with patch.object(
        config_flow.socket, "gethostbyname", side_effect=OSError("not found")
    ):
        assert await config_flow._async_guess_ip() == "truenas.local"


async def test_guess_ip_does_not_wait_for_hanging_dns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resolver that never answers must not stall the form (#196)."""
    caplog.set_level(logging.DEBUG, logger=config_flow.__name__)
    release = threading.Event()

    def hanging_gethostbyname(host: str) -> str:
        release.wait(5)
        raise OSError("timed out")

    try:
        with (
            patch.object(config_flow, "GUESS_HOST_TIMEOUT", 0.2),
            patch.object(
                config_flow.socket,
                "gethostbyname",
                side_effect=hanging_gethostbyname,
            ),
        ):
            start = time.monotonic()
            result = await config_flow._async_guess_ip()
            elapsed = time.monotonic() - start
    finally:
        release.set()
    assert result == config_flow.DEFAULT_HOST
    assert elapsed < 1  # 0.2 s cap plus scheduling slack, far below the 5 s hang
    candidates = 1 + len(const.KNOWN_DOMAINS)
    messages = [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == config_flow.__name__
    ]
    assert any(
        level == logging.DEBUG
        and msg.startswith("Host guess finished in ")
        and msg.endswith(f"({candidates} of {candidates} lookups still pending)")
        for level, msg in messages
    )
    assert (
        logging.DEBUG,
        f"No candidate hostname resolved within 0.2s, using {config_flow.DEFAULT_HOST}",
    ) in messages


# ---------------------------
#   TrueNASConfigFlow._validate_connection
# ---------------------------
async def test_validate_connection_invalid_host_sets_error() -> None:
    flow = TrueNASConfigFlow()
    config = {CONF_HOST: "bad host", CONF_API_KEY: "key", CONF_VERIFY_SSL: True}
    errors: dict[str, str] = {}
    with patch.object(config_flow, "TrueNASAPI", side_effect=ValueError):
        await flow._validate_connection(config, errors)
    assert errors[CONF_HOST] == config_flow.ERR_INVALID_HOSTNAME


async def test_validate_connection_success_sets_no_errors() -> None:
    flow = TrueNASConfigFlow()
    config = {CONF_HOST: "nas.local", CONF_API_KEY: "key", CONF_VERIFY_SSL: True}
    errors: dict[str, str] = {}
    api = MagicMock()
    api.connection_test = AsyncMock(return_value=(True, ""))
    api.query = AsyncMock(return_value="test-global-id")
    api.disconnect = AsyncMock()
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        await flow._validate_connection(config, errors)
    assert not errors
    assert config[CONF_SYSTEM_ID] == "test-global-id"
    api.query.assert_awaited_once_with("system.global.id")
    api.disconnect.assert_awaited_once()


async def test_validate_connection_failure_maps_error() -> None:
    flow = TrueNASConfigFlow()
    config = {CONF_HOST: "nas.local", CONF_API_KEY: "key", CONF_VERIFY_SSL: True}
    errors: dict[str, str] = {}
    api = MagicMock()
    api.connection_test = AsyncMock(return_value=(False, ERR_MALFORMED_RESULT))
    api.query = AsyncMock()
    api.disconnect = AsyncMock()
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        await flow._validate_connection(config, errors)
    assert errors[CONF_HOST] == ERR_MALFORMED_RESULT
    api.query.assert_not_awaited()


async def test_validate_connection_system_id_lookup_failure_does_not_block() -> None:
    """A broken system.global.id lookup must not fail the whole connection test."""
    flow = TrueNASConfigFlow()
    config = {CONF_HOST: "nas.local", CONF_API_KEY: "key", CONF_VERIFY_SSL: True}
    errors: dict[str, str] = {}
    api = MagicMock()
    api.connection_test = AsyncMock(return_value=(True, ""))
    api.query = AsyncMock(side_effect=RuntimeError("boom"))
    api.disconnect = AsyncMock()
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        await flow._validate_connection(config, errors)
    assert not errors
    assert CONF_SYSTEM_ID not in config
    api.query.assert_awaited_once_with("system.global.id")
    api.disconnect.assert_awaited_once()


async def test_validate_connection_unknown_error_when_connection_test_raises() -> None:
    """An unexpected exception from connection_test() degrades to a retryable error.

    Previously such an exception propagated out of _validate_connection()
    entirely (crashing the flow with an unhandled traceback) instead of
    setting a normal, retryable "unknown" form error -- inconsistent with
    every other connection helper in this file, which already catches and
    logs broadly. It must also still disconnect, so the socket isn't leaked.
    """
    flow = TrueNASConfigFlow()
    config = {CONF_HOST: "nas.local", CONF_API_KEY: "key", CONF_VERIFY_SSL: True}
    errors: dict[str, str] = {}
    api = MagicMock()
    api.connection_test = AsyncMock(side_effect=RuntimeError("boom"))
    api.disconnect = AsyncMock()
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        await flow._validate_connection(config, errors)
    assert errors[CONF_HOST] == "unknown"
    api.disconnect.assert_awaited_once()


# ---------------------------
#   TrueNASConfigFlow._probe_is_truenas
# ---------------------------
async def test_probe_is_truenas_true_on_invalid_key() -> None:
    """A bogus-key rejection proves it's a real TrueNAS JSON-RPC endpoint."""
    api = MagicMock()
    api.connect = AsyncMock(return_value=False)
    api.disconnect = AsyncMock()
    api.error = ERR_INVALID_KEY
    with patch.object(config_flow, "TrueNASAPI", return_value=api) as mock_api:
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") == "1.2.3.4"
    mock_api.assert_called_once_with("1.2.3.4", "-", verify_ssl=False, scheme="wss")
    api.disconnect.assert_awaited_once()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TrueNASAuthenticationError("rejected"), "1.2.3.4"),
        (TrueNASConnectionRefusedError("refused"), None),
    ],
)
async def test_probe_is_truenas_real_api_connect_failure(
    exc: Exception, expected: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the real TrueNASAPI's bogus-key rejection identifies a TrueNAS.

    Regression test: the probe used to bail out on any failed connect()
    before checking ``api.error``, so it could never recognise a TrueNAS --
    the mocked tests above hid it by letting connect() return a truthy mock.
    Either way the probe stays quiet: nothing above DEBUG, no traceback.
    """
    caplog.set_level(logging.DEBUG)
    with patch("aiotruenas.TrueNASClient.connect", AsyncMock(side_effect=exc)):
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") == expected
    assert not [r for r in caplog.records if r.levelno > logging.DEBUG]
    assert all(r.exc_info is None for r in caplog.records)


async def test_probe_is_truenas_false_when_bogus_key_connects() -> None:
    """A device that accepts the bogus key is not a TrueNAS answer."""
    api = MagicMock()
    api.connect = AsyncMock(return_value=True)
    api.disconnect = AsyncMock()
    api.error = ""
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") is None
    api.disconnect.assert_awaited_once()


async def test_probe_is_truenas_connects_quietly() -> None:
    """Probing must not flood the log: most _http._tcp devices aren't TrueNAS.

    Regression test for the follow-up to issue #46: zeroconf discovery
    connecting to every non-TrueNAS device on the LAN was logging a full
    ERROR traceback per candidate.
    """
    api = MagicMock()
    api.connect = AsyncMock(return_value=False)
    api.disconnect = AsyncMock()
    api.error = ERR_INVALID_KEY
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") == "1.2.3.4"
    api.connect.assert_awaited_once_with(quiet=True)


async def test_probe_is_truenas_false_when_not_truenas() -> None:
    """Any other error means some unrelated _http._tcp device, not TrueNAS."""
    api = MagicMock()
    api.connect = AsyncMock(return_value=False)
    api.disconnect = AsyncMock()
    api.error = ERR_CONNECTION_REFUSED
    with patch.object(config_flow, "TrueNASAPI", return_value=api) as mock_api:
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") is None
    assert mock_api.call_count == 1
    assert api.disconnect.await_count == 1


async def test_probe_is_truenas_false_when_connect_raises() -> None:
    """An unexpected connect() error is treated as "not TrueNAS", not a crash."""
    api = MagicMock()
    api.connect = AsyncMock(side_effect=OSError("connection refused"))
    api.disconnect = AsyncMock()
    with patch.object(config_flow, "TrueNASAPI", return_value=api) as mock_api:
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") is None
    assert mock_api.call_count == 1
    assert api.disconnect.await_count == 1


async def test_probe_is_truenas_true_when_disconnect_raises() -> None:
    """A broken disconnect() must not abort the probe for the whole flow."""
    api = MagicMock()
    api.connect = AsyncMock(return_value=False)
    api.disconnect = AsyncMock(side_effect=OSError("already closed"))
    api.error = ERR_INVALID_KEY
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") == "1.2.3.4"
    api.disconnect.assert_awaited_once()


async def test_probe_is_truenas_falls_back_to_advertised_port() -> None:
    """A box reachable only on its advertised port keeps that port."""
    default_api = MagicMock()
    default_api.connect = AsyncMock(return_value=False)
    default_api.disconnect = AsyncMock()
    default_api.error = ERR_CONNECTION_REFUSED

    port_api = MagicMock()
    port_api.connect = AsyncMock(return_value=False)
    port_api.disconnect = AsyncMock()
    port_api.error = ERR_INVALID_KEY

    with patch.object(
        config_flow,
        "TrueNASAPI",
        side_effect=[default_api, port_api],
    ) as mock_api:
        probed = await TrueNASConfigFlow._probe_is_truenas("1.2.3.4", 8443)
    assert probed == "1.2.3.4:8443"
    mock_api.assert_any_call("1.2.3.4:8443", "-", verify_ssl=False, scheme="wss")


@pytest.mark.parametrize("port", [None, 80, 443])
async def test_probe_is_truenas_ignores_default_ports(port: int | None) -> None:
    """Default ports add nothing over the bare host, so they are not retried."""
    api = MagicMock()
    api.connect = AsyncMock(return_value=False)
    api.disconnect = AsyncMock()
    api.error = ERR_CONNECTION_REFUSED
    with patch.object(config_flow, "TrueNASAPI", return_value=api) as mock_api:
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4", port) is None
    assert mock_api.call_count == 1  # bare host only, wss only


# ---------------------------
#   TrueNASConfigFlow._find_legacy_config
# ---------------------------
def test_find_legacy_config_none_when_domain_is_legacy() -> None:
    flow = TrueNASConfigFlow()
    with patch.object(config_flow, "DOMAIN", LEGACY_DOMAIN):
        assert flow._find_legacy_config() is None


def test_find_legacy_config_returns_first_entry() -> None:
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    legacy_entry = MagicMock()
    flow.hass.config_entries.async_entries.return_value = [legacy_entry]
    assert flow._find_legacy_config() is legacy_entry
    flow.hass.config_entries.async_entries.assert_called_once_with(LEGACY_DOMAIN)


def test_find_legacy_config_none_when_no_legacy_entries() -> None:
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_entries.return_value = []
    assert flow._find_legacy_config() is None


def test_find_legacy_config_matches_regardless_of_host_case() -> None:
    """A legacy host stored in a different case still matches (e.g. NAS.local).

    Regression test: host matching here must stay in sync with
    ``migration._find_legacy_entry``, which normalizes both sides the same way.
    """
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    legacy_entry = MagicMock()
    legacy_entry.data = {CONF_HOST: "NAS.Local"}
    other_entry = MagicMock()
    other_entry.data = {CONF_HOST: "other.local"}
    flow.hass.config_entries.async_entries.return_value = [other_entry, legacy_entry]
    flow.truenas_config[CONF_HOST] = "nas.local"
    assert flow._find_legacy_config() is legacy_entry


# ---------------------------
#   TrueNASConfigFlow._validate_passphrase_names
# ---------------------------
def test_validate_passphrase_names_no_coordinator_skips() -> None:
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = None
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._validate_passphrase_names({"tank/data": "x"}, "entry1", errors, placeholders)
    assert not errors


def test_validate_passphrase_names_no_known_datasets_skips() -> None:
    flow = TrueNASConfigFlow()
    coordinator = MagicMock()
    coordinator.ds = {"dataset": {}}
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = MagicMock(
        runtime_data=coordinator
    )
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._validate_passphrase_names({"tank/data": "x"}, "entry1", errors, placeholders)
    assert not errors


def test_validate_passphrase_names_unknown_dataset_sets_error() -> None:
    flow = TrueNASConfigFlow()
    coordinator = MagicMock()
    coordinator.ds = {"dataset": {"1": {"name": "tank/data"}}}
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = MagicMock(
        runtime_data=coordinator
    )
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._validate_passphrase_names(
        {"tank/unknown": "x"}, "entry1", errors, placeholders
    )
    assert errors["dataset_passphrases"] == "unknown_dataset"
    assert placeholders["datasets"] == "tank/unknown"


def test_validate_passphrase_names_multiple_unknown_datasets_sets_error() -> None:
    flow = TrueNASConfigFlow()
    coordinator = MagicMock()
    coordinator.ds = {"dataset": {"1": {"name": "tank/data"}}}
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = MagicMock(
        runtime_data=coordinator
    )
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._validate_passphrase_names(
        {"tank/unknown1": "x", "pool/unknown2": "y"}, "entry1", errors, placeholders
    )
    assert errors["dataset_passphrases"] == "unknown_dataset"
    assert placeholders["datasets"] == "pool/unknown2, tank/unknown1"


def test_validate_passphrase_names_all_known_sets_no_error() -> None:
    flow = TrueNASConfigFlow()
    coordinator = MagicMock()
    coordinator.ds = {"dataset": {"1": {"name": "tank/data"}}}
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = MagicMock(
        runtime_data=coordinator
    )
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._validate_passphrase_names({"tank/data": "x"}, "entry1", errors, placeholders)
    assert not errors


# ---------------------------
#   TrueNASConfigFlow._apply_passphrase_input
# ---------------------------
def test_apply_passphrase_input_blank_text_removes_key() -> None:
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = None
    user_input = {"dataset_passphrases": "   "}
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._apply_passphrase_input(user_input, {}, "entry1", errors, placeholders)
    assert "dataset_passphrases" not in user_input
    assert not errors


def test_apply_passphrase_input_malformed_line_sets_error() -> None:
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = None
    user_input = {"dataset_passphrases": "no-separator-here"}
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._apply_passphrase_input(user_input, {}, "entry1", errors, placeholders)
    assert errors["dataset_passphrases"] == "passphrase_malformed_line"
    assert placeholders["line"] == "no-separator-here"


def test_apply_passphrase_input_empty_dataset_name_sets_error() -> None:
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = None
    user_input = {"dataset_passphrases": "#value"}
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._apply_passphrase_input(user_input, {}, "entry1", errors, placeholders)
    assert errors["dataset_passphrases"] == "passphrase_empty_name"
    assert placeholders["line"] == "#value"


def test_apply_passphrase_input_empty_passphrase_value_sets_error() -> None:
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = None
    user_input = {"dataset_passphrases": "dataset#"}
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._apply_passphrase_input(user_input, {}, "entry1", errors, placeholders)
    assert errors["dataset_passphrases"] == "passphrase_empty_value"
    assert placeholders["line"] == "dataset#"


def test_apply_passphrase_input_merges_with_existing() -> None:
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = None
    truenas_config = {"dataset_passphrases": {"tank/old": "oldsecret"}}
    user_input = {"dataset_passphrases": "tank/new#newsecret"}
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._apply_passphrase_input(
        user_input, truenas_config, "entry1", errors, placeholders
    )
    assert user_input["dataset_passphrases"] == {
        "tank/old": "oldsecret",
        "tank/new": "newsecret",
    }


def test_apply_passphrase_input_overrides_existing_dataset_passphrase() -> None:
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = None
    truenas_config = {"dataset_passphrases": {"tank/data": "oldsecret"}}
    user_input = {"dataset_passphrases": "tank/data#newsecret"}
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._apply_passphrase_input(
        user_input, truenas_config, "entry1", errors, placeholders
    )
    assert not errors
    assert user_input["dataset_passphrases"] == {"tank/data": "newsecret"}


def test_apply_passphrase_input_strips_dataset_name_but_not_passphrase() -> None:
    """The dataset name is trimmed; the passphrase value is taken verbatim."""
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = None
    user_input = {"dataset_passphrases": "  tank/data  #  secret  "}
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._apply_passphrase_input(user_input, {}, "entry1", errors, placeholders)
    assert not errors
    assert user_input["dataset_passphrases"] == {"tank/data": "  secret"}


def test_apply_passphrase_input_unknown_dataset_propagates_validation_error() -> None:
    flow = TrueNASConfigFlow()
    coordinator = MagicMock()
    coordinator.ds = {"dataset": {"1": {"name": "tank/data"}}}
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = MagicMock(
        runtime_data=coordinator
    )
    user_input = {"dataset_passphrases": "tank/unknown#secret"}
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    flow._apply_passphrase_input(user_input, {}, "entry1", errors, placeholders)
    assert errors["dataset_passphrases"] == "unknown_dataset"
    assert placeholders["datasets"] == "tank/unknown"
    # On error, user_input is left untouched (not converted to a dict).
    assert user_input["dataset_passphrases"] == "tank/unknown#secret"


# ---------------------------
#   TrueNASConfigFlow._check_connection_if_changed
# ---------------------------
async def test_check_connection_if_changed_skips_when_unchanged() -> None:
    flow = TrueNASConfigFlow()
    entry_data = {CONF_HOST: "nas.local", CONF_API_KEY: "key", CONF_VERIFY_SSL: True}
    truenas_config = dict(entry_data)
    errors: dict[str, str] = {}
    with patch.object(flow, "_validate_connection", AsyncMock()) as mocked_validate:
        await flow._check_connection_if_changed(truenas_config, entry_data, errors)
    mocked_validate.assert_not_awaited()


async def test_check_connection_if_changed_runs_when_host_changed() -> None:
    flow = TrueNASConfigFlow()
    entry_data = {CONF_HOST: "nas.local", CONF_API_KEY: "key", CONF_VERIFY_SSL: True}
    truenas_config = dict(entry_data)
    truenas_config[CONF_HOST] = "nas.new"
    errors: dict[str, str] = {}
    with patch.object(flow, "_validate_connection", AsyncMock()) as mocked_validate:
        await flow._check_connection_if_changed(truenas_config, entry_data, errors)
    mocked_validate.assert_awaited_once_with(truenas_config, errors)


# ---------------------------
#   zeroconf identity matching (#195)
# ---------------------------
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("nas.local", "nas.local"),
        ("nas.local:8443", "nas.local"),
        ("192.168.1.50:443", "192.168.1.50"),
        ("[fe80::1]", "fe80::1"),
        ("[2001:db8::1]:8443", "2001:db8::1"),
        ("nas.local.", "nas.local"),
        ("nas.local.:8443", "nas.local"),
        ("user:secret@nas.local:8443", "nas.local"),
        ("user:secret@[fe80::1]:8443", "fe80::1"),
    ],
)
def test_host_without_port(host: str, expected: str) -> None:
    assert config_flow._host_without_port(host) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.168.1.50", "192.168.1.50"),
        ("FE80::0001", "fe80::1"),
        ("fe80::1%eth0", "fe80::1"),
        ("nas.local", None),
        ("", None),
    ],
)
def test_normalize_ip(value: str, expected: str | None) -> None:
    assert config_flow._normalize_ip(value) == expected


async def test_resolve_host_returns_resolved_ips() -> None:
    hass = MagicMock()
    hass.loop.getaddrinfo = AsyncMock(
        return_value=[
            (2, 1, 6, "", ("192.168.1.50", 0)),
            (23, 1, 6, "", ("fe80::1%2", 0, 0, 2)),
        ]
    )
    assert await config_flow._async_resolve_host(hass, "nas.lan") == {
        "192.168.1.50",
        "fe80::1",
    }


@pytest.mark.parametrize(
    "error",
    [
        OSError("NXDOMAIN"),
        TimeoutError(),
        # What getaddrinfo really raises for a malformed host like "nas..lan".
        UnicodeError("label empty or too long"),
        ValueError("embedded null character"),
    ],
)
async def test_resolve_host_failure_yields_empty_set(error: Exception) -> None:
    hass = MagicMock()
    hass.loop.getaddrinfo = AsyncMock(side_effect=error)
    assert await config_flow._async_resolve_host(hass, "nas..lan") == set()


def _loaded_entry(query: AsyncMock) -> MagicMock:
    entry = MagicMock()
    entry.state = config_flow.ConfigEntryState.LOADED
    entry.runtime_data.api.query = query
    entry.runtime_data.api.connected = MagicMock(return_value=True)
    return entry


async def test_entry_reported_ips_parses_ip_in_use() -> None:
    # Shape verified against a live TrueNAS 25.10 box.
    query = AsyncMock(
        return_value=[
            {"type": "INET", "address": "192.168.0.252", "netmask": 24},
            {"type": "INET6", "address": "fe80::d250:99ff:fefb:368d", "netmask": 64},
            {"type": "INET", "address": None},
            "garbage",
        ]
    )
    ips = await config_flow._async_entry_reported_ips(_loaded_entry(query))
    assert ips == {"192.168.0.252", "fe80::d250:99ff:fefb:368d"}
    query.assert_awaited_once_with("interface.ip_in_use", config_flow._IP_IN_USE_PARAMS)


@pytest.mark.parametrize(
    "query",
    [
        AsyncMock(return_value=None),
        AsyncMock(return_value={"address": "192.168.0.252"}),
        AsyncMock(side_effect=RuntimeError("boom")),
    ],
)
async def test_entry_reported_ips_tolerates_bad_replies(query: AsyncMock) -> None:
    assert await config_flow._async_entry_reported_ips(_loaded_entry(query)) == set()


async def test_entry_reported_ips_skips_unloaded_entry() -> None:
    query = AsyncMock()
    entry = _loaded_entry(query)
    entry.state = config_flow.ConfigEntryState.SETUP_RETRY
    assert await config_flow._async_entry_reported_ips(entry) == set()
    query.assert_not_awaited()


async def test_entry_reported_ips_skips_disconnected_api() -> None:
    """No RPC (and so no reconnect attempt) while the entry is offline."""
    query = AsyncMock()
    entry = _loaded_entry(query)
    entry.runtime_data.api.connected.return_value = False
    assert await config_flow._async_entry_reported_ips(entry) == set()
    query.assert_not_awaited()


@pytest.mark.parametrize(
    ("matches", "expected"),
    [([], False), ([False, False], False), ([False, True], True), ([True, True], True)],
)
async def test_discovery_is_configured_checks_all_entries(
    matches: list[bool], expected: bool
) -> None:
    """Any matching entry (not just the first) marks the discovery configured."""
    entries = [
        SimpleNamespace(
            title=f"e{i}",
            entry_id=f"id{i}",
            state=config_flow.ConfigEntryState.NOT_LOADED,
        )
        for i in range(len(matches))
    ]
    seen: list[object] = []

    async def fake_match(
        hass: object, entry: object, addresses: set[str], hostname: str
    ) -> bool:
        seen.append(entry)
        assert addresses == {"192.168.1.50"}
        assert hostname == "nas.local"
        return matches[entries.index(entry)]

    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.data = {}
    info = SimpleNamespace(ip_addresses=[], host="192.168.1.50", hostname="NAS.local.")
    with (
        patch.object(config_flow, "_async_entry_matches_discovery", fake_match),
        patch.object(flow, "_async_current_entries", return_value=entries),
    ):
        assert await flow._async_discovery_is_configured(info) is expected
    assert seen == entries  # every entry is checked concurrently
    flow.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.parametrize(
    ("state", "reloaded"),
    [
        (config_flow.ConfigEntryState.SETUP_RETRY, True),
        (config_flow.ConfigEntryState.LOADED, False),
        (config_flow.ConfigEntryState.SETUP_ERROR, False),
    ],
)
async def test_discovery_is_configured_reloads_retrying_match(
    state: config_flow.ConfigEntryState, reloaded: bool
) -> None:
    """A matched entry waiting for its setup retry is reloaded right away (#196)."""
    other = SimpleNamespace(
        title="other", entry_id="other", state=config_flow.ConfigEntryState.SETUP_RETRY
    )
    matched = SimpleNamespace(title="nas", entry_id="nas", state=state)

    async def fake_match(
        hass: object, entry: object, addresses: set[str], hostname: str
    ) -> bool:
        return entry is matched

    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.data = {}
    info = SimpleNamespace(ip_addresses=[], host="192.168.1.50", hostname="nas.local.")
    with (
        patch.object(config_flow, "_async_entry_matches_discovery", fake_match),
        patch.object(flow, "_async_current_entries", return_value=[other, matched]),
    ):
        assert await flow._async_discovery_is_configured(info) is True
    reload = flow.hass.config_entries.async_schedule_reload
    if reloaded:
        # Only the matched entry, never an unrelated one that is also retrying.
        reload.assert_called_once_with("nas")
    else:
        reload.assert_not_called()


async def test_discovery_is_configured_reloads_every_retrying_match() -> None:
    """Every matching entry waiting for a retry is reloaded, not just the first."""
    retry = config_flow.ConfigEntryState.SETUP_RETRY
    entries = [
        SimpleNamespace(title=name, entry_id=name, state=retry)
        for name in ("first", "second", "unmatched")
    ]

    async def fake_match(
        hass: object, entry: object, addresses: set[str], hostname: str
    ) -> bool:
        return entry is not entries[2]

    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.data = {}
    info = SimpleNamespace(ip_addresses=[], host="192.168.1.50", hostname="nas.local.")
    with (
        patch.object(config_flow, "_async_entry_matches_discovery", fake_match),
        patch.object(flow, "_async_current_entries", return_value=entries),
    ):
        assert await flow._async_discovery_is_configured(info) is True
    reload = flow.hass.config_entries.async_schedule_reload
    assert [c.args[0] for c in reload.call_args_list] == ["first", "second"]


def _retry_entry(entry_id: str, host: str, state: object) -> SimpleNamespace:
    return SimpleNamespace(
        title=entry_id, entry_id=entry_id, state=state, data={CONF_HOST: host}
    )


@pytest.mark.parametrize(
    ("probed", "reloaded"), [("192.168.1.50", ["nas"]), (None, [])]
)
async def test_retry_entries_for_host_reloads_only_after_probe(
    probed: str | None, reloaded: list[str]
) -> None:
    """Only retrying entries stored with the host, once the API answers (#196)."""
    retry = config_flow.ConfigEntryState.SETUP_RETRY
    entries = [
        _retry_entry("nas", "192.168.1.50", retry),
        _retry_entry("loaded", "192.168.1.50", config_flow.ConfigEntryState.LOADED),
        _retry_entry("other", "192.168.1.60", retry),
    ]
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.data = {}
    probe = AsyncMock(return_value=probed)
    with (
        patch.object(flow, "_async_current_entries", return_value=entries) as current,
        patch.object(flow, "_probe_is_truenas", probe),
    ):
        await flow._async_retry_entries_for_host("192.168.1.50", 443)
    current.assert_called_once_with(include_ignore=False)
    probe.assert_awaited_once_with("192.168.1.50", 443)
    reload = flow.hass.config_entries.async_schedule_reload
    assert [c.args[0] for c in reload.call_args_list] == reloaded


async def test_retry_entries_for_host_skips_probe_without_retrying_entry() -> None:
    """A plain rediscovery of a loaded box costs no extra probe."""
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    loaded = _retry_entry("nas", "192.168.1.50", config_flow.ConfigEntryState.LOADED)
    probe = AsyncMock()
    with (
        patch.object(flow, "_async_current_entries", return_value=[loaded]),
        patch.object(flow, "_probe_is_truenas", probe),
    ):
        await flow._async_retry_entries_for_host("192.168.1.50", None)
    probe.assert_not_awaited()
    flow.hass.config_entries.async_schedule_reload.assert_not_called()


def test_reload_if_retrying_respects_cooldown(caplog: pytest.LogCaptureFixture) -> None:
    """Announcement bursts retry once per cooldown, then again afterwards."""
    flow = TrueNASConfigFlow()
    flow.hass = MagicMock()
    flow.hass.data = {}
    entry = _retry_entry(
        "nas", "192.168.1.50", config_flow.ConfigEntryState.SETUP_RETRY
    )
    reload = flow.hass.config_entries.async_schedule_reload
    cooldown = config_flow._DISCOVERY_RELOAD_COOLDOWN
    with (
        caplog.at_level(logging.DEBUG, logger=config_flow.__name__),
        patch.object(
            config_flow.time, "monotonic", side_effect=[100.0, 101.0, 100.0 + cooldown]
        ),
    ):
        flow._async_reload_if_retrying(entry)
        flow._async_reload_if_retrying(entry)
        flow._async_reload_if_retrying(entry)
    assert reload.call_count == 2
    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert (
        messages
        == [
            (
                logging.DEBUG,
                "TrueNAS nas: discovered while waiting to retry setup, retrying now",
            )
        ]
        * 2
    )


@pytest.mark.parametrize(
    ("stored", "mdns", "resolved_with"),
    [
        # A stored FQDN with trailing dot matches the mDNS name without DNS.
        ("nas.local.", "nas.local", None),
        # Userinfo and port are stripped before the host reaches the resolver.
        ("user:secret@nas.lan:8443", "other.local", "nas.lan"),
    ],
)
async def test_entry_matches_discovery_uses_stripped_host(
    stored: str, mdns: str, resolved_with: str | None
) -> None:
    """The stripped stored host is what gets compared and resolved (#195)."""
    entry = SimpleNamespace(data={CONF_HOST: stored}, state=None, title="t")
    resolve = AsyncMock(return_value={"192.168.1.50"})
    with patch.object(config_flow, "_async_resolve_host", resolve):
        assert await config_flow._async_entry_matches_discovery(
            MagicMock(), entry, {"192.168.1.50"}, mdns
        )
    if resolved_with is None:
        resolve.assert_not_awaited()
    else:
        resolve.assert_awaited_once_with(ANY, resolved_with)


async def test_resolve_host_failure_does_not_log_host(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed lookup never logs the (possibly credential-bearing) host."""
    hass = MagicMock()
    hass.loop.getaddrinfo = AsyncMock(side_effect=OSError("boom"))
    with caplog.at_level(logging.DEBUG, logger=config_flow.__name__):
        assert await config_flow._async_resolve_host(hass, "user:pa") == set()
    assert "user:pa" not in caplog.text
    assert "failed to resolve a stored host" in caplog.text


async def test_guess_ip_skips_new_lookups_while_earlier_ones_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stalled lookups of one guess must neither stall nor pile up behind the next."""
    caplog.set_level(logging.DEBUG, logger=config_flow.__name__)
    release = threading.Event()
    all_entered = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def hanging_gethostbyname(host: str) -> str:
        with lock:
            started.append(host)
            if len(started) == GUESS_CANDIDATES:
                all_entered.set()
        release.wait(5)
        raise OSError("timed out")

    try:
        with (
            patch.object(config_flow, "GUESS_HOST_TIMEOUT", 1.0),
            patch.object(
                config_flow.socket,
                "gethostbyname",
                side_effect=hanging_gethostbyname,
            ),
        ):
            assert await config_flow._async_guess_ip() == config_flow.DEFAULT_HOST
            assert all_entered.wait(2)  # every worker is stuck in the resolver

            result = await config_flow._async_guess_ip()
    finally:
        release.set()
    assert result == config_flow.DEFAULT_HOST
    # The second guess queued nothing and took the guard's early return.
    assert len(started) == GUESS_CANDIDATES
    assert (
        logging.DEBUG,
        "Lookups of an earlier host guess are still running, using "
        f"{config_flow.DEFAULT_HOST}",
    ) in [(r.levelno, r.getMessage()) for r in caplog.records]


async def test_guess_ip_reuses_last_answer_while_its_lookups_still_run() -> None:
    release = threading.Event()

    def fake_gethostbyname(host: str) -> str:
        if host == "truenas":
            return "10.0.0.1"
        release.wait(5)
        raise OSError("timed out")

    try:
        with (
            patch.object(config_flow, "GUESS_HOST_TIMEOUT", 5.0),
            patch.object(
                config_flow.socket, "gethostbyname", side_effect=fake_gethostbyname
            ),
        ):
            assert await config_flow._async_guess_ip() == "10.0.0.1"
            # Lower-priority lookups still run: no new batch, same answer.
            assert await config_flow._async_guess_ip() == "10.0.0.1"
    finally:
        release.set()


async def test_guess_ip_picks_up_an_answer_that_arrived_after_the_deadline() -> None:
    top_release = threading.Event()
    rest_release = threading.Event()

    def fake_gethostbyname(host: str) -> str:
        if host == "truenas":
            top_release.wait(5)
            return "10.0.0.1"
        rest_release.wait(5)
        raise OSError("timed out")

    try:
        with (
            patch.object(config_flow, "GUESS_HOST_TIMEOUT", 0.2),
            patch.object(
                config_flow.socket, "gethostbyname", side_effect=fake_gethostbyname
            ),
        ):
            assert await config_flow._async_guess_ip() == config_flow.DEFAULT_HOST
            top_release.set()
            top = config_flow._host_guess.lookups[0]
            await asyncio.to_thread(top.result, 2)
            # The other lookups still run, yet the late top answer is used.
            assert await config_flow._async_guess_ip() == "10.0.0.1"
    finally:
        top_release.set()
        rest_release.set()


async def test_guess_ip_runs_again_once_earlier_lookups_finished() -> None:
    release = threading.Event()

    def hanging_gethostbyname(host: str) -> str:
        release.wait(5)
        raise OSError("timed out")

    with (
        patch.object(config_flow, "GUESS_HOST_TIMEOUT", 0.2),
        patch.object(
            config_flow.socket, "gethostbyname", side_effect=hanging_gethostbyname
        ),
    ):
        try:
            assert await config_flow._async_guess_ip() == config_flow.DEFAULT_HOST
        finally:
            release.set()
        deadline = time.monotonic() + 2
        while not all(lookup.done() for lookup in config_flow._host_guess.lookups):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
    with patch.object(config_flow.socket, "gethostbyname", return_value="10.0.0.7"):
        assert await config_flow._async_guess_ip() == "10.0.0.7"


async def test_guess_ip_logs_and_skips_an_unexpected_lookup_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=config_flow.__name__)

    def fake_gethostbyname(host: str) -> str:
        if host == "truenas":
            raise RuntimeError("resolver bug")
        if host == "truenas.local":
            return "10.0.0.9"
        raise OSError("not found")

    with patch.object(
        config_flow.socket, "gethostbyname", side_effect=fake_gethostbyname
    ):
        assert await config_flow._async_guess_ip() == "10.0.0.9"
    # Visible in the debug log, and it never reaches a future as an exception.
    records = [
        r
        for r in caplog.records
        if r.getMessage() == "Unexpected error resolving truenas"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].exc_info is not None
    assert all(lookup.exception() is None for lookup in config_flow._host_guess.lookups)


async def test_guess_ip_cancels_queued_lookups_when_the_flow_is_cancelled() -> None:
    release = threading.Event()
    entered = threading.Event()
    single_worker = ThreadPoolExecutor(max_workers=1)
    called: list[str] = []

    def hanging_gethostbyname(host: str) -> str:
        called.append(host)
        entered.set()
        release.wait(5)
        raise OSError("timed out")

    with (
        patch.object(config_flow, "_HOST_GUESS_EXECUTOR", single_worker),
        patch.object(config_flow, "GUESS_HOST_TIMEOUT", 5.0),
        patch.object(
            config_flow.socket, "gethostbyname", side_effect=hanging_gethostbyname
        ),
    ):
        try:
            task = asyncio.create_task(config_flow._async_guess_ip())
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            lookups = list(config_flow._host_guess.lookups)
        finally:
            release.set()
            single_worker.shutdown(wait=True)
    # One lookup was running; every queued one was cancelled and never ran.
    assert len(lookups) == GUESS_CANDIDATES
    assert sum(lookup.cancelled() for lookup in lookups) == GUESS_CANDIDATES - 1
    assert called == ["truenas"]
