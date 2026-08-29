"""Unit tests for the pure/self-contained helpers and mockable logic in
coordinator.py.

Like ``config_flow.py``, this module uses relative imports and must be loaded
as a real package module. ``TrueNASCoordinator`` normally requires a running
Home Assistant (``__init__`` builds a real ``DataUpdateCoordinator``), which
``pytest-homeassistant-custom-component`` would be needed for -- unusable on
this repo's Windows dev machine (see the memory note on that incompatibility).
Instead, instance methods here are tested by constructing a bare instance via
``TrueNASCoordinator.__new__`` and setting only the attributes each method
under test actually touches, mirroring the Mock/AsyncMock approach already
used for ``TrueNASConfigFlow``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import slugify

from custom_components.truenas_ce import coordinator as coordinator_module
from custom_components.truenas_ce.const import (
    BEHAVIOR_SKIP_DISABLED_CRONJOBS,
    CONF_BEHAVIORS,
    CONF_MONITORED_GROUPS,
    CONF_POLL_INTERVAL,
    CONF_STATISTICS_CLEANUP_IGNORED,
    DEFAULT_DEVICE_NAME,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    LEGACY_DOMAIN,
    MIGRATION_LEGACY_ENTRY_ID,
    MIGRATION_RECORDS,
    MONITOR_GROUP_CLOUDSYNC,
    MONITOR_GROUP_CONTAINERS,
    MONITOR_GROUP_CRONJOBS,
    MONITOR_GROUP_DATASETS,
    MONITOR_GROUP_DIRECTORY_SERVICES,
    MONITOR_GROUP_REPLICATION,
    MONITOR_GROUP_RSYNC,
    MONITOR_GROUP_SNAPSHOTS,
    MONITOR_GROUP_UPS,
    MONITOR_GROUP_VMS,
)
from custom_components.truenas_ce.coordinator import (
    SubscriptionCircuitBreaker,
    TrueNASCoordinator,
    _count_statistics_with_data,
    _is_truenas_sensor_id,
    _PushSourceState,
    _stat_name_similar,
)


def _bare_coordinator() -> TrueNASCoordinator:
    """Build a TrueNASCoordinator without running its hass-dependent __init__."""
    coord = TrueNASCoordinator.__new__(TrueNASCoordinator)
    coord._app_stats_event_name = None
    coord._app_stats_sub_id = None
    coord._alerts_sub_id = None
    coord._alerts_push_consumer = None
    coord._alerts_breaker = SubscriptionCircuitBreaker()
    coord._service_push = _PushSourceState()
    coord._pool_push = _PushSourceState()
    coord._cloudsync_push = _PushSourceState()
    coord._replication_push = _PushSourceState()
    coord._rsync_push = _PushSourceState()
    coord._vm_push = _PushSourceState()
    coord._container_push = _PushSourceState()
    coord._app_push = _PushSourceState()
    coord.orphaned_statistics = []
    coord.last_updatecheck_update = datetime(1970, 1, 1, tzinfo=UTC)
    coord.host = "truenas.local"
    coord._version_major = 0
    coord._version_minor = 0
    coord._poisoned_certificate_commons = set()
    return coord


# ---------------------------
#   _stat_name_similar
# ---------------------------
@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("cpu", "cpu", False),
        ("arc_size", "arcsize", True),
        ("cputemp", "cpu", True),
        ("cpu", "cputemp", True),
        ("memroy", "memory", True),
        ("load", "interface", False),
    ],
)
def test_stat_name_similar(a: str, b: str, expected: bool) -> None:
    assert _stat_name_similar(a, b) == expected


# ---------------------------
#   _is_truenas_sensor_id
# ---------------------------
def test_is_truenas_sensor_id_matches_device_slug_token() -> None:
    slug = slugify(DEFAULT_DEVICE_NAME)
    assert _is_truenas_sensor_id(f"sensor.{slug}_cpu_usage", slug) is True
    assert _is_truenas_sensor_id(f"sensor.system_{slug}_uptime", slug) is True
    assert _is_truenas_sensor_id(f"sensor.{slug}viacfnoauth_cpu", slug) is True


def test_is_truenas_sensor_id_rejects_other_domains() -> None:
    slug = slugify(DEFAULT_DEVICE_NAME)
    assert _is_truenas_sensor_id("sensor.unrelated_integration_temp", slug) is False


def test_is_truenas_sensor_id_rejects_non_sensor_entities() -> None:
    slug = slugify(DEFAULT_DEVICE_NAME)
    assert _is_truenas_sensor_id(f"binary_sensor.{slug}_online", slug) is False


def test_is_truenas_sensor_id_unaffected_by_domain_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: matching used to depend on DOMAIN/LEGACY_DOMAIN, which broke
    since the 2.0.0 CE rename because DOMAIN ("truenas_ce") contains an
    underscore and can never appear whole inside an underscore-split token.
    The fix matches the device-name slug instead -- the same string real
    entity ids are slugged from -- so behavior no longer depends on
    DOMAIN/LEGACY_DOMAIN at all, even if both constants are ever renamed or
    removed (e.g. a future HA Core submission dropping the "_ce" suffix).
    """
    monkeypatch.setattr(coordinator_module, "DOMAIN", "something_else_entirely")
    monkeypatch.setattr(coordinator_module, "LEGACY_DOMAIN", "unrelated")
    assert _is_truenas_sensor_id("sensor.truenas_cpu_usage", "truenas") is True


def test_is_truenas_sensor_id_scoped_to_this_entrys_device_slug() -> None:
    """Regression (#61): a global slug match flagged every entry's orphans on
    multi-entry installs. Each entry must only match its own device slug.
    """
    assert (
        _is_truenas_sensor_id("sensor.truenas_nuc13_cpu_usage", "truenas_nuc13") is True
    )
    assert (
        _is_truenas_sensor_id("sensor.truenas_x11dpu_cpu_usage", "truenas_nuc13")
        is False
    )


def test_is_truenas_sensor_id_rejects_empty_device_slug() -> None:
    """An empty slug (e.g. a blank device name) must never match every id."""
    assert _is_truenas_sensor_id("sensor.truenas_nuc13_cpu_usage", "") is False


# ---------------------------
#   _is_group_monitored
# ---------------------------
def test_is_group_monitored_true_when_in_options() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_VMS]}
    assert coord._is_group_monitored(MONITOR_GROUP_VMS) is True


def test_is_group_monitored_false_when_absent() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    assert coord._is_group_monitored(MONITOR_GROUP_VMS) is False


# ---------------------------
#   set_optimistic_running
# ---------------------------
def test_set_optimistic_running_sets_state_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"vm": {"1": {"state": "STOPPED"}}}
    coord.async_update_listeners = MagicMock()
    coord.set_optimistic_running("vm", "1")
    assert coord.ds["vm"]["1"]["state"] == "RUNNING"
    coord.async_update_listeners.assert_called_once()


def test_set_optimistic_running_noop_for_unknown_object_id() -> None:
    coord = _bare_coordinator()
    coord.ds = {"vm": {"1": {"state": "STOPPED"}}}
    coord.async_update_listeners = MagicMock()
    coord.set_optimistic_running("vm", "does-not-exist")
    assert coord.ds["vm"]["1"]["state"] == "STOPPED"
    coord.async_update_listeners.assert_not_called()


def test_set_optimistic_running_matches_int_object_id_against_str_keyed_ds() -> None:
    """Migrated endpoints (rsynctask, replication, snapshottask, scrub, ...)
    pass the raw ``id`` field from entity data, which is still int-typed at
    the API level, while self.ds is str-keyed end to end (see
    ``_as_str_keyed``) -- the lookup must convert, not fail to match."""
    coord = _bare_coordinator()
    coord.ds = {"rsynctask": {"1": {"state": "STOPPED"}}}
    coord.async_update_listeners = MagicMock()
    coord.set_optimistic_running("rsynctask", 1)
    assert coord.ds["rsynctask"]["1"]["state"] == "RUNNING"
    coord.async_update_listeners.assert_called_once()


# ---------------------------
#   async_run_task
# ---------------------------
async def test_async_run_task_marks_running_on_success() -> None:
    coord = _bare_coordinator()
    coord.ds = {"rsynctask": {"1": {"state": "STOPPED"}}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=42)
    coord.api.error = ""
    coord.async_update_listeners = MagicMock()
    await coord.async_run_task("rsynctask.run", "1", "rsynctask")
    assert coord.ds["rsynctask"]["1"]["state"] == "RUNNING"


async def test_async_run_task_raises_and_skips_optimistic_state_on_failure() -> None:
    coord = _bare_coordinator()
    coord.ds = {"rsynctask": {"1": {"state": "STOPPED"}}}
    coord.host = "truenas.local"
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=None)
    coord.api.error = "ERR_LOST_QUERY"
    coord.async_update_listeners = MagicMock()
    with pytest.raises(HomeAssistantError) as exc_info:
        await coord.async_run_task("rsynctask.run", "1", "rsynctask")
    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "run_task_failed"
    assert exc_info.value.translation_placeholders == {
        "host": "truenas.local",
        "error": "ERR_LOST_QUERY",
    }
    assert coord.ds["rsynctask"]["1"]["state"] == "STOPPED"
    coord.async_update_listeners.assert_not_called()


# ---------------------------
#   _parse_version
# ---------------------------
def test_parse_version_extracts_major_minor() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"version": "TrueNAS-SCALE-25.04.1"}}
    coord._parse_version()
    assert coord._version_major == 25
    assert coord._version_minor == 4


def test_parse_version_leaves_unset_on_no_match() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"version": "not-a-version-string"}}
    coord._version_major = 0
    coord._version_minor = 0
    coord._parse_version()
    assert coord._version_major == 0
    assert coord._version_minor == 0


# ---------------------------
#   _detect_virtualization
# ---------------------------
def test_detect_virtualization_true_for_known_manufacturer() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"system_manufacturer": "QEMU", "system_product": ""}}
    coord._detect_virtualization()
    assert coord._is_virtual is True


def test_detect_virtualization_true_for_known_product() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "system_info": {"system_manufacturer": "", "system_product": "VirtualBox"}
    }
    coord._detect_virtualization()
    assert coord._is_virtual is True


def test_detect_virtualization_false_for_physical_hardware() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "system_info": {"system_manufacturer": "Dell Inc.", "system_product": "R730"}
    }
    coord._detect_virtualization()
    assert coord._is_virtual is False


# ---------------------------
#   _update_uptime
# ---------------------------
def test_update_uptime_sets_epoch_on_first_run() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"uptime_seconds": 3600, "uptimeEpoch": 0}}
    coord._update_uptime()
    assert coord.ds["system_info"]["uptimeEpoch"] > 0


def test_update_uptime_keeps_old_epoch_within_tolerance() -> None:
    coord = _bare_coordinator()
    now_epoch = int(datetime.now(UTC).timestamp())
    old_epoch = now_epoch - 3600 + 5  # within the 300s tolerance of a fresh reading
    coord.ds = {"system_info": {"uptime_seconds": 3600, "uptimeEpoch": old_epoch}}
    coord._update_uptime()
    assert coord.ds["system_info"]["uptimeEpoch"] == old_epoch


def test_update_uptime_replaces_stale_epoch_outside_tolerance() -> None:
    coord = _bare_coordinator()
    now_epoch = int(datetime.now(UTC).timestamp())
    old_epoch = now_epoch - 3600 - 600  # 600s drift, well beyond the 300s tolerance
    coord.ds = {"system_info": {"uptime_seconds": 3600, "uptimeEpoch": old_epoch}}
    coord._update_uptime()
    new_epoch = coord.ds["system_info"]["uptimeEpoch"]
    assert new_epoch != old_epoch
    # Replaced by a freshly computed epoch (now - uptime_seconds).
    assert abs(new_epoch - (now_epoch - 3600)) <= 5


def test_update_uptime_skips_when_uptime_not_positive() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"uptime_seconds": 0, "uptimeEpoch": 123}}
    coord._update_uptime()
    assert coord.ds["system_info"]["uptimeEpoch"] == 123


# ---------------------------
#   _systemstats_process / _store_stat_value / _store_stat_defaults
# ---------------------------
def test_systemstats_process_stores_matching_legend_values() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    graph = {
        "legend": ["shortterm", "midterm", "longterm"],
        "aggregations": {"mean": {"shortterm": 1.234, "midterm": 2.0}},
    }
    coord._systemstats_process(("shortterm", "midterm", "longterm"), graph, "load")
    assert coord.ds["system_info"]["load_shortterm"] == pytest.approx(1.23)
    assert coord.ds["system_info"]["load_midterm"] == pytest.approx(2.0)
    # "longterm" is in the legend but missing from the mean dict, so it falls
    # back to 0.0 rather than being skipped.
    assert coord.ds["system_info"]["load_longterm"] == pytest.approx(0.0)


def test_systemstats_process_falls_back_to_defaults_without_aggregations() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._systemstats_process("cpu", {}, "cpu")
    assert coord.ds["system_info"]["cpu_cpu"] == pytest.approx(0.0)


def test_systemstats_process_defaults_use_dedicated_keys() -> None:
    """Defaults for a malformed graph land under the same key as a real value.

    Regression test for a bug where defaults bypassed the type-specific key
    mapping in _store_stat_value and were written under the bare var name
    instead, leaving the actually-exposed sensor keys stale.
    """
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._systemstats_process("size", {}, "arcsize")
    assert coord.ds["system_info"]["cache_size-arc_value"] == 0.0
    coord._systemstats_process("available", {}, "memory")
    assert coord.ds["system_info"]["memory-free_value"] == 0


def test_systemstats_process_skips_legend_var_not_in_arr() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    graph = {
        "legend": ["shortterm", "other"],
        "aggregations": {"mean": {"shortterm": 1.0, "other": 99.0}},
    }
    coord._systemstats_process(("shortterm",), graph, "load")
    assert coord.ds["system_info"]["load_shortterm"] == pytest.approx(1.0)
    assert "load_other" not in coord.ds["system_info"]


