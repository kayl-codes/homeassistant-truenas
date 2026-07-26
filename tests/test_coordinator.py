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

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import slugify

from custom_components.truenas_ce import coordinator as coordinator_module
from custom_components.truenas_ce.const import (
    CONF_MONITORED_GROUPS,
    CONF_POLL_INTERVAL,
    DEFAULT_DEVICE_NAME,
    DEFAULT_POLL_INTERVAL,
    LEGACY_DOMAIN,
    MIGRATION_LEGACY_ENTRY_ID,
    MONITOR_GROUP_CONTAINERS,
    MONITOR_GROUP_VMS,
)
from custom_components.truenas_ce.coordinator import (
    TrueNASCoordinator,
    _accumulate_vdev_errors,
    _aggregate_topology_errors,
    _arc_value,
    _as_int,
    _first_ipv4,
    _is_truenas_sensor_id,
    _median,
    _netdata_mean_value,
    _stat_name_similar,
    _to_int,
)


def _bare_coordinator() -> TrueNASCoordinator:
    """Build a TrueNASCoordinator without running its hass-dependent __init__."""
    coord = TrueNASCoordinator.__new__(TrueNASCoordinator)
    coord._app_stats_event_name = None
    coord._app_stats_sub_id = None
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
#   _median
# ---------------------------
def test_median_odd_count() -> None:
    assert _median([3.0, 1.0, 2.0]) == pytest.approx(2.0)


def test_median_even_count() -> None:
    assert _median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)


def test_median_single_value() -> None:
    assert _median([42.0]) == pytest.approx(42.0)


def test_median_empty_list_raises_index_error() -> None:
    """Empty input is outside _median's contract (docstring: non-empty list);
    its only caller guards with a non-empty check. Lock in the current
    fail-loud behaviour instead of silently returning a value."""
    with pytest.raises(IndexError):
        _median([])


# ---------------------------
#   _as_int / _to_int
# ---------------------------
def test_as_int_returns_int_unchanged() -> None:
    assert _as_int(5) == 5


def test_as_int_returns_zero_for_non_int() -> None:
    assert _as_int("5") == 0
    assert _as_int(None) == 0
    assert _as_int(1.5) == 0


def test_to_int_parses_numeric_string() -> None:
    assert _to_int("48") == 48


def test_to_int_falls_back_to_default_on_invalid() -> None:
    assert _to_int("not-a-number", default=7) == 7
    assert _to_int(None, default=7) == 7


# ---------------------------
#   _accumulate_vdev_errors / _aggregate_topology_errors
# ---------------------------
def test_accumulate_vdev_errors_leaf_disk() -> None:
    totals = {"read": 0, "write": 0, "checksum": 0}
    vdev = {"stats": {"read_errors": 1, "write_errors": 2, "checksum_errors": 3}}
    _accumulate_vdev_errors(vdev, totals)
    assert totals == {"read": 1, "write": 2, "checksum": 3}


def test_accumulate_vdev_errors_recurses_into_children_only() -> None:
    """A mirror vdev's own stats must not be double-counted on top of its disks."""
    totals = {"read": 0, "write": 0, "checksum": 0}
    mirror = {
        "stats": {"read_errors": 99, "write_errors": 99, "checksum_errors": 99},
        "children": [
            {"stats": {"read_errors": 1, "write_errors": 0, "checksum_errors": 0}},
            {"stats": {"read_errors": 0, "write_errors": 1, "checksum_errors": 0}},
        ],
    }
    _accumulate_vdev_errors(mirror, totals)
    assert totals == {"read": 1, "write": 1, "checksum": 0}


def test_accumulate_vdev_errors_ignores_non_dict() -> None:
    totals = {"read": 0, "write": 0, "checksum": 0}
    _accumulate_vdev_errors("not-a-dict", totals)
    assert totals == {"read": 0, "write": 0, "checksum": 0}


