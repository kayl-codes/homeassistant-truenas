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
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL

from custom_components.truenas_ce import config_flow
from custom_components.truenas_ce.config_flow import (
    TrueNASConfigFlow,
    _map_error_to_ha,
    _sanitize_host,
    _text_to_passphrases,
    configured_instances,
)
from custom_components.truenas_ce.const import (
    DOMAIN,
    ERR_CERT_VERIFY_FAILED,
    ERR_INVALID_KEY,
    ERR_MALFORMED_RESULT,
    LEGACY_DOMAIN,
)


# ---------------------------
#   _sanitize_host
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
    ],
)
def test_sanitize_host(raw: str, expected: str) -> None:
    assert _sanitize_host(raw) == expected


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
    text = "\n  \ntank/data#secret1\n\n"
    assert _text_to_passphrases(text) == {"tank/data": "secret1"}


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
#   configured_instances
# ---------------------------
def test_configured_instances_returns_names() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [
        MagicMock(data={CONF_NAME: "nas1"}),
        MagicMock(data={CONF_NAME: "nas2"}),
    ]
    assert configured_instances(hass) == {"nas1", "nas2"}
    hass.config_entries.async_entries.assert_called_once_with(DOMAIN)


def test_configured_instances_empty() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    assert configured_instances(hass) == set()


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
    api.disconnect = AsyncMock()
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        await flow._validate_connection(config, errors)
    assert not errors
    api.disconnect.assert_awaited_once()


async def test_validate_connection_failure_maps_error() -> None:
    flow = TrueNASConfigFlow()
    config = {CONF_HOST: "nas.local", CONF_API_KEY: "key", CONF_VERIFY_SSL: True}
    errors: dict[str, str] = {}
    api = MagicMock()
    api.connection_test = AsyncMock(return_value=(False, ERR_MALFORMED_RESULT))
    api.disconnect = AsyncMock()
    with patch.object(config_flow, "TrueNASAPI", return_value=api):
        await flow._validate_connection(config, errors)
    assert errors[CONF_HOST] == ERR_MALFORMED_RESULT


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