def test_store_stat_value_arcsize_uses_dedicated_key() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._store_stat_value("arcsize", "size", 12.345)
    assert coord.ds["system_info"]["cache_size-arc_value"] == pytest.approx(12.35)


def test_store_stat_value_cpu_uses_prefixed_key() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._store_stat_value("cpu", "cpu", 12.345)
    assert coord.ds["system_info"]["cpu_cpu"] == pytest.approx(12.35)


def test_store_stat_value_memory_only_stores_available() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._store_stat_value("memory", "available", 100.0)
    assert coord.ds["system_info"]["memory-free_value"] == 100
    coord._store_stat_value("memory", "used", 50.0)
    assert "memory-used" not in coord.ds["system_info"]


def test_store_stat_value_unknown_type_stores_raw_key() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._store_stat_value("diskstats", "reads", 12.345)
    assert coord.ds["system_info"]["reads"] == pytest.approx(12.35)


# ---------------------------
#   _rollback_possible / issue-id builders
# ---------------------------
def test_rollback_possible_false_when_domain_is_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord = _bare_coordinator()
    monkeypatch.setattr(coordinator_module, "DOMAIN", LEGACY_DOMAIN)
    assert coord._rollback_possible() is False


def test_rollback_possible_true_when_legacy_entry_exists() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.data = {MIGRATION_LEGACY_ENTRY_ID: "legacy-id-1"}
    coord.hass = MagicMock()
    coord.hass.config_entries.async_get_entry.return_value = MagicMock()
    assert coord._rollback_possible() is True


def test_rollback_possible_false_when_no_legacy_id_recorded() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.data = {}
    coord.hass = MagicMock()
    assert coord._rollback_possible() is False


def test_statistics_issue_id_includes_entry_id() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry123"
    assert coord._statistics_issue_id() == "statistics_orphaned_entry123"


def test_migration_rollback_issue_id_includes_entry_id() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry123"
    assert (
        coord._migration_rollback_issue_id() == "migration_rollback_available_entry123"
    )


# ---------------------------
#   get_alerts
# ---------------------------
# The dismissed-filtering/level-counting/disk_issues-heuristic logic these
# tests used to exercise directly now lives in and is tested by aiotruenas's
# own TrueNASState.get_alerts() (see tests/test_domain_state.py in that
# repo). _refresh_alerts just delegates and assigns the result, so this only
# needs to lock in that plumbing.
async def test_refresh_alerts_delegates_to_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {"alerts": {}}
    coord.state = MagicMock()
    coord.state.get_alerts = AsyncMock(
        return_value={
            "count": 1,
            "messages": ["Pool full"],
            "critical": 1,
            "warning": 0,
            "info": 0,
            "disk_issues": True,
            "uuids": ["u1"],
        }
    )
    await coord._refresh_alerts()
    assert coord.ds["alerts"]["count"] == 1
    assert coord.ds["alerts"]["uuids"] == ["u1"]


# ---------------------------
#   alerts push subscription
# ---------------------------
def _hass_with_background_tasks() -> MagicMock:
    """A hass stub whose async_create_background_task really schedules the coro.

    A plain MagicMock would return a mock without ever awaiting the passed
    coroutine, leaving it un-awaited (and the consumer loop never running) --
    real ``asyncio.create_task`` is needed so ``SubscriptionPushConsumer``
    behaves like it does in production.
    """
    hass = MagicMock()
    hass.async_create_background_task = lambda coro, name: asyncio.create_task(
        coro, name=name
    )
    return hass


async def test_ensure_alerts_subscription_noop_when_not_connected() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.subscribe_events = AsyncMock()

    await coord._ensure_alerts_subscription()

    coord.api.subscribe_events.assert_not_awaited()
    assert coord._alerts_sub_id is None


async def test_ensure_alerts_subscription_subscribes_once() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))

    await coord._ensure_alerts_subscription()

    coord.api.subscribe_events.assert_awaited_once_with("alert.list")
    assert coord._alerts_sub_id == "sub-1"
    assert coord._alerts_push_consumer is not None
    await coord._alerts_push_consumer.stop()


async def test_ensure_alerts_subscription_noop_when_already_active() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.is_subscribed = AsyncMock(return_value=True)
    coord.api.subscribe_events = AsyncMock()
    coord._alerts_sub_id = "sub-existing"

    await coord._ensure_alerts_subscription()

    coord.api.subscribe_events.assert_not_awaited()
    assert coord._alerts_sub_id == "sub-existing"


async def test_ensure_alerts_subscription_resubscribes_when_stale() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.is_subscribed = AsyncMock(return_value=False)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", asyncio.Queue()))
    coord._alerts_sub_id = "sub-stale"

    await coord._ensure_alerts_subscription()

    coord.api.subscribe_events.assert_awaited_once_with("alert.list")
    assert coord._alerts_sub_id == "sub-new"
    await coord._alerts_push_consumer.stop()


async def test_ensure_alerts_subscription_stops_orphaned_consumer_when_stale() -> None:
    """A stale alerts subscription must stop the old consumer's background
    task, not just drop the local reference, or it keeps running orphaned
    and delivering duplicate refreshes (#101 review, same bug as the
    generic _ensure_push_subscription path)."""
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.is_subscribed = AsyncMock(return_value=False)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    coord._alerts_sub_id = "sub-stale"
    old_consumer = coord._alerts_push_consumer = MagicMock()
    old_consumer.stop = AsyncMock()

    await coord._ensure_alerts_subscription()

    old_consumer.stop.assert_awaited_once()
    assert coord._alerts_push_consumer is not old_consumer
    await coord._alerts_push_consumer.stop()


async def test_ensure_alerts_subscription_handles_subscribe_failure() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(side_effect=Exception("subscribe failed"))

    await coord._ensure_alerts_subscription()

    assert coord._alerts_sub_id is None
    assert coord._alerts_push_consumer is None


async def test_ensure_alerts_subscription_handles_no_sub_id_returned() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=(None, None))

    await coord._ensure_alerts_subscription()

    assert coord._alerts_sub_id is None


async def test_ensure_alerts_subscription_respects_breaker_cooldown() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock()
    for _ in range(3):  # default config trips after 3 consecutive breaches
        coord._alerts_breaker.record_batch(9999)

    await coord._ensure_alerts_subscription()

    coord.api.subscribe_events.assert_not_awaited()
    assert coord._alerts_sub_id is None


async def test_on_alerts_push_refreshes_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"alerts": {}}
    coord.state = MagicMock()
    coord.state.get_alerts = AsyncMock(return_value={"count": 1})
    coord.async_set_updated_data = MagicMock()

    await coord._on_alerts_push([{"msg": "removed"}])

    assert coord.ds["alerts"]["count"] == 1
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


async def test_on_alerts_breaker_trip_stops_subscription() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock()
    coord._alerts_sub_id = "sub-1"

    await coord._on_alerts_breaker_trip()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._alerts_sub_id is None
    assert coord._alerts_push_consumer is None


async def test_stop_alerts_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    await coord._ensure_alerts_subscription()

    await coord.stop_alerts()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._alerts_sub_id is None
    assert coord._alerts_push_consumer is None


async def test_stop_alerts_noop_when_not_subscribed() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock()

    await coord.stop_alerts()

    coord.api.unsubscribe_events.assert_not_awaited()
    assert coord._alerts_sub_id is None


# ---------------------------
#   get_smb
# ---------------------------
# The list/dict-with-sessions parsing and malformed-response fallback these
# tests used to exercise directly now live in and are tested by aiotruenas's
# own TrueNASState.get_smb(). get_smb just merges the result's "connections"
# key into ds["system_info"], so this only needs to lock in that plumbing.
async def test_get_smb_merges_connections_into_system_info() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord.state = MagicMock()
    coord.state.get_smb = AsyncMock(return_value={"connections": 2})
    await coord.get_smb()
    assert coord.ds["system_info"]["smb_connections"] == 2


async def test_get_smb_keeps_previous_count_when_key_absent() -> None:
    """TrueNASState.get_smb() omits "connections" on a malformed/failed
    response instead of publishing a false "0 connections"."""
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"smb_connections": 3}}
    coord.state = MagicMock()
    coord.state.get_smb = AsyncMock(return_value={})
    await coord.get_smb()
    assert coord.ds["system_info"]["smb_connections"] == 3


# ---------------------------
#   get_updatecheck
# ---------------------------
# The update.status parsing/malformed-response handling these tests used to
# exercise directly now live in and are tested by aiotruenas's own
# TrueNASState.get_update(). get_updatecheck just merges the result into
# ds["system_info"], so this only needs to lock in that plumbing.
async def test_get_updatecheck_no_update_falls_back_to_running_version() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"version": "25.04.1"}}
    coord.state = MagicMock()
    coord.state.get_update = AsyncMock(
        return_value={
            "update_available": False,
            "update_state": "IDLE",
            "update_version": "up-to-date",
            "update_date": None,
            "update_profile": None,
            "update_train": None,
            "update_filename": None,
        }
    )
    await coord.get_updatecheck()
    info = coord.ds["system_info"]
    assert info["update_available"] is False
    assert info["update_version"] == "25.04.1"


async def test_get_updatecheck_pending_update_keeps_new_version() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"version": "25.04.1"}}
    coord.state = MagicMock()
    coord.state.get_update = AsyncMock(
        return_value={
            "update_available": True,
            "update_state": "AVAILABLE",
            "update_version": "25.10.0",
            "update_date": None,
            "update_profile": None,
            "update_train": None,
            "update_filename": None,
        }
    )
    await coord.get_updatecheck()
    info = coord.ds["system_info"]
    assert info["update_available"] is True
    assert info["update_version"] == "25.10.0"


async def test_start_app_stats_stops_when_containers_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {}},
        "app_stats": {"old-app": {"app_name": "old-app"}},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", MagicMock()))
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord._app_stats_sub_id = "sub-old"
    coord._app_stats_event_name = 'app.stats:{"interval": 5}'

    with patch.object(coord, "stop_app_stats", new=AsyncMock()) as stop_mock:
        await coord.start_app_stats()

    stop_mock.assert_awaited_once_with(force=True)
    assert coord.ds["app_stats"] == {}


async def test_start_app_stats_clears_stats_when_never_subscribed() -> None:
    """Containers unmonitored and never subscribed: clear stats, no stop."""
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {}},
        "app_stats": {"old-app": {"app_name": "old-app"}},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", MagicMock()))
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord._app_stats_sub_id = None
    coord._app_stats_event_name = None

    with patch.object(coord, "stop_app_stats", new=AsyncMock()) as stop_mock:
        await coord.start_app_stats()

    stop_mock.assert_not_awaited()
    assert coord.ds["app_stats"] == {}


async def test_start_app_stats_defaults_when_config_entry_missing() -> None:
    """start_app_stats should treat groups as monitored when config_entry is None."""
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", MagicMock()))
    coord.config_entry = None
    coord._app_stats_sub_id = None
    coord._app_stats_event_name = 'app.stats:{"interval": 5}'

    with patch.object(
        coord, "_is_group_monitored", wraps=coord._is_group_monitored
    ) as monitored_mock:
        await coord.start_app_stats()

    monitored_mock.assert_called()
    coord.api.subscribe_events.assert_awaited_once()


async def test_start_app_stats_defaults_when_monitored_groups_missing() -> None:
    """Treat groups as monitored when CONF_MONITORED_GROUPS is absent."""
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", MagicMock()))
    coord.config_entry = MagicMock()
    coord.config_entry.options = {}
    coord._app_stats_sub_id = None
    coord._app_stats_event_name = 'app.stats:{"interval": 5}'

    with patch.object(
        coord, "_is_group_monitored", wraps=coord._is_group_monitored
    ) as monitored_mock:
        await coord.start_app_stats()

    monitored_mock.assert_called()
    coord.api.subscribe_events.assert_awaited_once()


async def test_start_app_stats_noops_when_api_not_connected() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {}},
        "app_stats": {"existing-app": {"app_name": "existing-app"}},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.subscribe_events = AsyncMock()
    coord.config_entry = MagicMock()
    coord.config_entry.options = {
        CONF_MONITORED_GROUPS: ["app", MONITOR_GROUP_CONTAINERS]
    }
    coord._app_stats_sub_id = None
    coord._app_stats_event_name = 'app.stats:{"interval": 5}'

    with patch.object(coord, "stop_app_stats", new=AsyncMock()) as stop_mock:
        await coord.start_app_stats()

    coord.api.subscribe_events.assert_not_called()
    stop_mock.assert_not_awaited()
    assert coord.ds["app_stats"] == {
        "existing-app": {"app_name": "existing-app"},
    }


async def test_start_app_stats_keeps_existing_sub_when_no_apps() -> None:
    """With no apps and same event name, start_app_stats keeps the existing sub."""
    coord = _bare_coordinator()
    coord.ds = {"app": {}, "app_stats": {"existing-app": {"app_name": "existing-app"}}}

    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", MagicMock()))
    coord.api.is_subscribed = AsyncMock(return_value=True)

    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_CONTAINERS]}

    coord._app_stats_sub_id = "sub-old"
    coord._app_stats_event_name = 'app.stats:{"interval": 60}'

    await coord.start_app_stats()

    coord.api.subscribe_events.assert_not_awaited()
    assert coord._app_stats_sub_id == "sub-old"
    assert coord._app_stats_event_name == 'app.stats:{"interval": 60}'


async def test_get_app_stats_clears_when_containers_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app_stats": {"stale-app": {"cpu": 1}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    coord.stop_app_stats = AsyncMock()
    coord._app_stats_sub_id = "sub-1"

    await coord.get_app_stats()

    coord.stop_app_stats.assert_awaited_once_with(force=True)
    assert coord.ds["app_stats"] == {}


async def test_get_app_stats_does_nothing_when_disconnected_mid_call() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {"test-app": {"cpu": 1, "memory": 2}},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.get_subscription_events = AsyncMock()
    coord.api.is_subscribed = AsyncMock()
    coord._app_stats_sub_id = "existing-sub-id"

    original_ds = coord.ds.copy()
    original_sub_id = coord._app_stats_sub_id

    await coord.get_app_stats()

    coord.api.get_subscription_events.assert_not_called()
    assert coord.ds == original_ds
    assert coord._app_stats_sub_id == original_sub_id