def test_aggregate_topology_errors_sums_all_categories() -> None:
    topology = {
        "data": [
            {"stats": {"read_errors": 1, "write_errors": 0, "checksum_errors": 0}}
        ],
        "cache": [
            {"stats": {"read_errors": 0, "write_errors": 2, "checksum_errors": 0}}
        ],
    }
    assert _aggregate_topology_errors(topology) == (1, 2, 0)


def test_aggregate_topology_errors_non_dict_returns_zeros() -> None:
    assert _aggregate_topology_errors(None) == (0, 0, 0)


# ---------------------------
#   _netdata_mean_value / _arc_value / _ups_value
# ---------------------------
def test_netdata_mean_value_computes_mean() -> None:
    graph_data = [{"aggregations": {"mean": {"a": 1.0, "b": 3.0}}}]
    assert _netdata_mean_value(graph_data) == pytest.approx(2.0)


def test_netdata_mean_value_returns_none_for_empty_list() -> None:
    assert _netdata_mean_value([]) is None


def test_netdata_mean_value_returns_none_for_malformed_item() -> None:
    assert _netdata_mean_value(["not-a-dict"]) is None
    assert _netdata_mean_value([{"aggregations": {"mean": "not-a-dict"}}]) is None
    assert _netdata_mean_value([{"aggregations": {"mean": {}}}]) is None


def test_arc_value_delegates_to_netdata_mean_value() -> None:
    graph_data = [{"aggregations": {"mean": {"a": 10.0}}}]
    assert _arc_value(graph_data) == pytest.approx(10.0)


# ---------------------------
#   _first_ipv4
# ---------------------------
def test_first_ipv4_returns_first_inet_address() -> None:
    aliases = [
        {"type": "INET6", "address": "2001:db8::1"},
        {"type": "INET", "address": "192.0.2.5"},
        {"type": "INET", "address": "192.0.2.6"},
    ]
    assert _first_ipv4(aliases) == "192.0.2.5"


def test_first_ipv4_returns_unknown_when_no_inet() -> None:
    assert _first_ipv4([{"type": "INET6", "address": "2001:db8::1"}]) == "unknown"
    assert _first_ipv4(None) == "unknown"
    assert _first_ipv4([]) == "unknown"


# ---------------------------
#   _is_truenas_sensor_id
# ---------------------------
def test_is_truenas_sensor_id_matches_device_slug_token() -> None:
    slug = slugify(DEFAULT_DEVICE_NAME)
    assert _is_truenas_sensor_id(f"sensor.{slug}_cpu_usage") is True
    assert _is_truenas_sensor_id(f"sensor.system_{slug}_uptime") is True
    assert _is_truenas_sensor_id(f"sensor.{slug}viacfnoauth_cpu") is True


def test_is_truenas_sensor_id_rejects_other_domains() -> None:
    assert _is_truenas_sensor_id("sensor.unrelated_integration_temp") is False


def test_is_truenas_sensor_id_rejects_non_sensor_entities() -> None:
    slug = slugify(DEFAULT_DEVICE_NAME)
    assert _is_truenas_sensor_id(f"binary_sensor.{slug}_online") is False


