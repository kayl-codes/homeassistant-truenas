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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_VERIFY_SSL

from custom_components.truenas_ce import config_flow
from custom_components.truenas_ce.config_flow import (
    TrueNASConfigFlow,
    _map_error_to_ha,
    _text_to_passphrases,
)
from custom_components.truenas_ce.const import (
    CONF_SYSTEM_ID,
    ERR_CERT_VERIFY_FAILED,
    ERR_CONNECTION_REFUSED,
    ERR_INVALID_KEY,
    ERR_MALFORMED_RESULT,
    LEGACY_DOMAIN,
)


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


def test_map_error_to_ha_unknown_error_falls_back() -> None:
    assert _map_error_to_ha("some_never_seen_errorcode") == "unknown"


# ---------------------------
#   _guess_ip
# ---------------------------
def test_guess_ip_returns_first_resolvable_host() -> None:
    with patch.object(config_flow.socket, "gethostbyname", return_value="10.0.0.5"):
        assert config_flow._guess_ip() == "10.0.0.5"


def test_guess_ip_falls_back_through_known_domains() -> None:
    calls: list[str] = []

    def fake_gethostbyname(host: str) -> str:
        calls.append(host)
        if len(calls) < 3:
            raise OSError("not found")
        return "10.0.0.9"

    with patch.object(
        config_flow.socket, "gethostbyname", side_effect=fake_gethostbyname
    ):
        assert config_flow._guess_ip() == "10.0.0.9"
    assert calls == ["truenas", "truenas.fritz.box", "truenas.local"]


def test_guess_ip_returns_default_host_when_none_resolve() -> None:
    with patch.object(
        config_flow.socket, "gethostbyname", side_effect=OSError("not found")
    ):
        assert config_flow._guess_ip() == config_flow.DEFAULT_HOST


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


# ---------------------------
#   TrueNASConfigFlow._probe_is_truenas
# ---------------------------
async def test_probe_is_truenas_true_on_invalid_key() -> None:
    """A bogus-key rejection proves it's a real TrueNAS JSON-RPC endpoint."""
    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.error = ERR_INVALID_KEY
    with patch.object(config_flow, "TrueNASAPI", return_value=api) as mock_api:
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") == "1.2.3.4"
    mock_api.assert_called_once_with("1.2.3.4", "-", verify_ssl=False, scheme="wss")
    api.disconnect.assert_awaited_once()


async def test_probe_is_truenas_connects_quietly() -> None:
    """Probing must not flood the log: most _http._tcp devices aren't TrueNAS.

    Regression test for the follow-up to issue #46: zeroconf discovery
    connecting to every non-TrueNAS device on the LAN was logging a full
    ERROR traceback per candidate.
    """
    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.error = ERR_INVALID_KEY
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") == "1.2.3.4"
    api.connect.assert_awaited_once_with(quiet=True)


async def test_probe_is_truenas_false_when_not_truenas() -> None:
    """Any other error means some unrelated _http._tcp device, not TrueNAS."""
    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.error = ERR_CONNECTION_REFUSED
    with patch.object(config_flow, "TrueNASAPI", return_value=api) as mock_api:
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") is None
    assert mock_api.call_count == 2
    assert api.disconnect.await_count == 2


async def test_probe_is_truenas_falls_back_from_wss_to_ws() -> None:
    """A wss failure still tries plain ws before giving up."""
    wss_api = MagicMock()
    wss_api.connect = AsyncMock()
    wss_api.disconnect = AsyncMock()
    wss_api.error = ERR_CONNECTION_REFUSED

    ws_api = MagicMock()
    ws_api.connect = AsyncMock()
    ws_api.disconnect = AsyncMock()
    ws_api.error = ERR_INVALID_KEY

    with patch.object(
        config_flow, "TrueNASAPI", side_effect=[wss_api, ws_api]
    ) as mock_api:
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") == "1.2.3.4"
    assert mock_api.call_count == 2
    mock_api.assert_any_call("1.2.3.4", "-", verify_ssl=False, scheme="ws")


async def test_probe_is_truenas_false_when_connect_raises() -> None:
    """An unexpected connect() error is treated as "not TrueNAS", not a crash."""
    api = MagicMock()
    api.connect = AsyncMock(side_effect=OSError("connection refused"))
    api.disconnect = AsyncMock()
    with patch.object(config_flow, "TrueNASAPI", return_value=api) as mock_api:
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") is None
    assert mock_api.call_count == 2
    assert api.disconnect.await_count == 2


async def test_probe_is_truenas_true_when_disconnect_raises() -> None:
    """A broken disconnect() must not abort the probe for the whole flow."""
    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock(side_effect=OSError("already closed"))
    api.error = ERR_INVALID_KEY
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4") == "1.2.3.4"
    api.disconnect.assert_awaited_once()


async def test_probe_is_truenas_falls_back_to_advertised_port() -> None:
    """A box reachable only on its advertised port keeps that port."""
    default_api = MagicMock()
    default_api.connect = AsyncMock()
    default_api.disconnect = AsyncMock()
    default_api.error = ERR_CONNECTION_REFUSED

    port_api = MagicMock()
    port_api.connect = AsyncMock()
    port_api.disconnect = AsyncMock()
    port_api.error = ERR_INVALID_KEY

    with patch.object(
        config_flow,
        "TrueNASAPI",
        side_effect=[default_api, default_api, port_api],
    ) as mock_api:
        probed = await TrueNASConfigFlow._probe_is_truenas("1.2.3.4", 8443)
    assert probed == "1.2.3.4:8443"
    mock_api.assert_any_call("1.2.3.4:8443", "-", verify_ssl=False, scheme="wss")


@pytest.mark.parametrize("port", [None, 80, 443])
async def test_probe_is_truenas_ignores_default_ports(port: int | None) -> None:
    """Default ports add nothing over the bare host, so they are not retried."""
    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.error = ERR_CONNECTION_REFUSED
    with patch.object(config_flow, "TrueNASAPI", return_value=api) as mock_api:
        assert await TrueNASConfigFlow._probe_is_truenas("1.2.3.4", port) is None
    assert mock_api.call_count == 2  # wss + ws on the bare host only


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