async def test_get_app_stats_does_nothing_when_no_apps() -> None:
    """No apps: get_app_stats is a no-op."""
    coord = _bare_coordinator()
    coord.ds = {
        "app": {},
        "app_stats": {"existing-app": {"app_name": "existing-app"}},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock()
    coord.api.is_subscribed = AsyncMock(return_value=True)
    coord._app_stats_sub_id = "sub-1"

    await coord.get_app_stats()

    coord.api.get_subscription_events.assert_not_called()
    assert coord.ds["app_stats"] == {"existing-app": {"app_name": "existing-app"}}


async def test_get_app_stats_re_subscribes_when_sub_id_missing() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(return_value=[])
    coord.api.is_subscribed = AsyncMock(return_value=False)
    coord._app_stats_sub_id = None

    with patch.object(coord, "start_app_stats", new_callable=AsyncMock) as start_mock:
        await coord.get_app_stats()

    start_mock.assert_awaited_once()


async def test_get_app_stats_re_subscribes_when_existing_sub_not_active() -> None:
    """If sub_id exists but api.is_subscribed is False, clear and resubscribe."""
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    original_sub_id = "sub-1"
    coord._app_stats_sub_id = original_sub_id

    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(return_value=[])
    coord.api.is_subscribed = AsyncMock(return_value=False)

    with patch.object(coord, "start_app_stats", new_callable=AsyncMock) as start_mock:
        await coord.get_app_stats()

    start_mock.assert_awaited_once()


async def test_get_app_stats_skips_malformed_app_name() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(
        return_value=[
            {"fields": [{"app_name": 123}]},
            {"fields": [{"app_name": "", "cpu_usage": 2.0}]},
            {"fields": [{"app_name": "test-app", "cpu_usage": 1.0}]},
        ]
    )
    coord._app_stats_sub_id = "sub-1"
    coord.api.is_subscribed = AsyncMock(return_value=True)

    await coord.get_app_stats()

    assert "test-app" in coord.ds["app_stats"]
    assert 123 not in coord.ds["app_stats"]
    assert "" not in coord.ds["app_stats"]


# ---------------------------
#   start_app_stats / get_app_stats / stop_app_stats
# ---------------------------
async def test_start_app_stats_subscribes_once() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"test-app": {}}}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", MagicMock()))
    coord.config_entry = MagicMock()
    coord.config_entry.options = {}
    coord._app_stats_sub_id = None

    await coord.start_app_stats()

    coord.api.subscribe_events.assert_awaited_once()
    assert coord._app_stats_sub_id == "sub-1"


async def test_start_app_stats_clears_stale_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"test-app": {}}}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", MagicMock()))
    coord.api.unsubscribe_events = AsyncMock()
    coord.config_entry = MagicMock()
    coord.config_entry.options = {}
    coord._app_stats_sub_id = "sub-old"
    coord._app_stats_event_name = 'app.stats:{"interval": 5}'

    await coord.start_app_stats()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-old")
    coord.api.subscribe_events.assert_awaited_once()
    assert coord._app_stats_sub_id == "sub-new"


async def test_start_app_stats_handles_subscribe_failure() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"test-app": {}}}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(side_effect=Exception("subscribe failed"))
    coord.config_entry = MagicMock()
    coord.config_entry.options = {}
    coord._app_stats_sub_id = None

    await coord.start_app_stats()

    assert coord._app_stats_sub_id is None


async def test_get_app_stats_processes_and_updates_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(
        return_value=[
            {
                "fields": [
                    {
                        "app_name": "test-app",
                        "cpu_usage": 12.5,
                        "memory": 1024000,
                        "blkio": {"read": 5000, "write": 2000},
                        "networks": [
                            {
                                "interface_name": "eth0",
                                "rx_bytes": 1000,
                                "tx_bytes": 500,
                            }
                        ],
                    }
                ]
            }
        ]
    )
    coord._app_stats_sub_id = "sub-1"
    coord.api.is_subscribed = AsyncMock(return_value=True)

    await coord.get_app_stats()

    assert coord.ds["app_stats"]["test-app"]["app_name"] == "test-app"
    assert coord.ds["app_stats"]["test-app"]["cpu_usage"] == pytest.approx(12.5)
    assert coord.ds["app_stats"]["test-app"]["memory"] == 1024000
    assert coord.ds["app_stats"]["test-app"]["blkio_read"] == 5000
    assert coord.ds["app_stats"]["test-app"]["blkio_write"] == 2000
    assert coord.ds["app_stats"]["test-app"]["networks"] == [
        {"interface_name": "eth0", "rx_bytes": 1000, "tx_bytes": 500}
    ]


async def test_get_app_stats_removes_missing_apps() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {
            "test-app": {"name": "test-app"},
        },
        "app_stats": {
            "test-app": {"app_name": "test-app"},
            "old-app": {"app_name": "old-app"},
        },
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(return_value=[])
    coord._app_stats_sub_id = "sub-1"
    coord.api.is_subscribed = AsyncMock(return_value=True)

    await coord.get_app_stats()

    assert "test-app" in coord.ds["app_stats"]
    assert "old-app" not in coord.ds["app_stats"]


async def test_get_app_stats_skips_malformed_fields() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(
        return_value=[
            {"fields": "not-a-list"},
            {"fields": [{"not_an_app": 1}]},
            {"fields": [{"app_name": "test-app", "cpu_usage": 1.0}]},
        ]
    )
    coord._app_stats_sub_id = "sub-1"
    coord.api.is_subscribed = AsyncMock(return_value=True)

    await coord.get_app_stats()

    assert "test-app" in coord.ds["app_stats"]
    assert coord.ds["app_stats"]["test-app"]["cpu_usage"] == pytest.approx(1.0)


async def test_stop_app_stats_unsubscribes_events() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock()
    coord._app_stats_sub_id = "sub-1"

    await coord.stop_app_stats()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._app_stats_sub_id is None
    assert coord._app_stats_event_name is None


async def test_stop_app_stats_default_clears_even_when_disconnected() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.unsubscribe_events = AsyncMock()
    coord._app_stats_sub_id = "sub-1"
    coord._app_stats_event_name = 'app.stats:{"interval": 5}'

    await coord.stop_app_stats()

    coord.api.unsubscribe_events.assert_not_awaited()
    assert coord._app_stats_sub_id is None
    assert coord._app_stats_event_name is None


async def test_get_app_stats_unwraps_collection_update_envelope() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(
        return_value=[
            {
                "method": "collection_update",
                "params": {
                    "fields": [
                        {
                            "app_name": "test-app",
                            "cpu_usage": 12.5,
                            "memory": 1024000,
                            "blkio": {"read": 5000, "write": 2000},
                            "networks": [
                                {
                                    "interface_name": "eth0",
                                    "rx_bytes": 1000,
                                    "tx_bytes": 500,
                                }
                            ],
                        }
                    ]
                },
            }
        ]
    )
    coord._app_stats_sub_id = "sub-1"
    coord.api.is_subscribed = AsyncMock(return_value=True)

    await coord.get_app_stats()

    assert coord.ds["app_stats"]["test-app"]["app_name"] == "test-app"
    assert coord.ds["app_stats"]["test-app"]["cpu_usage"] == pytest.approx(12.5)
    assert coord.ds["app_stats"]["test-app"]["memory"] == 1024000
    assert coord.ds["app_stats"]["test-app"]["blkio_read"] == 5000
    assert coord.ds["app_stats"]["test-app"]["blkio_write"] == 2000
    assert coord.ds["app_stats"]["test-app"]["networks"] == [
        {"interface_name": "eth0", "rx_bytes": 1000, "tx_bytes": 500}
    ]


async def test_get_app_stats_handles_missing_blkio_and_networks() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(
        return_value=[
            {
                "fields": [
                    {
                        "app_name": "test-app",
                        "cpu_usage": 1.0,
                        "memory": 1024,
                        "blkio": "not-a-dict",
                        "networks": "not-a-list",
                    }
                ]
            }
        ]
    )
    coord._app_stats_sub_id = "sub-1"
    coord.api.is_subscribed = AsyncMock(return_value=True)

    await coord.get_app_stats()

    assert coord.ds["app_stats"]["test-app"]["blkio_read"] is None
    assert coord.ds["app_stats"]["test-app"]["blkio_write"] is None
    assert coord.ds["app_stats"]["test-app"]["networks"] == []


async def test_get_app_stats_handles_malformed_networks_list() -> None:
    """Ensure _upsert_app_stats_entry keeps only valid network dicts."""
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(
        return_value=[
            {
                "fields": [
                    {
                        "app_name": "test-app",
                        "cpu_usage": 5.0,
                        "memory": 2048,
                        "networks": [
                            "bad",
                            {"interface_name": None, "rx_bytes": 10, "tx_bytes": 20},
                            {},
                            {
                                "interface_name": "eth0",
                                "rx_bytes": 1000,
                                "tx_bytes": 500,
                            },
                            {
                                "interface_name": "eth1",
                                "rx_bytes": 2000,
                                "tx_bytes": 1500,
                            },
                        ],
                    }
                ]
            }
        ]
    )
    coord._app_stats_sub_id = "sub-1"
    coord.api.is_subscribed = AsyncMock(return_value=True)

    await coord.get_app_stats()

    networks = coord.ds["app_stats"]["test-app"]["networks"]
    assert networks == [
        {"interface_name": "eth0", "rx_bytes": 1000, "tx_bytes": 500},
        {"interface_name": "eth1", "rx_bytes": 2000, "tx_bytes": 1500},
    ]


async def test_get_app_stats_ignores_non_dict_app_entries() -> None:
    """Ensure _upsert_app_stats_entry ignores non-dict app objects in messages."""
    coord = _bare_coordinator()
    coord.ds = {
        "app": {"test-app": {"name": "test-app"}},
        "app_stats": {},
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(
        return_value=[
            {"fields": ["not-a-dict", 42, None]},
        ]
    )
    coord._app_stats_sub_id = "sub-1"
    coord.api.is_subscribed = AsyncMock(return_value=True)

    await coord.get_app_stats()

    assert coord.ds["app_stats"] == {}


async def test_get_app_stats_normalizes_invalid_app_stats_to_none() -> None:
    """Invalid cpu_usage/memory/blkio_read values should be normalized to None."""
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.get_subscription_events = AsyncMock(
        return_value=[
            {
                "fields": [
                    {
                        "app_name": "test-app",
                        "cpu_usage": "bad",
                        "memory": {},
                        "blkio": {"read": "x"},
                        "networks": [],
                    }
                ]
            }
        ]
    )
    coord._app_stats_sub_id = "sub-1"
    coord.api.is_subscribed = AsyncMock(return_value=True)
    coord.ds = {"app": {"test-app": {"name": "test-app"}}, "app_stats": {}}

    await coord.get_app_stats()

    assert coord.ds["app_stats"]["test-app"]["cpu_usage"] is None
    assert coord.ds["app_stats"]["test-app"]["memory"] is None
    assert coord.ds["app_stats"]["test-app"]["blkio_read"] is None


def test_unwrap_app_stats_message_accepts_collection_update() -> None:
    from custom_components.truenas_ce.coordinator import _unwrap_app_stats_message

    msg = {"method": "collection_update", "params": {"fields": [{"app_name": "x"}]}}
    assert _unwrap_app_stats_message(msg) == {"fields": [{"app_name": "x"}]}


def test_unwrap_app_stats_message_accepts_top_level_fields() -> None:
    from custom_components.truenas_ce.coordinator import _unwrap_app_stats_message

    msg = {"fields": [{"app_name": "x"}]}
    assert _unwrap_app_stats_message(msg) == msg


def test_unwrap_app_stats_message_rejects_missing_fields() -> None:
    from custom_components.truenas_ce.coordinator import _unwrap_app_stats_message

    assert (
        _unwrap_app_stats_message({"method": "collection_update", "params": {}}) is None
    )
    assert (
        _unwrap_app_stats_message(
            {"method": "collection_update", "params": {"other": 1}}
        )
        is None
    )
    assert _unwrap_app_stats_message({"method": "collection_update"}) is None
    assert _unwrap_app_stats_message({"other": "data"}) is None


def test_unwrap_app_stats_message_rejects_non_dict_params() -> None:
    from custom_components.truenas_ce.coordinator import _unwrap_app_stats_message

    assert (
        _unwrap_app_stats_message({"method": "collection_update", "params": "bad"})
        is None
    )


async def test_start_app_stats_falls_back_on_invalid_poll_interval() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"test-app": {}}}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", MagicMock()))
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_POLL_INTERVAL: "not-a-number"}
    coord._app_stats_sub_id = "sub-old"
    coord._app_stats_event_name = 'app.stats:{"interval": 5}'

    await coord.start_app_stats()

    assert (
        coord._app_stats_event_name
        == f'app.stats:{{"interval": {DEFAULT_POLL_INTERVAL}}}'
    )
    coord.api.subscribe_events.assert_awaited_once()


# ---------------------------
#   connected
# ---------------------------
# Note: TrueNASCoordinator.__init__ itself is not unit-tested here -- HA's
# DataUpdateCoordinator.__init__ calls frame.report_usage(), which requires
# hass's frame helper to have been set up by a running Home Assistant core
# (unavailable via pytest-homeassistant-custom-component on this Windows dev
# machine). It is exercised by CI's hass-fixture-based integration tests.
def test_connected_delegates_to_api() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    assert coord.connected() is True
    coord.api.connected.assert_called_once()


# ---------------------------
#   _async_ensure_connected
# ---------------------------
async def test_async_ensure_connected_noop_when_already_connected() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.connect = AsyncMock()
    await coord._async_ensure_connected()
    coord.api.connect.assert_not_awaited()


async def test_async_ensure_connected_raises_update_failed_on_exception() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.connect = AsyncMock(side_effect=Exception("boom"))
    with pytest.raises(coordinator_module.UpdateFailed):
        await coord._async_ensure_connected()


