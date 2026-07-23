"""Full config-flow / options-flow coverage via a real HomeAssistant instance.

Unlike ``test_config_flow.py`` (which only exercises the pure/self-contained
helpers), this module drives ``TrueNASConfigFlow``/``TrueNASOptionsFlow``
through ``hass.config_entries.flow``/``hass.config_entries.options`` to cover
the user/migrate/reauth/reconfigure/options steps end to end. This requires
``pytest-homeassistant-custom-component`` for the ``hass``/
``enable_custom_integrations`` fixtures and ``MockConfigEntry``. That package
cannot be installed on this repo's Windows dev machine (its pytest11 plugin
unconditionally imports the POSIX-only ``fcntl`` module and would crash every
local pytest run), so it is only installed in CI (see
``.github/workflows/ci.yml``); ``conftest.py`` skips collecting this whole
module when the package is absent, e.g. on the local Windows dev machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas_ce.const import (
    CONF_BEHAVIORS,
    CONF_CRONJOB_SKIP_DISABLED,
    CONF_DATA_UNIT,
    CONF_DATASET_PASSPHRASES,
    CONF_MONITORED_GROUPS,
    CONF_POLL_INTERVAL,
    DEFAULT_CRONJOB_SKIP_DISABLED,
    DEFAULT_DATA_UNIT,
    DEFAULT_HOST,
    DOMAIN,
    ERR_INVALID_KEY,
    LEGACY_DOMAIN,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

_API_PATH = "custom_components.truenas_ce.config_flow.TrueNASAPI"


def _user_input(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        CONF_NAME: "TrueNAS",
        CONF_HOST: "truenas.example.com",
        CONF_API_KEY: "test-key",
        CONF_VERIFY_SSL: False,
        CONF_CRONJOB_SKIP_DISABLED: DEFAULT_CRONJOB_SKIP_DISABLED,
        CONF_DATA_UNIT: DEFAULT_DATA_UNIT,
    } | overrides
    return data


@pytest.fixture(autouse=True)
def _mock_setup_entry() -> Iterator[AsyncMock]:
    """Prevent a real integration setup from running during flow tests."""
    with patch(
        "custom_components.truenas_ce.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup


@pytest.fixture(autouse=True)
def _mock_guess_ip() -> Iterator[None]:
    """Avoid the DNS lookup pytest-homeassistant-custom-component forbids.

    ``async_step_user`` calls ``_guess_ip()`` (a real ``socket.gethostbyname``
    lookup) to prefill a default host; the test harness patches
    ``socket.gethostbyname`` to raise for any non-local/non-IP hostname.
    """
    with patch(
        "custom_components.truenas_ce.config_flow._guess_ip",
        return_value=DEFAULT_HOST,
    ):
        yield


@pytest.fixture
def _mock_connection_ok() -> Iterator[None]:
    with (
        patch(f"{_API_PATH}.connection_test", AsyncMock(return_value=(True, None))),
        patch(f"{_API_PATH}.disconnect", AsyncMock(return_value=None)),
    ):
        yield


@contextmanager
def _mock_connection_failure(errorcode: str) -> Iterator[None]:
    """Patch connection_test to fail with errorcode; disconnect stays a no-op."""
    with (
        patch(
            f"{_API_PATH}.connection_test",
            AsyncMock(return_value=(False, errorcode)),
        ),
        patch(f"{_API_PATH}.disconnect", AsyncMock(return_value=None)),
    ):
        yield


# ---------------------------
#   user step
# ---------------------------
@pytest.mark.usefixtures("_mock_connection_ok")
async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input()
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "TrueNAS"
    assert result["data"][CONF_HOST] == "truenas.example.com"


async def test_user_flow_name_already_exists(hass: HomeAssistant) -> None:
    existing = MockConfigEntry(
        domain=DOMAIN, data=_user_input(**{CONF_HOST: "other-host.example.com"})
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "name_exists"}


async def test_user_flow_aborts_on_duplicate_host(hass: HomeAssistant) -> None:
    existing = MockConfigEntry(domain=DOMAIN, data=_user_input(**{CONF_NAME: "Other"}))
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(**{CONF_NAME: "New Name"})
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_connection_error_maps_to_ha_error(hass: HomeAssistant) -> None:
    with _mock_connection_failure(ERR_INVALID_KEY):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _user_input()
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOST: ERR_INVALID_KEY}


# ---------------------------
#   migrate step
# ---------------------------
def _legacy_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=LEGACY_DOMAIN,
        data={
            CONF_NAME: "TrueNAS",
            CONF_HOST: "legacy.example.com",
            CONF_API_KEY: "old-key",
            CONF_VERIFY_SSL: True,
            CONF_CRONJOB_SKIP_DISABLED: False,
            CONF_DATA_UNIT: "GB",
        },
        options={CONF_POLL_INTERVAL: "30"},
    )


async def test_user_flow_offers_migration_when_legacy_exists(
    hass: HomeAssistant,
) -> None:
    _legacy_entry().add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "migrate"
    assert result["menu_options"] == ["migrate_import", "migrate_manual"]


@pytest.mark.usefixtures("_mock_connection_ok")
async def test_migrate_import_prefills_and_creates_entry(hass: HomeAssistant) -> None:
    _legacy_entry().add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "migrate_import"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "TrueNAS",
            CONF_HOST: "legacy.example.com",
            CONF_API_KEY: "old-key",
            CONF_VERIFY_SSL: True,
            CONF_CRONJOB_SKIP_DISABLED: False,
            CONF_DATA_UNIT: "GB",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "legacy.example.com"
    assert result["options"] == {CONF_POLL_INTERVAL: "30"}


async def test_migrate_manual_skips_takeover(hass: HomeAssistant) -> None:
    _legacy_entry().add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "migrate_manual"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


# ---------------------------
#   reauth step
# ---------------------------
@pytest.mark.usefixtures("_mock_connection_ok")
async def test_reauth_flow_updates_api_key(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=_user_input())
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new-key"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new-key"


async def test_reauth_flow_shows_error_on_failed_connection(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=_user_input())
    entry.add_to_hass(hass)

    with _mock_connection_failure(ERR_INVALID_KEY):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "bad-key"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOST: ERR_INVALID_KEY}


# ---------------------------
#   reconfigure step
# ---------------------------
@pytest.mark.usefixtures("_mock_connection_ok")
async def test_reconfigure_flow_updates_host(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=_user_input())
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "new-host.example.com", CONF_VERIFY_SSL: False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "new-host.example.com"


async def test_reconfigure_flow_rejects_unknown_dataset_passphrase(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=_user_input())
    entry.runtime_data = SimpleNamespace(ds={"dataset": {"1": {"name": "tank/data"}}})
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: entry.data[CONF_HOST],
            CONF_VERIFY_SSL: entry.data[CONF_VERIFY_SSL],
            CONF_DATASET_PASSPHRASES: "tank/unknown#secret",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DATASET_PASSPHRASES: "unknown_dataset"}
    assert result["description_placeholders"] == {"datasets": "tank/unknown"}


# ---------------------------
#   options flow
# ---------------------------
async def test_options_flow_updates_entry(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=_user_input(), options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    new_options = {
        CONF_POLL_INTERVAL: "30",
        CONF_DATA_UNIT: "GB",
        CONF_BEHAVIORS: [],
        CONF_MONITORED_GROUPS: [],
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], new_options
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == new_options
