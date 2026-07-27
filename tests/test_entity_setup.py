"""Real-``hass`` coverage for entity.py's platform-setup wiring.

``_cleanup_orphaned_entities`` and ``async_add_entities`` both need a real
entity/device registry (``er.async_get``/``dr.async_get``) and, for
``async_add_entities``, the ``entity_platform`` context var that only exists
while a platform's own ``async_setup_entry`` is actually running -- none of
this can be faked with a bare-instance/``SimpleNamespace`` coordinator, unlike
most of this test suite. Like ``test_config_flow_flows.py``/``test_services.py``,
this therefore needs ``pytest-homeassistant-custom-component`` and only runs in
CI (see ``conftest.py``'s ``collect_ignore_glob`` for why it can't run on this
repo's Windows dev machine).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.truenas_ce as init_module
from custom_components.truenas_ce.const import CONF_MONITORED_GROUPS, DOMAIN
from custom_components.truenas_ce.entity import _cleanup_orphaned_entities

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


# ---------------------------
#   _cleanup_orphaned_entities
# ---------------------------
async def test_cleanup_orphaned_entities_removes_stale_entity_and_empty_device(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_NAME: "TrueNAS"})
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(last_update_success=True)

    dev_reg = dr.async_get(hass)
    live_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "live-device")}
    )
    empty_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "empty-device")}
    )

    ent_reg = er.async_get(hass)
    active_entity = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "truenas-active-id",
        config_entry=entry,
        device_id=live_device.id,
    )
    orphan_entity = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "truenas-live-base-orphan",
        config_entry=entry,
        device_id=empty_device.id,
    )
    unrelated_entity = ent_reg.async_get_or_create(
        "sensor", DOMAIN, "truenas-unrelated-id", config_entry=entry
    )

    with patch.object(
        init_module,
        "_collect_active_unique_ids",
        return_value=({"truenas-active-id"}, {"truenas-live-base"}),
    ):
        _cleanup_orphaned_entities(hass, entry, coordinator)

    assert ent_reg.async_get(active_entity.entity_id) is not None
    assert ent_reg.async_get(orphan_entity.entity_id) is None
    assert ent_reg.async_get(unrelated_entity.entity_id) is not None
    assert dev_reg.async_get(live_device.id) is not None
    assert dev_reg.async_get(empty_device.id) is None


async def test_cleanup_orphaned_entities_noop_after_failed_refresh(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_NAME: "TrueNAS"})
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(last_update_success=False)

    ent_reg = er.async_get(hass)
    stale_entity = ent_reg.async_get_or_create(
        "sensor", DOMAIN, "truenas-live-base-orphan", config_entry=entry
    )

    with patch.object(init_module, "_collect_active_unique_ids") as collect_mock:
        _cleanup_orphaned_entities(hass, entry, coordinator)
    collect_mock.assert_not_called()
    assert ent_reg.async_get(stale_entity.entity_id) is not None


# ---------------------------
#   async_add_entities (via a real platform-setup pass)
# ---------------------------
def _fake_api() -> SimpleNamespace:
    """A fake TrueNASAPI returning an empty (but present) system.info payload.

    Every other query returns None, matching the coordinator's normal handling
    of a not-yet-responding TrueNAS -- this keeps the platform-forward pass
    real while avoiding any actual network I/O.
    """

    async def _query(method: str, *args: object, **kwargs: object) -> dict | None:
        return {} if method == "system.info" else None

    return SimpleNamespace(
        connected=MagicMock(return_value=True),
        connect=AsyncMock(return_value=True),
        close=AsyncMock(),
        query=AsyncMock(side_effect=_query),
        error="",
        scheme="ws",
    )


async def test_async_setup_entry_creates_entities_via_real_platform_setup(
    hass: HomeAssistant,
) -> None:
    """A real ``async_setup_entry`` run forwards to every platform, so this
    exercises ``entity.async_add_entities``'s live wiring (service
    registration, dispatcher-connect, coordinator-identity check, entity
    creation) exactly as production does -- not reachable via a bare-instance
    coordinator since it needs the real ``entity_platform`` context var.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "TrueNAS",
            CONF_HOST: "truenas.local",
            CONF_API_KEY: "test-key",
            CONF_VERIFY_SSL: False,
        },
        options={CONF_MONITORED_GROUPS: []},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.truenas_ce.coordinator.TrueNASAPI",
        return_value=_fake_api(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.async_entity_ids("sensor")