async def test_async_ensure_connected_raises_auth_failed_on_invalid_key() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.connect = AsyncMock(return_value=False)
    coord.api.error = "ERR_INVALID_KEY"
    coord.host = "truenas.local"
    with (
        patch.object(coordinator_module, "ERR_INVALID_KEY", "ERR_INVALID_KEY"),
        pytest.raises(coordinator_module.ConfigEntryAuthFailed),
    ):
        await coord._async_ensure_connected()


async def test_async_ensure_connected_raises_update_failed_on_other_error() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.connect = AsyncMock(return_value=False)
    coord.api.error = "ERR_LOST_QUERY"
    coord.host = "truenas.local"
    with pytest.raises(coordinator_module.UpdateFailed):
        await coord._async_ensure_connected()


async def test_async_ensure_connected_succeeds() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.connect = AsyncMock(return_value=True)
    await coord._async_ensure_connected()  # must not raise


# ---------------------------
#   _async_update_data
# ---------------------------
def _stub_all_jobs(coord: TrueNASCoordinator) -> None:
    """Patch every job invoked by ``_async_update_data`` with a no-op AsyncMock."""
    for name in (
        "get_systeminfo",
        "get_systemstats",
        "get_service",
        "get_disk",
        "get_dataset",
        "get_vm",
        "get_container",
        "get_directoryservices",
        "get_cloudsync",
        "get_replication",
        "get_rsync",
        "get_snapshottask",
        "get_scrub",
        "get_app",
        "get_app_stats",
        "get_cronjob",
        "get_alerts",
        "get_certificates",
        "get_arc",
        "get_smb",
        "get_ups",
        "get_pool",
        "get_updatecheck",
    ):
        setattr(coord, name, AsyncMock())


async def test_async_update_data_runs_jobs_when_connected() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord._async_ensure_connected = AsyncMock()
    coord.async_detect_orphaned_statistics = AsyncMock()
    coord._clear_stale_migration_rollback_issue = MagicMock()
    coord.last_updatecheck_update = datetime(1970, 1, 1, tzinfo=UTC)
    _stub_all_jobs(coord)
    coord.ds = {"foo": "bar", "system_info": {"hostname": "truenas"}}

    result = await coord._async_update_data()

    coord.get_systeminfo.assert_awaited_once()
    coord.get_pool.assert_awaited_once()
    coord.get_updatecheck.assert_awaited_once()
    assert result is coord.ds


async def test_async_update_data_skips_jobs_when_disconnected() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord._async_ensure_connected = AsyncMock()
    coord.async_detect_orphaned_statistics = AsyncMock()
    coord._clear_stale_migration_rollback_issue = MagicMock()
    _stub_all_jobs(coord)

    with pytest.raises(coordinator_module.UpdateFailed):
        await coord._async_update_data()

    coord.get_systeminfo.assert_not_awaited()


async def test_async_update_data_swallows_job_exceptions() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord._async_ensure_connected = AsyncMock()
    coord.async_detect_orphaned_statistics = AsyncMock()
    coord._clear_stale_migration_rollback_issue = MagicMock()
    coord.last_updatecheck_update = datetime.now(UTC)
    _stub_all_jobs(coord)
    coord.get_service = AsyncMock(side_effect=Exception("boom"))
    coord.ds = {"system_info": {"hostname": "truenas"}}

    result = await coord._async_update_data()  # must not raise

    assert result is coord.ds


async def test_async_update_data_raises_when_system_info_missing() -> None:
    """A first refresh missing system.info must not be reported as successful."""
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord._async_ensure_connected = AsyncMock()
    _stub_all_jobs(coord)
    coord.ds = {"system_info": {}}

    with pytest.raises(coordinator_module.UpdateFailed):
        await coord._async_update_data()

    coord.get_systeminfo.assert_awaited_once()
    coord.get_pool.assert_not_awaited()


# ---------------------------
#   Orphaned statistics / migration rollback lifecycle
# ---------------------------
async def test_async_detect_orphaned_statistics_skips_without_recorder() -> None:
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.config.components = set()
    await coord.async_detect_orphaned_statistics()
    assert coord.orphaned_statistics == []


async def test_async_detect_orphaned_statistics_handles_listing_exception() -> None:
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.config.components = {"recorder"}
    with patch.object(
        coordinator_module,
        "get_instance",
        return_value=MagicMock(
            async_add_executor_job=AsyncMock(side_effect=Exception("boom"))
        ),
    ):
        await coord.async_detect_orphaned_statistics()
    assert coord.orphaned_statistics == []


async def test_async_detect_orphaned_statistics_filters_matching_ids() -> None:
    """Matching ids are kept, everything else dropped — and the result is sorted."""
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.config.components = {"recorder"}
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry1"
    coord.config_entry.options = {}
    slug = slugify(DEFAULT_DEVICE_NAME)
    coord._device_slug = slug
    stat_ids = [
        {"statistic_id": f"sensor.{slug}_cpu_usage", "source": "recorder"},
        {"statistic_id": f"sensor.{slug}_arc_size", "source": "recorder"},
        {"statistic_id": "sensor.unrelated_thing", "source": "recorder"},
        "not-a-dict",
    ]
    ent_reg = MagicMock()
    ent_reg.async_get.return_value = None
    with (
        patch.object(
            coordinator_module,
            "get_instance",
            return_value=MagicMock(
                async_add_executor_job=AsyncMock(return_value=stat_ids)
            ),
        ),
        patch.object(coordinator_module.er, "async_get", return_value=ent_reg),
        patch.object(coordinator_module.ir, "async_create_issue") as create_mock,
    ):
        await coord.async_detect_orphaned_statistics()

    assert coord.orphaned_statistics == [
        f"sensor.{slug}_arc_size",
        f"sensor.{slug}_cpu_usage",
    ]
    create_mock.assert_called_once()


async def test_async_detect_orphaned_statistics_ignores_other_entrys_device() -> None:
    """Regression (#61): with two TrueNAS config entries, detection used to
    match a fixed global slug, so each entry's coordinator flagged the *other*
    entry's orphaned statistics too and both raised their own duplicate
    Repairs issue for the same global list. Each entry must only see
    statistics whose id matches its own device-name slug.
    """
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.config.components = {"recorder"}
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry1"
    coord.config_entry.options = {}
    coord._device_slug = slugify("TrueNAS nuc13")
    stat_ids = [
        {
            "statistic_id": "sensor.truenas_nuc13_certificates_cert_time_until_expiry",
            "source": "recorder",
        },
        {
            "statistic_id": "sensor.truenas_x11dpu_certificates_cert_time_until_expiry",
            "source": "recorder",
        },
    ]
    ent_reg = MagicMock()
    ent_reg.async_get.return_value = None
    with (
        patch.object(
            coordinator_module,
            "get_instance",
            return_value=MagicMock(
                async_add_executor_job=AsyncMock(return_value=stat_ids)
            ),
        ),
        patch.object(coordinator_module.er, "async_get", return_value=ent_reg),
        patch.object(coordinator_module.ir, "async_create_issue"),
    ):
        await coord.async_detect_orphaned_statistics()

    assert coord.orphaned_statistics == [
        "sensor.truenas_nuc13_certificates_cert_time_until_expiry"
    ]