def test_is_truenas_sensor_id_unaffected_by_domain_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: matching used to depend on DOMAIN/LEGACY_DOMAIN, which broke
    since the 2.0.0 CE rename because DOMAIN ("truenas_ce") contains an
    underscore and can never appear whole inside an underscore-split token.
    The fix matches ``slugify(DEFAULT_DEVICE_NAME)`` instead -- the same
    string real entity ids are slugged from -- so behavior no longer depends
    on DOMAIN/LEGACY_DOMAIN at all, even if both constants are ever renamed or
    removed (e.g. a future HA Core submission dropping the "_ce" suffix).
    """
    monkeypatch.setattr(coordinator_module, "DOMAIN", "something_else_entirely")
    monkeypatch.setattr(coordinator_module, "LEGACY_DOMAIN", "unrelated")
    assert _is_truenas_sensor_id("sensor.truenas_cpu_usage") is True


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
#   _apply_pool_capacity
# ---------------------------
def test_apply_pool_capacity_uses_root_dataset_when_available() -> None:
    coord = _bare_coordinator()
    coord.ds = {"pool": {"p1": {}}}
    root_dataset = {"available": 40, "used": 60}
    coord._apply_pool_capacity("p1", {}, root_dataset)
    assert coord.ds["pool"]["p1"]["available"] == 40
    assert coord.ds["pool"]["p1"]["total"] == 100
    assert coord.ds["pool"]["p1"]["usage"] == 60


def test_apply_pool_capacity_falls_back_to_pool_fields_without_root_dataset() -> None:
    coord = _bare_coordinator()
    coord.ds = {"pool": {"p1": {}}}
    vals = {"free": 30, "size": 100}
    coord._apply_pool_capacity("p1", vals, None)
    assert coord.ds["pool"]["p1"]["available"] == 30
    assert coord.ds["pool"]["p1"]["total"] == 100
    assert coord.ds["pool"]["p1"]["usage"] == 70


def test_apply_pool_capacity_zero_total_yields_zero_usage() -> None:
    coord = _bare_coordinator()
    coord.ds = {"pool": {"p1": {}}}
    coord._apply_pool_capacity("p1", {"free": 0, "size": 0, "allocated": 0}, None)
    assert coord.ds["pool"]["p1"]["usage"] == 0


# ---------------------------
#   _apply_pool_errors
# ---------------------------
def test_apply_pool_errors_aggregates_into_matching_pool() -> None:
    coord = _bare_coordinator()
    coord.ds = {"pool": {"guid1": {}}}
    raw_pools = [
        {
            "guid": "guid1",
            "topology": {
                "data": [
                    {
                        "stats": {
                            "read_errors": 1,
                            "write_errors": 2,
                            "checksum_errors": 3,
                        }
                    }
                ]
            },
        }
    ]
    coord._apply_pool_errors(raw_pools)
    pool = coord.ds["pool"]["guid1"]
    assert (pool["read_errors"], pool["write_errors"], pool["checksum_errors"]) == (
        1,
        2,
        3,
    )
    assert pool["errors"] == 6


def test_apply_pool_errors_skips_unknown_guid_and_non_dict_entries() -> None:
    coord = _bare_coordinator()
    coord.ds = {"pool": {"guid1": {}}}
    coord._apply_pool_errors([{"guid": "unknown-guid", "topology": {}}, "not-a-dict"])
    assert coord.ds["pool"]["guid1"] == {}


def test_apply_pool_errors_noop_for_non_list_input() -> None:
    coord = _bare_coordinator()
    coord.ds = {"pool": {"guid1": {}}}
    coord._apply_pool_errors(None)
    assert coord.ds["pool"]["guid1"] == {}


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


def test_store_stat_value_arcsize_uses_dedicated_key() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._store_stat_value("arcsize", "size", 12.345)
    assert coord.ds["system_info"]["cache_size-arc_value"] == pytest.approx(12.35)


def test_store_stat_value_memory_only_stores_available() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord._store_stat_value("memory", "available", 100.0)
    assert coord.ds["system_info"]["memory-free_value"] == 100
    coord._store_stat_value("memory", "used", 50.0)
    assert "memory-used" not in coord.ds["system_info"]


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
async def test_get_alerts_malformed_response_resets_to_defaults() -> None:
    coord = _bare_coordinator()
    coord.ds = {"alerts": {}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value={"not": "a list"})
    await coord.get_alerts()
    assert coord.ds["alerts"] == {
        "count": 0,
        "messages": [],
        "critical": 0,
        "warning": 0,
        "info": 0,
        "disk_issues": False,
    }


async def test_get_alerts_filters_dismissed_and_counts_levels() -> None:
    coord = _bare_coordinator()
    coord.ds = {"alerts": {}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {
                "dismissed": True,
                "level": "CRITICAL",
                "klass": "disk",
                "formatted": "ignored",
            },
            {
                "dismissed": False,
                "level": "CRITICAL",
                "klass": "PoolUsage",
                "formatted": "Pool full",
                "uuid": "u1",
            },
            {
                "dismissed": False,
                "level": "WARNING",
                "klass": "Other",
                "title": "SMART failure",
                "formatted": "Smart warning",
                "uuid": "u2",
            },
            {
                "dismissed": False,
                "level": "INFO",
                "klass": "Other",
                "title": "",
                "formatted": "Just info",
            },
        ]
    )
    await coord.get_alerts()
    alerts = coord.ds["alerts"]
    assert alerts["count"] == 3
    assert alerts["critical"] == 1
    assert alerts["warning"] == 1
    assert alerts["info"] == 1
    assert alerts["disk_issues"] is True
    assert alerts["messages"] == ["Pool full", "Smart warning", "Just info"]
    assert alerts["uuids"] == ["u1", "u2"]


async def test_get_alerts_no_disk_issues_when_unrelated() -> None:
    coord = _bare_coordinator()
    coord.ds = {"alerts": {}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value=[
            {
                "dismissed": False,
                "level": "INFO",
                "klass": "CertificateExpiry",
                "title": "cert",
                "formatted": "Cert expiring",
            }
        ]
    )
    await coord.get_alerts()
    assert coord.ds["alerts"]["disk_issues"] is False


# ---------------------------
#   get_smb
# ---------------------------
async def test_get_smb_counts_list_response() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=[{"id": 1}, {"id": 2}])
    await coord.get_smb()
    assert coord.ds["system_info"]["smb_connections"] == 2


async def test_get_smb_counts_dict_with_sessions() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value={"sessions": [{}, {}, {}]})
    await coord.get_smb()
    assert coord.ds["system_info"]["smb_connections"] == 3


async def test_get_smb_defaults_to_zero_for_unexpected_shape() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value=None)
    await coord.get_smb()
    assert coord.ds["system_info"]["smb_connections"] == 0


# ---------------------------
#   get_updatecheck
# ---------------------------
async def test_get_updatecheck_malformed_response_resets_idle() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"version": "25.04.1"}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value="not-a-dict")
    await coord.get_updatecheck()
    info = coord.ds["system_info"]
    assert info["update_available"] is False
    assert info["update_version"] == "25.04.1"


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


async def test_get_updatecheck_empty_response_resets_status() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"version": "25.04.1"}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(return_value={})
    await coord.get_updatecheck()
    info = coord.ds["system_info"]
    assert info["update_available"] is False
    assert info["update_version"] == "25.04.1"


async def test_get_updatecheck_new_version_available() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"version": "25.04.1"}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value={
            "status": {
                "state": "AVAILABLE",
                "new_version": {
                    "version": "25.10.0",
                    "manifest": {
                        "date": "2026-01-01",
                        "profile": "stable",
                        "train": "SCALE",
                        "filename": "update.pkg",
                    },
                },
            }
        }
    )
    await coord.get_updatecheck()
    info = coord.ds["system_info"]
    assert info["update_available"] is True
    assert info["update_version"] == "25.10.0"
    assert info["update_state"] == "AVAILABLE"
    assert info["update_date"] == "2026-01-01"
    assert info["update_train"] == "SCALE"


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


async def test_get_updatecheck_no_new_version_resets_status() -> None:
    coord = _bare_coordinator()
    coord.ds = {"system_info": {"version": "25.04.1", "update_available": True}}
    coord.api = MagicMock()
    coord.api.query = AsyncMock(
        return_value={"status": {"state": "IDLE", "new_version": None}}
    )
    await coord.get_updatecheck()
    info = coord.ds["system_info"]
    assert info["update_available"] is False
    assert info["update_version"] == "25.04.1"