async def test_async_detect_orphaned_statistics_logs_ids_on_change(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The full id list is logged once per change, not on every poll."""
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.config.components = {"recorder"}
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry1"
    coord.config_entry.options = {}
    slug = slugify(DEFAULT_DEVICE_NAME)
    coord._device_slug = slug
    stat_ids = [{"statistic_id": f"sensor.{slug}_cpu_usage", "source": "recorder"}]
    ent_reg = MagicMock()
    ent_reg.async_get.return_value = None
    with (
        patch.object(
            coordinator_module,
            "get_instance",
            return_value=MagicMock(
                async_add_executor_job=AsyncMock(return_value=stat_ids)
            ),
        ),
        patch.object(coordinator_module.er, "async_get", return_value=ent_reg),
        patch.object(coordinator_module.ir, "async_create_issue"),
        caplog.at_level("DEBUG", logger=coordinator_module.__name__),
    ):
        await coord.async_detect_orphaned_statistics()
        assert f"sensor.{slug}_cpu_usage" in caplog.text

        caplog.clear()
        await coord.async_detect_orphaned_statistics()

    assert "Orphaned TrueNAS statistics" not in caplog.text


def test_count_statistics_with_data_counts_only_ids_with_rows() -> None:
    hass = MagicMock()
    with patch.object(
        coordinator_module,
        "get_last_statistics",
        side_effect=[{"sensor.a": [{"state": 1}]}, {}],
    ) as stats_mock:
        assert _count_statistics_with_data(hass, ["sensor.a", "sensor.b"]) == 1

    # Pinned on purpose: ``get_last_statistics`` takes a single id as a bare
    # ``str`` and builds ``{statistic_id}`` from it internally. Wrapping it in a
    # list would raise "unhashable type: 'list'", so this asserts the id is
    # never "helpfully" turned into a sequence.
    assert [call.args[2] for call in stats_mock.call_args_list] == [
        "sensor.a",
        "sensor.b",
    ]


async def test_async_count_orphans_with_data_returns_zero_without_orphans() -> None:
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.orphaned_statistics = []
    assert await coord.async_count_orphans_with_data() == 0


async def test_async_count_orphans_with_data_probes_recorder() -> None:
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.config.components = {"recorder"}
    coord.orphaned_statistics = ["sensor.truenas_a", "sensor.truenas_b"]
    with patch.object(
        coordinator_module,
        "get_instance",
        return_value=MagicMock(async_add_executor_job=AsyncMock(return_value=1)),
    ):
        assert await coord.async_count_orphans_with_data() == 1


async def test_async_count_orphans_with_data_assumes_all_without_recorder() -> None:
    """Without the recorder loaded the probe cannot run: assume the worst case."""
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.config.components = set()
    coord.orphaned_statistics = ["sensor.truenas_a"]
    assert await coord.async_count_orphans_with_data() == 1


async def test_async_count_orphans_with_data_falls_back_on_error() -> None:
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.config.components = {"recorder"}
    coord.orphaned_statistics = ["sensor.truenas_a", "sensor.truenas_b"]
    with patch.object(
        coordinator_module,
        "get_instance",
        return_value=MagicMock(
            async_add_executor_job=AsyncMock(side_effect=Exception("boom"))
        ),
    ):
        assert await coord.async_count_orphans_with_data() == 2


def test_update_statistics_issue_deletes_when_no_orphans() -> None:
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry1"
    coord.config_entry.options = {}
    coord.orphaned_statistics = []
    with patch.object(coordinator_module.ir, "async_delete_issue") as delete_mock:
        coord._update_statistics_issue()
    delete_mock.assert_called_once()


def test_update_statistics_issue_skips_creation_when_ignored() -> None:
    coord = _bare_coordinator()
    coord.hass = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry1"
    coord.config_entry.options = {CONF_STATISTICS_CLEANUP_IGNORED: True}
    coord.orphaned_statistics = ["sensor.truenas_x"]
    with patch.object(coordinator_module.ir, "async_delete_issue") as delete_mock:
        coord._update_statistics_issue()
    delete_mock.assert_called_once()


def test_raise_migration_rollback_issue_noop_when_not_possible() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.data = {}
    coord.hass = MagicMock()
    with patch.object(coordinator_module.ir, "async_create_issue") as create_mock:
        coord.raise_migration_rollback_issue()
    create_mock.assert_not_called()


def test_raise_migration_rollback_issue_creates_when_possible() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry1"
    coord.config_entry.data = {
        MIGRATION_LEGACY_ENTRY_ID: "legacy-1",
        MIGRATION_RECORDS: [1, 2],
    }
    coord.hass = MagicMock()
    coord.hass.config_entries.async_get_entry.return_value = MagicMock()
    with patch.object(coordinator_module.ir, "async_create_issue") as create_mock:
        coord.raise_migration_rollback_issue()
    create_mock.assert_called_once()


def test_clear_stale_migration_rollback_issue_inert_for_legacy_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "DOMAIN", LEGACY_DOMAIN)
    coord = _bare_coordinator()
    with patch.object(coordinator_module.ir, "async_delete_issue") as delete_mock:
        coord._clear_stale_migration_rollback_issue()
    delete_mock.assert_not_called()


def test_clear_stale_migration_rollback_issue_deletes_when_rollback_impossible() -> (
    None
):
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry1"
    coord.config_entry.data = {}
    coord.hass = MagicMock()
    with patch.object(coordinator_module.ir, "async_delete_issue") as delete_mock:
        coord._clear_stale_migration_rollback_issue()
    delete_mock.assert_called_once()


async def test_async_clear_orphaned_statistics_noop_when_empty() -> None:
    coord = _bare_coordinator()
    coord.orphaned_statistics = []
    coord.hass = MagicMock()
    await coord.async_clear_orphaned_statistics()
    coord.hass.assert_not_called() if callable(coord.hass) else None


async def test_async_clear_orphaned_statistics_clears_and_refreshes() -> None:
    coord = _bare_coordinator()
    coord.orphaned_statistics = ["sensor.truenas_x"]
    coord.hass = MagicMock()
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry1"
    coord.ds = {"foo": "bar"}
    coord.async_set_updated_data = MagicMock()
    instance_mock = MagicMock()
    instance_mock.async_clear_statistics = MagicMock()
    with (
        patch.object(coordinator_module, "get_instance", return_value=instance_mock),
        patch.object(coordinator_module.ir, "async_delete_issue") as delete_mock,
    ):
        await coord.async_clear_orphaned_statistics()

    instance_mock.async_clear_statistics.assert_called_once_with(["sensor.truenas_x"])
    assert coord.orphaned_statistics == []
    delete_mock.assert_called_once()
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


# ---------------------------
#   get_systeminfo / _handle_update_job / _query_interfaces
# ---------------------------
async def test_get_systeminfo_parses_valid_response_and_runs_pipeline() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}, "interface": {}}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.query = AsyncMock(
        return_value={
            "version": "TrueNAS-SCALE-25.04.1",
            "hostname": "nas1",
            "uptime_seconds": 100,
            "physmem": 1000,
        }
    )
    coord._handle_update_job = AsyncMock()

    await coord.get_systeminfo()

    assert coord.ds["system_info"]["hostname"] == "nas1"
    assert coord.ds["system_info"]["update_version"] == "TrueNAS-SCALE-25.04.1"
    assert coord._version_major == 25
    coord._handle_update_job.assert_awaited_once()


async def test_get_systeminfo_skips_parse_on_invalid_response() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}, "interface": {}}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.query = AsyncMock(return_value=None)
    coord._handle_update_job = AsyncMock()

    await coord.get_systeminfo()

    coord._handle_update_job.assert_awaited_once()


async def test_get_systeminfo_returns_early_when_disconnected_after_parse() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}, "interface": {}}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.query = AsyncMock(return_value={"version": "25.04.1"})
    coord._handle_update_job = AsyncMock()

    await coord.get_systeminfo()

    coord._handle_update_job.assert_not_awaited()


async def test_get_systeminfo_returns_early_disconnected_after_update_job() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}, "interface": {}}
    coord.api = MagicMock()
    # Connected for the pre-update-job check, disconnected right after.
    coord.api.connected = MagicMock(side_effect=[True, False])
    coord.api.query = AsyncMock(return_value={"version": "25.04.1"})
    coord._handle_update_job = AsyncMock()
    coord._parse_version = MagicMock()

    await coord.get_systeminfo()

    coord._handle_update_job.assert_awaited_once()
    coord._parse_version.assert_not_called()


async def test_handle_update_job_noop_without_jobid() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"update_jobid": 0}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock()
    await coord._handle_update_job()
    coord.api.query.assert_not_awaited()


async def test_handle_update_job_keeps_progress_while_running() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"update_jobid": 5, "update_available": True}}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.query = AsyncMock(
        return_value={"progress": {"percent": 42}, "state": "RUNNING"}
    )
    await coord._handle_update_job()
    assert coord.ds["system_info"]["update_progress"] == 42
    assert coord.ds["system_info"]["update_state"] == "RUNNING"


async def test_handle_update_job_resets_when_finished() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "system_info": {
            "update_jobid": 5,
            "update_available": False,
            "version": "25.04.1",
        }
    }
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.query = AsyncMock(
        return_value={"progress": {"percent": 100}, "state": "SUCCESS"}
    )
    await coord._handle_update_job()
    assert coord.ds["system_info"]["update_progress"] == 0
    assert coord.ds["system_info"]["update_jobid"] == 0
    assert coord.ds["system_info"]["update_state"] == "unknown"


async def test_handle_update_job_returns_early_when_disconnected() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"update_jobid": 5}}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.query = AsyncMock(return_value=None)
    await coord._handle_update_job()
    assert coord.ds["system_info"]["update_jobid"] == 5


async def test_query_interfaces_derives_link_up() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {"id": "eth0", "name": "eth0", "state": {"link_state": "LINK_STATE_UP"}},
            {"id": "eth1", "name": "eth1", "state": {"link_state": "LINK_STATE_DOWN"}},
        ]
    )
    await coord._query_interfaces()
    assert coord.ds["interface"]["eth0"]["link_up"] is True
    assert coord.ds["interface"]["eth1"]["link_up"] is False


# ---------------------------
#   get_systemstats family
# ---------------------------
def test_select_stat_graph_names_includes_interface_when_present() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {"eth0": {}}}
    coord._is_virtual = False
    coord._systemstats_errored = {}
    names = coord._select_stat_graph_names()
    assert "interface" in names
    assert "cputemp" in names


def test_select_stat_graph_names_removes_cputemp_for_virtual() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {}}
    coord._is_virtual = True
    coord._systemstats_errored = {}
    names = coord._select_stat_graph_names()
    assert "cputemp" not in names
    assert "interface" not in names


def test_select_stat_graph_names_filters_cooldown_graphs() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {}}
    coord._is_virtual = False
    coord._systemstats_errored = {"cpu": datetime.now(UTC)}
    coord._systemstats_error_cooldown = timedelta(minutes=10)
    names = coord._select_stat_graph_names()
    assert "cpu" not in names


async def test_fetch_stat_graphs_collects_and_records_failures() -> None:
    coord = _bare_coordinator()
    coord.host = "truenas.local"
    coord._systemstats_errored = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(side_effect=[[{"name": "load"}], None])
    result = await coord._fetch_stat_graphs(["load", "cpu"], {"start": 0, "end": 1})
    assert result == [{"name": "load"}]
    assert "cpu" in coord._systemstats_errored


def test_record_failed_graphs_logs_only_new_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coord = _bare_coordinator()
    coord.host = "truenas.local"
    coord._systemstats_errored = {"cpu": datetime.now(UTC)}
    with caplog.at_level("WARNING"):
        coord._record_failed_graphs(["cpu", "memory"])
    assert "memory" in caplog.text
    assert coord._systemstats_errored.keys() == {"cpu", "memory"}


def test_record_failed_graphs_noop_for_empty_list() -> None:
    coord = _bare_coordinator()
    coord._systemstats_errored = {}
    coord._record_failed_graphs([])
    assert coord._systemstats_errored == {}


def test_process_system_stat_dispatches_by_name() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}, "interface": {}}
    # Missing "aggregations"/"legend" fails the isinstance guard in
    # _systemstats_process, so it falls back to _store_stat_defaults, which
    # routes through _store_stat_value the same as a successful value would.
    coord._process_system_stat({"name": "load"})
    assert coord.ds["system_info"]["load_shortterm"] == 0.0


def test_process_system_stat_ignores_missing_name() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._process_system_stat({})  # must not raise


def test_process_system_stat_dispatches_cputemp() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    item = {"name": "cputemp", "aggregations": {"mean": {"core0": 40.0}}}
    with patch.object(coord, "_process_cputemp") as mock:
        coord._process_system_stat(item)
    mock.assert_called_once_with(item)


def test_process_system_stat_dispatches_cpu_and_rounds_usage() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._process_system_stat({"name": "cpu"})
    # No aggregations/legend -> _store_stat_defaults zeroes cpu_cpu, which then
    # feeds cpu_usage.
    assert coord.ds["system_info"]["cpu_usage"] == pytest.approx(0.0)


def test_process_system_stat_dispatches_interface_for_known_identifier() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}, "interface": {"eth0": {}}}
    coord._process_system_stat(
        {"name": "interface", "identifier": "eth0", "legend": "not-a-list"}
    )
    assert coord.ds["interface"]["eth0"]["rx"] == 0.0
    assert coord.ds["interface"]["eth0"]["tx"] == 0.0


def test_process_system_stat_ignores_interface_for_unknown_identifier() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}, "interface": {}}
    coord._process_system_stat({"name": "interface", "identifier": "eth99"})
    assert coord.ds["interface"] == {}


def test_process_system_stat_dispatches_memory() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"physmem": 1000}}
    coord._process_system_stat(
        {
            "name": "memory",
            "legend": ["available"],
            "aggregations": {"mean": {"available": 250.0}},
        }
    )
    assert coord.ds["system_info"]["memory-free_value"] == 250


def test_process_system_stat_dispatches_arcsize() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._process_system_stat(
        {
            "name": "arcsize",
            "legend": ["size"],
            "aggregations": {"mean": {"size": 12.345}},
        }
    )
    assert coord.ds["system_info"]["cache_size-arc_value"] == pytest.approx(12.35)


def test_process_system_stat_dispatches_unknown_name() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord.host = "truenas.local"
    coord._unknown_system_stat_names = set()
    coord._process_system_stat({"name": "weird_stat"})
    assert "weird_stat" in coord._unknown_system_stat_names


def test_process_cputemp_stores_max_mean() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._process_cputemp({"aggregations": {"mean": {"core0": 40.0, "core1": 45.0}}})
    assert coord.ds["system_info"]["cpu_temperature"] == 45.0


def test_process_cputemp_none_when_no_valid_means() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._process_cputemp({"aggregations": {"mean": {}}})
    assert coord.ds["system_info"]["cpu_temperature"] is None


def test_process_memory_stat_computes_usage_percent() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"physmem": 1000}}
    coord._process_memory_stat(
        {"legend": ["available"], "aggregations": {"mean": {"available": 250.0}}}
    )
    assert coord.ds["system_info"]["memory-total_value"] == 1000
    assert coord.ds["system_info"]["memory-free_value"] == 250
    assert coord.ds["system_info"]["memory-usage_percent"] == 75


def test_handle_unknown_stat_logs_once_and_detects_near_miss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coord = _bare_coordinator()
    coord.host = "truenas.local"
    coord._unknown_system_stat_names = set()
    with caplog.at_level("DEBUG"):
        coord._handle_unknown_stat("cpu_usage")
        coord._handle_unknown_stat("cpu_usage")
    assert caplog.text.count("unknown system stat graph name") == 1


def test_process_system_stat_interface_updates_rx_tx() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {"eth0": {}}}
    item = {
        "legend": ["received", "sent"],
        "aggregations": {"mean": {"received": 100.0, "sent": 50.0}},
    }
    coord._process_system_stat_interface(item, "eth0")
    assert coord.ds["interface"]["eth0"]["rx"] > 0
    assert coord.ds["interface"]["eth0"]["tx"] > 0


def test_process_system_stat_interface_zeroes_on_invalid_legend() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {"eth0": {}}}
    coord._process_system_stat_interface({"legend": "not-a-list"}, "eth0")
    assert coord.ds["interface"]["eth0"]["rx"] == 0.0
    assert coord.ds["interface"]["eth0"]["tx"] == 0.0


def test_process_system_stat_interface_zeroes_when_mean_not_dict() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {"eth0": {}}}
    item = {
        "legend": ["received", "sent"],
        "aggregations": {"mean": "not-a-dict"},
    }
    coord._process_system_stat_interface(item, "eth0")
    assert coord.ds["interface"]["eth0"]["rx"] == 0.0
    assert coord.ds["interface"]["eth0"]["tx"] == 0.0


async def test_get_systemstats_returns_early_without_graph_names() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {}}
    coord._is_virtual = True
    coord._systemstats_errored = {
        name: datetime.now(UTC) for name in ("load", "cpu", "arcsize", "memory")
    }
    coord._systemstats_error_cooldown = timedelta(minutes=10)
    coord.config_entry = MagicMock()
    coord.config_entry.options = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock()
    await coord.get_systemstats()
    coord.api.query.assert_not_awaited()


async def test_get_systemstats_returns_when_fetch_yields_no_graphs() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {}, "system_info": {}}
    coord._is_virtual = True
    coord._systemstats_errored = {}
    coord.host = "truenas.local"
    coord.config_entry = MagicMock()
    coord.config_entry.options = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=None)
    await coord.get_systemstats()
    assert coord.ds["system_info"] == {}


async def test_get_systemstats_processes_returned_graphs() -> None:
    coord = _bare_coordinator()
    coord.ds = {"interface": {}, "system_info": {}}
    coord._is_virtual = True
    coord._systemstats_errored = {}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=[{"name": "load"}])
    await coord.get_systemstats()
    assert coord.ds["system_info"]["load_shortterm"] == 0.0


# ---------------------------
#   get_service
# ---------------------------
# The running/display_name derivation these tests used to exercise directly
# now lives in and is tested by aiotruenas's own TrueNASState.get_service().
# _refresh_service just delegates and assigns the result, so this only needs
# to lock in that plumbing.
async def test_refresh_service_delegates_to_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {"service": {}}
    coord.state = MagicMock()
    coord.state.get_service = AsyncMock(
        return_value={1: {"running": True, "display_name": "SMB"}}
    )
    await coord._refresh_service()
    assert coord.ds["service"]["1"]["running"] is True
    assert coord.ds["service"]["1"]["display_name"] == "SMB"


# ---------------------------
#   generic push-subscription helpers (_ensure_push_subscription et al.)
# ---------------------------
async def test_ensure_push_subscription_noop_when_not_connected() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.api.subscribe_events = AsyncMock()
    state = _PushSourceState()

    await coord._ensure_push_subscription(state, "svc.query", AsyncMock(), label="svc")

    coord.api.subscribe_events.assert_not_awaited()
    assert state.sub_id is None


async def test_ensure_push_subscription_subscribes_once() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    state = _PushSourceState()

    await coord._ensure_push_subscription(state, "svc.query", AsyncMock(), label="svc")

    coord.api.subscribe_events.assert_awaited_once_with("svc.query")
    assert state.sub_id == "sub-1"
    assert state.consumer is not None
    await state.consumer.stop()


async def test_ensure_push_subscription_noop_when_already_active() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.is_subscribed = AsyncMock(return_value=True)
    coord.api.subscribe_events = AsyncMock()
    state = _PushSourceState()
    state.sub_id = "sub-existing"
    state.event = "svc.query"

    await coord._ensure_push_subscription(state, "svc.query", AsyncMock(), label="svc")

    coord.api.subscribe_events.assert_not_awaited()
    assert state.sub_id == "sub-existing"


async def test_ensure_push_subscription_resubscribes_when_stale() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.is_subscribed = AsyncMock(return_value=False)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", asyncio.Queue()))
    state = _PushSourceState()
    state.sub_id = "sub-stale"

    await coord._ensure_push_subscription(state, "svc.query", AsyncMock(), label="svc")

    coord.api.subscribe_events.assert_awaited_once_with("svc.query")
    assert state.sub_id == "sub-new"
    await state.consumer.stop()


async def test_ensure_push_subscription_resubscribes_when_topic_changes() -> None:
    """A source's event topic changing (e.g. mid-session API upgrade) must
    tear down the old subscription and establish a new one for the new
    topic, not silently keep listening on the old one (#101 review)."""
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.is_subscribed = AsyncMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-new", asyncio.Queue()))
    state = _PushSourceState()
    state.sub_id = "sub-old"
    state.event = "virt.instance.query"

    await coord._ensure_push_subscription(
        state, "container.query", AsyncMock(), label="container"
    )

    coord.api.subscribe_events.assert_awaited_once_with("container.query")
    assert state.sub_id == "sub-new"
    assert state.event == "container.query"
    await state.consumer.stop()


async def test_ensure_push_subscription_handles_subscribe_failure() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(side_effect=Exception("subscribe failed"))
    state = _PushSourceState()

    await coord._ensure_push_subscription(state, "svc.query", AsyncMock(), label="svc")

    assert state.sub_id is None
    assert state.consumer is None


async def test_ensure_push_subscription_handles_no_sub_id_returned() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=(None, None))
    state = _PushSourceState()

    await coord._ensure_push_subscription(state, "svc.query", AsyncMock(), label="svc")

    assert state.sub_id is None


async def test_ensure_push_subscription_respects_breaker_cooldown() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock()
    state = _PushSourceState()
    for _ in range(3):  # default config trips after 3 consecutive breaches
        state.breaker.record_batch(9999)

    await coord._ensure_push_subscription(state, "svc.query", AsyncMock(), label="svc")

    coord.api.subscribe_events.assert_not_awaited()
    assert state.sub_id is None


async def test_stop_push_subscription_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    state = _PushSourceState()
    await coord._ensure_push_subscription(state, "svc.query", AsyncMock(), label="svc")

    await coord._stop_push_subscription(state)

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert state.sub_id is None
    assert state.consumer is None


async def test_stop_push_subscription_noop_when_not_subscribed() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock()
    state = _PushSourceState()

    await coord._stop_push_subscription(state)

    coord.api.unsubscribe_events.assert_not_awaited()
    assert state.sub_id is None


# ---------------------------
#   get_service push subscription wiring
# ---------------------------
async def test_get_service_ensures_push_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"service": {}}
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.state = MagicMock()
    coord.state.get_service = AsyncMock(return_value={})
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))

    await coord.get_service()

    coord.api.subscribe_events.assert_awaited_once_with("service.query")
    assert coord._service_push.sub_id == "sub-1"
    await coord._service_push.consumer.stop()


async def test_on_service_push_refreshes_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"service": {}}
    coord.state = MagicMock()
    coord.state.get_service = AsyncMock(
        return_value={1: {"running": True, "display_name": "SSH"}}
    )
    coord.async_set_updated_data = MagicMock()

    await coord._on_service_push([{"msg": "changed"}])

    assert coord.ds["service"]["1"]["running"] is True
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


async def test_refresh_locked_serializes_concurrent_refreshes() -> None:
    """A slower poll refresh must not clobber a faster, later-started push
    refresh of the same source once it has already applied its result
    (#101 review: push vs. poll race on ``self.ds``)."""
    coord = _bare_coordinator()
    state = _PushSourceState()
    order: list[str] = []

    async def slow_refresh() -> None:
        order.append("slow-start")
        await asyncio.sleep(0.02)
        order.append("slow-end")

    async def fast_refresh() -> None:
        order.append("fast-start")
        order.append("fast-end")

    slow_task = asyncio.create_task(coord._refresh_locked(state, slow_refresh))
    await asyncio.sleep(0)  # let slow_refresh acquire the lock and start
    fast_task = asyncio.create_task(coord._refresh_locked(state, fast_refresh))

    await asyncio.gather(slow_task, fast_task)

    # fast_refresh must wait for slow_refresh to fully finish, not interleave.
    assert order == ["slow-start", "slow-end", "fast-start", "fast-end"]


async def test_stop_service_push_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    await coord._ensure_push_subscription(
        coord._service_push,
        coord._SERVICE_EVENT,
        coord._on_service_push,
        label="service",
    )

    await coord.stop_service_push()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._service_push.sub_id is None


# ---------------------------
#   get_cloudsync / get_replication / get_rsync push subscription wiring
# ---------------------------
async def test_get_cloudsync_ensures_push_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"cloudsync": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_CLOUDSYNC]}
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.state = MagicMock()
    coord.state.get_cloudsync = AsyncMock(return_value={})
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))

    await coord.get_cloudsync()

    coord.api.subscribe_events.assert_awaited_once_with("cloudsync.query")
    assert coord._cloudsync_push.sub_id == "sub-1"
    await coord._cloudsync_push.consumer.stop()


async def test_on_cloudsync_push_refreshes_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"cloudsync": {}}
    coord.state = MagicMock()
    coord.state.get_cloudsync = AsyncMock(
        return_value={"cs1": {"id": "cs1", "description": "backup"}}
    )
    coord.async_set_updated_data = MagicMock()

    await coord._on_cloudsync_push([{"msg": "changed"}])

    assert "cs1" in coord.ds["cloudsync"]
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


async def test_stop_cloudsync_push_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    await coord._ensure_push_subscription(
        coord._cloudsync_push,
        coord._CLOUDSYNC_EVENT,
        coord._on_cloudsync_push,
        label="cloudsync",
    )

    await coord.stop_cloudsync_push()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._cloudsync_push.sub_id is None


async def test_get_replication_ensures_push_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"replication": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_REPLICATION]}
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.state = MagicMock()
    coord.state.get_replication = AsyncMock(return_value={})
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))

    await coord.get_replication()

    coord.api.subscribe_events.assert_awaited_once_with("replication.query")
    assert coord._replication_push.sub_id == "sub-1"
    await coord._replication_push.consumer.stop()


async def test_on_replication_push_refreshes_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"replication": {}}
    coord.state = MagicMock()
    coord.state.get_replication = AsyncMock(
        return_value={1: {"id": 1, "name": "repl1", "state": "RUNNING"}}
    )
    coord.async_set_updated_data = MagicMock()

    await coord._on_replication_push([{"msg": "changed"}])

    assert coord.ds["replication"]["1"]["state"] == "RUNNING"
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


async def test_stop_replication_push_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    await coord._ensure_push_subscription(
        coord._replication_push,
        coord._REPLICATION_EVENT,
        coord._on_replication_push,
        label="replication",
    )

    await coord.stop_replication_push()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._replication_push.sub_id is None


async def test_get_rsync_ensures_push_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"rsynctask": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_RSYNC]}
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.state = MagicMock()
    coord.state.get_rsync = AsyncMock(return_value={})
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))

    await coord.get_rsync()

    coord.api.subscribe_events.assert_awaited_once_with("rsynctask.query")
    assert coord._rsync_push.sub_id == "sub-1"
    await coord._rsync_push.consumer.stop()


async def test_on_rsync_push_refreshes_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"rsynctask": {}}
    coord.state = MagicMock()
    coord.state.get_rsync = AsyncMock(return_value={1: {"id": 1, "path": "/mnt/tank"}})
    coord.async_set_updated_data = MagicMock()

    await coord._on_rsync_push([{"msg": "changed"}])

    assert "1" in coord.ds["rsynctask"]
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


async def test_stop_rsync_push_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    await coord._ensure_push_subscription(
        coord._rsync_push,
        coord._RSYNC_EVENT,
        coord._on_rsync_push,
        label="rsync",
    )

    await coord.stop_rsync_push()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._rsync_push.sub_id is None


# ---------------------------
#   get_pool
# ---------------------------
# The pool capacity/mountpoint-matching/boot-pool-merge/topology-error-
# aggregation logic these tests used to exercise directly now lives in and is
# tested by aiotruenas's own TrueNASState.get_pool() (see
# tests/test_domain_state.py in that repo). _refresh_pool just delegates and
# assigns the result, so these tests only need to lock in that plumbing.
async def test_refresh_pool_delegates_to_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {"pool": {}}
    coord.state = MagicMock()
    coord.state.get_pool = AsyncMock(
        return_value={"g1": {"name": "tank", "available": 40, "total": 100}}
    )
    await coord._refresh_pool()
    assert coord.ds["pool"]["g1"]["available"] == 40
    assert coord.ds["pool"]["g1"]["total"] == 100


# ---------------------------
#   get_pool push subscription wiring
# ---------------------------
async def test_get_pool_ensures_push_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"pool": {}}
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.state = MagicMock()
    coord.state.get_pool = AsyncMock(return_value={})
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))

    await coord.get_pool()

    coord.api.subscribe_events.assert_awaited_once_with("pool.query")
    assert coord._pool_push.sub_id == "sub-1"
    await coord._pool_push.consumer.stop()


async def test_on_pool_push_refreshes_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"pool": {}}
    coord.state = MagicMock()
    coord.state.get_pool = AsyncMock(
        return_value={"g1": {"name": "tank", "available": 40}}
    )
    coord.async_set_updated_data = MagicMock()

    await coord._on_pool_push([{"msg": "changed"}])

    assert coord.ds["pool"]["g1"]["available"] == 40
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


async def test_stop_pool_push_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    await coord._ensure_push_subscription(
        coord._pool_push,
        coord._POOL_EVENT,
        coord._on_pool_push,
        label="pool",
    )

    await coord.stop_pool_push()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._pool_push.sub_id is None


# ---------------------------
#   get_dataset
# ---------------------------
async def test_get_dataset_empty_when_group_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"dataset": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.state = MagicMock()
    coord.state.get_dataset = AsyncMock()
    await coord.get_dataset()
    assert coord.ds["dataset"] == {}
    coord.state.get_dataset.assert_not_awaited()


async def test_get_dataset_returns_empty_when_none_found() -> None:
    coord = _bare_coordinator()
    coord.ds = {"dataset": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_DATASETS]}
    coord.state = MagicMock()
    coord.state.get_dataset = AsyncMock(return_value={})
    await coord.get_dataset()
    assert coord.ds["dataset"] == {}


async def test_get_dataset_parses_when_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"dataset": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_DATASETS]}
    coord.state = MagicMock()
    coord.state.get_dataset = AsyncMock(
        return_value={"tank": {"id": "tank", "type": "FILESYSTEM", "name": "tank"}}
    )
    await coord.get_dataset()
    assert "tank" in coord.ds["dataset"]


# ---------------------------
#   get_disk
# ---------------------------
# The disk.query normalization and netdata/API-fallback temperature-
# enrichment logic these tests used to exercise directly now lives in and is
# tested by aiotruenas's own TrueNASState.get_disk() (see
# tests/test_domain_state.py in that repo). get_disk just delegates and
# assigns the result, so this only needs to lock in that plumbing.
async def test_get_disk_delegates_to_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {"disk": {}}
    coord.state = MagicMock()
    coord.state.get_disk = AsyncMock(
        return_value={"disk1": {"name": "sda", "temperature": 35.0}}
    )
    await coord.get_disk()
    assert coord.ds["disk"]["disk1"]["temperature"] == 35.0


# ---------------------------
#   get_vm
# ---------------------------
# The memory/running derivation these tests used to exercise directly now
# lives in and is tested by aiotruenas's own TrueNASState.get_vm().
# _refresh_vm just delegates and assigns the result, so this only needs to
# lock in that plumbing.
async def test_get_vm_empty_when_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"vm": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    await coord.get_vm()
    assert coord.ds["vm"] == {}


async def test_refresh_vm_delegates_to_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {"vm": {}}
    coord.state = MagicMock()
    coord.state.get_vm = AsyncMock(return_value={1: {"memory": 2, "running": True}})
    await coord._refresh_vm()
    assert coord.ds["vm"]["1"]["memory"] == 2
    assert coord.ds["vm"]["1"]["running"] is True


# ---------------------------
#   get_vm push subscription wiring
# ---------------------------
async def test_get_vm_ensures_push_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"vm": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_VMS]}
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.state = MagicMock()
    coord.state.get_vm = AsyncMock(return_value={})
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))

    await coord.get_vm()

    coord.api.subscribe_events.assert_awaited_once_with("vm.query")
    assert coord._vm_push.sub_id == "sub-1"
    await coord._vm_push.consumer.stop()


async def test_on_vm_push_refreshes_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"vm": {}}
    coord.state = MagicMock()
    coord.state.get_vm = AsyncMock(return_value={1: {"running": True}})
    coord.async_set_updated_data = MagicMock()

    await coord._on_vm_push([{"msg": "changed"}])

    assert coord.ds["vm"]["1"]["running"] is True
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


async def test_stop_vm_push_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    await coord._ensure_push_subscription(
        coord._vm_push,
        coord._VM_EVENT,
        coord._on_vm_push,
        label="vm",
    )

    await coord.stop_vm_push()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._vm_push.sub_id is None


async def test_get_vm_not_monitored_stops_push_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"vm": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock()
    coord._vm_push.sub_id = "sub-1"

    await coord.get_vm()

    assert coord.ds["vm"] == {}
    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._vm_push.sub_id is None


# ---------------------------
#   get_container
# ---------------------------
# The legacy-vs-v26 dispatch, field normalization and cpu/memory/ip_address
# derivation these tests used to exercise directly now live in and are
# tested by aiotruenas's own TrueNASState.get_container(). _refresh_container
# just delegates and assigns the result, so this only needs to lock in that
# plumbing.
async def test_refresh_container_delegates_to_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {"container": {}}
    coord.state = MagicMock()
    coord.state.get_container = AsyncMock(
        return_value={"c1": {"cpu": 1, "memory": 1, "running": True}}
    )
    await coord._refresh_container()
    assert coord.ds["container"]["c1"]["cpu"] == 1
    assert coord.ds["container"]["c1"]["running"] is True


def test_supports_container_api_from_26() -> None:
    coord = _bare_coordinator()
    coord._version_major, coord._version_minor = 25, 10
    assert coord.supports_container_api() is False
    coord._version_major, coord._version_minor = 26, 0
    assert coord.supports_container_api() is True


async def test_get_container_empty_when_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"container": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    await coord.get_container()
    assert coord.ds["container"] == {}


# ---------------------------
#   get_container push subscription wiring
# ---------------------------
async def test_get_container_ensures_push_subscription_legacy_topic() -> None:
    coord = _bare_coordinator()
    coord.ds = {"container": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_CONTAINERS]}
    coord._version_major, coord._version_minor = 25, 10
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.state = MagicMock()
    coord.state.get_container = AsyncMock(return_value={})
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))

    await coord.get_container()

    coord.api.subscribe_events.assert_awaited_once_with("virt.instance.query")
    assert coord._container_push.sub_id == "sub-1"
    await coord._container_push.consumer.stop()


async def test_get_container_ensures_push_subscription_v26_topic() -> None:
    coord = _bare_coordinator()
    coord.ds = {"container": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_CONTAINERS]}
    coord._version_major, coord._version_minor = 26, 0
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.state = MagicMock()
    coord.state.get_container = AsyncMock(return_value={})
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))

    await coord.get_container()

    coord.api.subscribe_events.assert_awaited_once_with("container.query")
    assert coord._container_push.sub_id == "sub-1"
    await coord._container_push.consumer.stop()


async def test_on_container_push_refreshes_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"container": {}}
    coord._version_major, coord._version_minor = 25, 10
    coord.state = MagicMock()
    coord.state.get_container = AsyncMock(return_value={"c1": {"running": True}})
    coord.async_set_updated_data = MagicMock()

    await coord._on_container_push([{"msg": "changed"}])

    assert coord.ds["container"]["c1"]["running"] is True
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


async def test_stop_container_push_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    await coord._ensure_push_subscription(
        coord._container_push,
        "virt.instance.query",
        coord._on_container_push,
        label="container",
    )

    await coord.stop_container_push()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._container_push.sub_id is None


async def test_get_container_not_monitored_stops_push_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"container": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock()
    coord._container_push.sub_id = "sub-1"

    await coord.get_container()

    assert coord.ds["container"] == {}
    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._container_push.sub_id is None


# ---------------------------
#   get_directoryservices
# ---------------------------
async def test_get_directoryservices_empty_when_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"directoryservices": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    coord.api.query = AsyncMock()
    await coord.get_directoryservices()
    assert coord.ds["directoryservices"] == {}


async def test_refresh_directoryservices_delegates_to_state() -> None:
    """The config+status merge, empty-when-unconfigured and healthy
    derivation these tests used to exercise directly now live in and are
    tested by aiotruenas's own TrueNASState.get_directoryservices().
    get_directoryservices just gates on the monitored group and delegates,
    so this only needs to lock in that plumbing."""
    coord = _bare_coordinator()
    coord.ds = {"directoryservices": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {
        CONF_MONITORED_GROUPS: [MONITOR_GROUP_DIRECTORY_SERVICES]
    }
    coord.state = MagicMock()
    coord.state.get_directoryservices = AsyncMock(return_value={1: {"healthy": True}})
    await coord.get_directoryservices()
    assert coord.ds["directoryservices"]["1"]["healthy"] is True


# ---------------------------
#   get_certificates
# ---------------------------
async def test_get_certificates_computes_days_until_expiry() -> None:
    coord = _bare_coordinator()
    coord.ds = {}
    coord.api = MagicMock()
    future = datetime.now(UTC) + timedelta(days=10)
    coord.api.query = AsyncMock(
        return_value=[
            {
                "id": 1,
                "name": "cert1",
                "cert_type": "CERTIFICATE",
                "until": future.strftime("%c"),
            }
        ]
    )
    await coord.get_certificates()
    assert coord.ds["certificate"]["cert1"]["days_until_expiry"] in (9, 10)


async def test_get_certificates_none_expiry_when_until_missing() -> None:
    coord = _bare_coordinator()
    coord.ds = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=[{"id": 1, "name": "cert1"}])
    await coord.get_certificates()
    assert coord.ds["certificate"]["cert1"]["days_until_expiry"] is None


async def test_get_certificates_keyed_by_common_name_when_present() -> None:
    coord = _bare_coordinator()
    coord.ds = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {
                "id": 1,
                "name": "letsencrypt-2026-08-27-172301",
                "common": "nas.example.com",
            }
        ]
    )
    await coord.get_certificates()
    cert = coord.ds["certificate"].get("nas.example.com")
    assert cert is not None
    assert cert["name"] == "letsencrypt-2026-08-27-172301"


async def test_get_certificates_falls_back_to_name_when_common_empty() -> None:
    coord = _bare_coordinator()
    coord.ds = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=[{"id": 1, "name": "cert1", "common": ""}])
    await coord.get_certificates()
    assert "cert1" in coord.ds["certificate"]


async def test_get_certificates_falls_back_to_name_on_shared_common() -> None:
    """Two certificates sharing a common name must not collapse into one.

    Keying both by the shared ``common`` would make the second entry
    overwrite the first under the same dict key, silently dropping one
    certificate's sensors -- see ``_assign_certificate_identities``.
    """
    coord = _bare_coordinator()
    coord.ds = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {"id": 1, "name": "cert-a", "common": "shared.example.com"},
            {"id": 2, "name": "cert-b", "common": "shared.example.com"},
        ]
    )
    await coord.get_certificates()
    assert set(coord.ds["certificate"].keys()) == {"cert-a", "cert-b"}


async def test_get_certificates_keeps_name_fallback_once_common_has_collided() -> None:
    """A common name that collided once must not flip back to identity later.

    If cert-b (sharing cert-a's common) disappears on the next poll, cert-a's
    common becomes unique again. Without persisting the collision, cert-a's
    identity would flip from ``name`` back to ``common``, changing its dict
    key/unique_id and orphaning its own recorder statistics -- the exact
    failure this migration exists to prevent (Sourcery finding on an earlier
    version of ``_assign_certificate_identities``).
    """
    coord = _bare_coordinator()
    coord.ds = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {"id": 1, "name": "cert-a", "common": "shared.example.com"},
            {"id": 2, "name": "cert-b", "common": "shared.example.com"},
        ]
    )
    await coord.get_certificates()
    assert set(coord.ds["certificate"].keys()) == {"cert-a", "cert-b"}

    coord.api.query = AsyncMock(
        return_value=[
            {"id": 1, "name": "cert-a", "common": "shared.example.com"},
        ]
    )
    await coord.get_certificates()
    assert set(coord.ds["certificate"].keys()) == {"cert-a"}


async def test_get_certificates_survives_name_rotation_with_stable_common() -> None:
    """A rotating ACME-style tool renaming the cert on every renewal (#113).

    Keeping ``common`` stable across polls must keep the same dict key/entity
    (rather than orphaning the previous name-keyed entry), even though the
    underlying ``id``/``name`` change on every run.
    """
    coord = _bare_coordinator()
    coord.ds = {}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {
                "id": 1,
                "name": "letsencrypt-2026-08-27-090000",
                "common": "nas.example.com",
            }
        ]
    )
    await coord.get_certificates()
    coord.api.query = AsyncMock(
        return_value=[
            {
                "id": 2,
                "name": "letsencrypt-2026-11-27-090000",
                "common": "nas.example.com",
            }
        ]
    )
    await coord.get_certificates()
    assert list(coord.ds["certificate"].keys()) == ["nas.example.com"]
    assert coord.ds["certificate"]["nas.example.com"]["name"] == (
        "letsencrypt-2026-11-27-090000"
    )
    assert coord.ds["certificate"]["nas.example.com"]["id"] == 2


# ---------------------------
#   get_arc
# ---------------------------
# The netdata-graph parsing these tests used to exercise directly now lives
# in and is tested by aiotruenas's own TrueNASState.get_arc(). get_arc just
# delegates and assigns the result, so this only needs to lock in that
# plumbing.
async def test_get_arc_delegates_to_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {}
    coord.state = MagicMock()
    coord.state.get_arc = AsyncMock(return_value={"data_hit_percent": 90.0})
    await coord.get_arc()
    assert coord.ds["arc"]["data_hit_percent"] == 90.0


# ---------------------------
#   get_ups
# ---------------------------
# The graph-discovery/parsing these tests used to exercise directly now
# lives in and is tested by aiotruenas's own TrueNASState.get_ups(). get_ups
# just gates on the monitored group and delegates, so this only needs to
# lock in that plumbing.
async def test_get_ups_empty_when_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"ups": {"stale": 1}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    await coord.get_ups()
    assert coord.ds["ups"] == {}


async def test_get_ups_delegates_to_state_when_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"ups": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_UPS]}
    coord.state = MagicMock()
    coord.state.get_ups = AsyncMock(return_value={"battery_charge": 80.0})
    await coord.get_ups()
    assert coord.ds["ups"]["battery_charge"] == 80.0


# ---------------------------
#   get_cloudsync / get_replication / get_rsync / get_snapshottask / get_scrub
# ---------------------------
async def test_get_cloudsync_empty_when_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"cloudsync": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    await coord.get_cloudsync()
    assert coord.ds["cloudsync"] == {}


async def test_get_cloudsync_parses_when_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"cloudsync": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_CLOUDSYNC]}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.state = MagicMock()
    coord.state.get_cloudsync = AsyncMock(
        return_value={"cs1": {"id": "cs1", "description": "backup"}}
    )
    await coord.get_cloudsync()
    assert "cs1" in coord.ds["cloudsync"]


async def test_get_cloudsync_not_monitored_stops_active_push() -> None:
    coord = _bare_coordinator()
    coord.ds = {"cloudsync": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock()
    consumer = MagicMock()
    consumer.stop = AsyncMock()
    coord._cloudsync_push.sub_id = "sub-1"
    coord._cloudsync_push.consumer = consumer

    await coord.get_cloudsync()

    assert coord.ds["cloudsync"] == {}
    consumer.stop.assert_awaited_once()
    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._cloudsync_push.sub_id is None


async def test_refresh_replication_delegates_to_state() -> None:
    """The job-state-fallback logic this test used to exercise directly now
    lives in and is tested by aiotruenas's own TrueNASState.get_replication().
    _refresh_replication just delegates and assigns the result, so this only
    needs to lock in that plumbing."""
    coord = _bare_coordinator()
    coord.ds = {"replication": {}}
    coord.state = MagicMock()
    coord.state.get_replication = AsyncMock(
        return_value={1: {"id": 1, "name": "repl1", "state": "RUNNING"}}
    )
    await coord._refresh_replication()
    assert coord.ds["replication"]["1"]["state"] == "RUNNING"


async def test_get_replication_empty_when_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"replication": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    coord.api.query = AsyncMock()
    await coord.get_replication()
    assert coord.ds["replication"] == {}


async def test_get_replication_not_monitored_stops_active_push() -> None:
    coord = _bare_coordinator()
    coord.ds = {"replication": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock()
    consumer = MagicMock()
    consumer.stop = AsyncMock()
    coord._replication_push.sub_id = "sub-1"
    coord._replication_push.consumer = consumer

    await coord.get_replication()

    assert coord.ds["replication"] == {}
    consumer.stop.assert_awaited_once()
    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._replication_push.sub_id is None


async def test_get_rsync_empty_when_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"rsynctask": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    coord.api.query = AsyncMock()
    await coord.get_rsync()
    assert coord.ds["rsynctask"] == {}


async def test_get_rsync_not_monitored_stops_active_push() -> None:
    coord = _bare_coordinator()
    coord.ds = {"rsynctask": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock()
    consumer = MagicMock()
    consumer.stop = AsyncMock()
    coord._rsync_push.sub_id = "sub-1"
    coord._rsync_push.consumer = consumer

    await coord.get_rsync()

    assert coord.ds["rsynctask"] == {}
    consumer.stop.assert_awaited_once()
    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._rsync_push.sub_id is None


async def test_get_rsync_parses_when_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"rsynctask": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_RSYNC]}
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord.state = MagicMock()
    coord.state.get_rsync = AsyncMock(return_value={1: {"id": 1, "path": "/mnt/tank"}})
    await coord.get_rsync()
    assert "1" in coord.ds["rsynctask"]


async def test_get_snapshottask_empty_when_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"snapshottask": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    await coord.get_snapshottask()
    assert coord.ds["snapshottask"] == {}


async def test_get_snapshottask_delegates_to_state_when_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"snapshottask": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: [MONITOR_GROUP_SNAPSHOTS]}
    schedule = {"minute": "0", "hour": "*", "dom": "*", "month": "*", "dow": "*"}
    coord.state = MagicMock()
    coord.state.get_snapshottask = AsyncMock(
        return_value={1: {"dataset": "tank/data", "schedule": schedule}}
    )
    await coord.get_snapshottask()
    assert "1" in coord.ds["snapshottask"]
    assert coord.ds["snapshottask"]["1"]["schedule"] == schedule


async def test_get_scrub_delegates_to_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {"scrub": {}}
    coord.state = MagicMock()
    coord.state.get_scrub = AsyncMock(return_value={1: {"pool_name": "tank"}})
    await coord.get_scrub()
    assert "1" in coord.ds["scrub"]


# ---------------------------
#   get_app / app update job tracking
# ---------------------------
# The running/update_available derivation (catalog upgrade_available vs.
# custom-app image_updates_available fallback) these tests used to exercise
# directly now lives in and is tested by aiotruenas's own
# TrueNASState.get_app(). _refresh_app just delegates and assigns the
# result, so this only needs to lock in that plumbing.
async def test_refresh_app_delegates_to_state() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {}}
    coord.state = MagicMock()
    coord.state.get_app = AsyncMock(
        return_value={"app1": {"running": True, "update_available": True}}
    )
    coord._refresh_app_update_jobs = AsyncMock()
    await coord._refresh_app()
    assert coord.ds["app"]["app1"]["running"] is True
    assert coord.ds["app"]["app1"]["update_available"] is True
    assert coord.ds["app"]["app1"]["update_jobid"] == 0


async def test_refresh_app_carries_forward_update_job_tracking() -> None:
    """An in-progress upgrade job's tracking state must survive across a
    poll, even though TrueNASState.get_app() returns a freshly-built dict
    that never carries these HA-specific fields (#101-style regression:
    losing update_jobid would strand the update entity 'in progress'
    forever)."""
    coord = _bare_coordinator()
    coord.ds = {
        "app": {
            "app1": {
                "update_jobid": 5,
                "update_progress": 42,
                "update_state": "RUNNING",
                "update_description": "Pulling image",
            }
        }
    }
    coord.state = MagicMock()
    coord.state.get_app = AsyncMock(return_value={"app1": {"running": True}})
    coord._refresh_app_update_jobs = AsyncMock()
    await coord._refresh_app()
    assert coord.ds["app"]["app1"]["update_jobid"] == 5
    assert coord.ds["app"]["app1"]["update_progress"] == 42
    assert coord.ds["app"]["app1"]["update_state"] == "RUNNING"


# ---------------------------
#   get_app push subscription wiring
# ---------------------------
async def test_get_app_ensures_push_subscription() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {}}
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.state = MagicMock()
    coord.state.get_app = AsyncMock(return_value={})
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord._refresh_app_update_jobs = AsyncMock()

    await coord.get_app()

    coord.api.subscribe_events.assert_awaited_once_with("app.query")
    assert coord._app_push.sub_id == "sub-1"
    await coord._app_push.consumer.stop()


async def test_on_app_push_refreshes_and_notifies() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {}}
    coord.state = MagicMock()
    coord.state.get_app = AsyncMock(return_value={"app1": {"running": True}})
    coord._refresh_app_update_jobs = AsyncMock()
    coord.async_set_updated_data = MagicMock()

    await coord._on_app_push([{"msg": "changed"}])

    assert coord.ds["app"]["app1"]["running"] is True
    coord.async_set_updated_data.assert_called_once_with(coord.ds)


async def test_stop_app_push_unsubscribes_and_clears() -> None:
    coord = _bare_coordinator()
    coord.hass = _hass_with_background_tasks()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.subscribe_events = AsyncMock(return_value=("sub-1", asyncio.Queue()))
    coord.api.unsubscribe_events = AsyncMock()
    await coord._ensure_push_subscription(
        coord._app_push,
        coord._APP_EVENT,
        coord._on_app_push,
        label="app",
    )

    await coord.stop_app_push()

    coord.api.unsubscribe_events.assert_awaited_once_with("sub-1")
    assert coord._app_push.sub_id is None


async def test_refresh_app_update_job_mirrors_running_progress() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"app1": {"update_jobid": 5}}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {
                "id": 5,
                "state": "RUNNING",
                "progress": {"percent": 42, "description": "Pulling image"},
            }
        ]
    )
    job = await coord.async_refresh_app_update_job("app1")
    assert job is not None
    assert job["state"] == "RUNNING"
    coord.api.query.assert_awaited_once_with("core.get_jobs", params=[[["id", "=", 5]]])
    app = coord.ds["app"]["app1"]
    assert app["update_jobid"] == 5
    assert app["update_state"] == "RUNNING"
    assert app["update_progress"] == 42
    assert app["update_description"] == "Pulling image"


async def test_refresh_app_update_job_resets_when_finished() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"app1": {"update_jobid": 5, "update_progress": 90}}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[{"id": 5, "state": "SUCCESS", "progress": {"percent": 100}}]
    )
    job = await coord.async_refresh_app_update_job("app1")
    assert job is not None
    assert job["state"] == "SUCCESS"
    app = coord.ds["app"]["app1"]
    assert app["update_jobid"] == 0
    assert app["update_state"] == "SUCCESS"
    # Final progress stays visible for troubleshooting.
    assert app["update_progress"] == 100


async def test_refresh_app_update_job_keeps_waiting_job_active() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"app1": {"update_jobid": 7}}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {
                "id": 7,
                "state": "WAITING",
                "progress": {"percent": 0, "description": "Queued"},
            }
        ]
    )
    job = await coord.async_refresh_app_update_job("app1")
    assert job is not None
    assert job["state"] == "WAITING"
    app = coord.ds["app"]["app1"]
    assert app["update_jobid"] == 7
    assert app["update_state"] == "WAITING"
    assert app["update_progress"] == 0
    assert app["update_description"] == "Queued"


async def test_refresh_app_update_job_failed_keeps_final_progress() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"app1": {"update_jobid": 5}}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {
                "id": 5,
                "state": "FAILED",
                "error": "pull failed",
                "progress": {"percent": 40, "description": "Pulling image"},
            }
        ]
    )
    job = await coord.async_refresh_app_update_job("app1")
    assert job is not None
    assert job["state"] == "FAILED"
    app = coord.ds["app"]["app1"]
    assert app["update_jobid"] == 0
    assert app["update_state"] == "FAILED"
    assert app["update_progress"] == 40
    assert app["update_description"] == "Pulling image"


async def test_refresh_app_update_job_resets_when_job_missing() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"app1": {"update_jobid": 5}}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=[])
    assert await coord.async_refresh_app_update_job("app1") is None
    assert coord.ds["app"]["app1"]["update_jobid"] == 0


async def test_refresh_app_update_job_keeps_job_on_api_error() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"app1": {"update_jobid": 5, "update_progress": 30}}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=None)
    assert await coord.async_refresh_app_update_job("app1") is None
    assert coord.ds["app"]["app1"]["update_jobid"] == 5
    assert coord.ds["app"]["app1"]["update_progress"] == 30


async def test_refresh_app_update_job_skips_without_jobid() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"app1": {"update_jobid": 0}}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock()
    assert await coord.async_refresh_app_update_job("app1") is None
    assert await coord.async_refresh_app_update_job("missing") is None
    coord.api.query.assert_not_awaited()


async def test_refresh_app_update_jobs_polls_every_tracked_app() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app": {
            "app1": {"update_jobid": 5},
            "app2": {"update_jobid": 0},
            "app3": {"update_jobid": 6},
        }
    }
    coord.async_refresh_app_update_job = AsyncMock()
    await coord._refresh_app_update_jobs()
    assert sorted(
        c.args[0] for c in coord.async_refresh_app_update_job.await_args_list
    ) == ["app1", "app3"]


# ---------------------------
#   app.stats subscription helpers
# ---------------------------
def test_get_app_identifier_prefers_name() -> None:
    coord = _bare_coordinator()
    assert coord._get_app_identifier({"name": "app1", "app_name": "legacy"}) == "app1"


def test_get_app_identifier_falls_back_to_app_name() -> None:
    coord = _bare_coordinator()
    assert coord._get_app_identifier({"app_name": "legacy"}) == "legacy"


def test_get_app_identifier_returns_none_when_missing() -> None:
    coord = _bare_coordinator()
    assert coord._get_app_identifier({}) is None


def test_resolve_app_stats_event_name_uses_poll_interval() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_POLL_INTERVAL: 30}
    assert coord._resolve_app_stats_event_name() == 'app.stats:{"interval": 30}'


def test_resolve_app_stats_event_name_falls_back_on_invalid_value() -> None:
    coord = _bare_coordinator()
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_POLL_INTERVAL: "bad"}
    assert (
        coord._resolve_app_stats_event_name()
        == f'app.stats:{{"interval": {DEFAULT_POLL_INTERVAL}}}'
    )


async def test_stop_app_stats_if_active_stops_when_subscribed() -> None:
    coord = _bare_coordinator()
    coord._app_stats_sub_id = "sub-1"
    coord.stop_app_stats = AsyncMock()
    await coord._stop_app_stats_if_active()
    coord.stop_app_stats.assert_awaited_once_with(force=True)


async def test_stop_app_stats_if_active_noop_when_not_subscribed() -> None:
    coord = _bare_coordinator()
    coord._app_stats_sub_id = None
    coord.stop_app_stats = AsyncMock()
    await coord._stop_app_stats_if_active()
    coord.stop_app_stats.assert_not_awaited()


async def test_maybe_teardown_changed_app_stats_subscription_stops_on_change() -> None:
    coord = _bare_coordinator()
    coord._app_stats_event_name = "old"
    coord.stop_app_stats = AsyncMock()
    await coord._maybe_teardown_changed_app_stats_subscription("new")
    coord.stop_app_stats.assert_awaited_once_with(force=True)


async def test_maybe_clear_inactive_app_stats_subscription_clears_when_inactive() -> (
    None
):
    coord = _bare_coordinator()
    coord._app_stats_sub_id = "sub-1"
    coord.api = MagicMock()
    coord.api.is_subscribed = AsyncMock(return_value=False)
    await coord._maybe_clear_inactive_app_stats_subscription()
    assert coord._app_stats_sub_id is None


async def test_subscribe_to_app_stats_handles_missing_sub_id() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.subscribe_events = AsyncMock(return_value=(None, None))
    await coord._subscribe_to_app_stats("event")
    assert coord._app_stats_sub_id is None


async def test_subscribe_to_app_stats_handles_exception() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.subscribe_events = AsyncMock(side_effect=Exception("boom"))
    await coord._subscribe_to_app_stats("event")  # must not raise
    assert coord._app_stats_sub_id is None


async def test_stop_app_stats_unsubscribe_exception_still_clears_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=True)
    coord.api.unsubscribe_events = AsyncMock(side_effect=Exception("boom"))
    coord._app_stats_sub_id = "sub-1"
    with caplog.at_level("DEBUG"):
        await coord.stop_app_stats()
    assert coord._app_stats_sub_id is None


async def test_stop_app_stats_not_connected_no_force_is_noop() -> None:
    coord = _bare_coordinator()
    coord.api = MagicMock()
    coord.api.connected = MagicMock(return_value=False)
    coord._app_stats_sub_id = "sub-1"
    coord._app_stats_event_name = "event"
    await coord.stop_app_stats(force=False)
    assert coord._app_stats_sub_id == "sub-1"


def test_coerce_float_handles_invalid_values() -> None:
    coord = _bare_coordinator()
    assert coord._coerce_float(None) is None
    assert coord._coerce_float("bad") is None
    assert coord._coerce_float("3.5") == pytest.approx(3.5)


def test_collect_current_app_names_uses_identifier() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app": {"a": {"name": "app1"}, "b": "not-a-dict"}}
    assert coord._collect_current_app_names() == {"app1"}


def test_prune_stale_app_stats_removes_missing_entries() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app_stats": {"app1": {}, "stale": {}}}
    coord._prune_stale_app_stats({"app1"})
    assert coord.ds["app_stats"] == {"app1": {}}


# ---------------------------
#   get_cronjob
# ---------------------------
async def test_get_cronjob_empty_when_not_monitored() -> None:
    coord = _bare_coordinator()
    coord.ds = {"cronjob": {"stale": {}}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {CONF_MONITORED_GROUPS: []}
    coord.api = MagicMock()
    await coord.get_cronjob()
    assert coord.ds["cronjob"] == {}


# display_name derivation now lives in and is tested by aiotruenas's own
# TrueNASState.get_cronjob(); the "skip disabled" filter below stays local
# (an HA options-flow behavior), so these tests mock the delegated result
# and lock in only the filtering plumbing.
async def test_get_cronjob_skips_disabled_by_default_behavior() -> None:
    coord = _bare_coordinator()
    coord.ds = {"cronjob": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {
        CONF_MONITORED_GROUPS: [MONITOR_GROUP_CRONJOBS],
        CONF_BEHAVIORS: [BEHAVIOR_SKIP_DISABLED_CRONJOBS],
    }
    coord.state = MagicMock()
    coord.state.get_cronjob = AsyncMock(
        return_value={
            1: {"enabled": True, "display_name": "Job A"},
            2: {"enabled": False, "display_name": "Job B"},
        }
    )
    await coord.get_cronjob()
    assert "1" in coord.ds["cronjob"]
    assert "2" not in coord.ds["cronjob"]
    assert coord.ds["cronjob"]["1"]["display_name"] == "Job A"


async def test_get_cronjob_keeps_disabled_when_behavior_off() -> None:
    coord = _bare_coordinator()
    coord.ds = {"cronjob": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {
        CONF_MONITORED_GROUPS: [MONITOR_GROUP_CRONJOBS],
        CONF_BEHAVIORS: [],
    }
    coord.state = MagicMock()
    coord.state.get_cronjob = AsyncMock(
        return_value={2: {"enabled": False, "display_name": "ls"}}
    )
    await coord.get_cronjob()
    assert coord.ds["cronjob"]["2"]["display_name"] == "ls"


async def test_get_cronjob_falls_back_to_legacy_option_when_behaviors_absent() -> None:
    coord = _bare_coordinator()
    coord.ds = {"cronjob": {}}
    coord.config_entry = MagicMock()
    coord.config_entry.options = {
        CONF_MONITORED_GROUPS: [MONITOR_GROUP_CRONJOBS],
        "cronjob_skip_disabled": False,
    }
    coord.config_entry.data = {}
    coord.state = MagicMock()
    coord.state.get_cronjob = AsyncMock(
        return_value={3: {"enabled": False, "display_name": "Cronjob 3"}}
    )
    await coord.get_cronjob()
    assert coord.ds["cronjob"]["3"]["display_name"] == "Cronjob 3"


# ---------------------------
#   app.stats: stopped apps keep their network interfaces
# ---------------------------
def _stats_entry(name: str, networks: list[dict] | None) -> dict:
    entry: dict = {"app_name": name, "cpu_usage": 0, "memory": 0}
    if networks is not None:
        entry["networks"] = networks
    return entry


def test_upsert_app_stats_keeps_known_interfaces_when_app_stops() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app_stats": {
            "plex": {
                "app_name": "plex",
                "networks": [
                    {"interface_name": "eth0", "rx_bytes": 10, "tx_bytes": 20}
                ],
            }
        }
    }
    coord._upsert_app_stats_entry(_stats_entry("plex", []))
    assert coord.ds["app_stats"]["plex"]["networks"] == [
        {"interface_name": "eth0", "rx_bytes": None, "tx_bytes": None, "stale": True}
    ]


def test_upsert_app_stats_no_interfaces_without_prior_knowledge() -> None:
    coord = _bare_coordinator()
    coord.ds = {"app_stats": {}}
    coord._upsert_app_stats_entry(_stats_entry("plex", []))
    assert coord.ds["app_stats"]["plex"]["networks"] == []


def test_upsert_app_stats_live_interfaces_replace_stale_ones() -> None:
    coord = _bare_coordinator()
    coord.ds = {
        "app_stats": {
            "plex": {
                "app_name": "plex",
                "networks": [
                    {
                        "interface_name": "eth0",
                        "rx_bytes": None,
                        "tx_bytes": None,
                        "stale": True,
                    }
                ],
            }
        }
    }
    coord._upsert_app_stats_entry(
        _stats_entry("plex", [{"interface_name": "eth0", "rx_bytes": 5, "tx_bytes": 6}])
    )
    assert coord.ds["app_stats"]["plex"]["networks"] == [
        {"interface_name": "eth0", "rx_bytes": 5, "tx_bytes": 6}
    ]
