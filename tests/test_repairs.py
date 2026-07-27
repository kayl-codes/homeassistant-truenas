"""Unit tests for repairs.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from _fakes import make_coordinator

from custom_components.truenas_ce.const import (
    CONF_STATISTICS_CLEANUP_IGNORED,
    MIGRATION_RECORDS,
)
from custom_components.truenas_ce.repairs import (
    MigrationRollbackRepairFlow,
    StatisticsCleanupRepairFlow,
    async_create_fix_flow,
)


def _close_coroutine(coro: object) -> None:
    """Close an unused coroutine so pytest doesn't warn about it never being awaited."""
    if hasattr(coro, "close"):
        coro.close()


def _make_hass(entry: SimpleNamespace | None) -> SimpleNamespace:
    return SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_entry=MagicMock(return_value=entry),
            async_update_entry=MagicMock(),
        ),
        async_create_task=MagicMock(side_effect=_close_coroutine),
    )


async def test_create_fix_flow_routes_statistics_issue() -> None:
    flow = await async_create_fix_flow(
        SimpleNamespace(), "statistics_orphaned_entry1", None
    )
    assert isinstance(flow, StatisticsCleanupRepairFlow)
    assert flow._entry_id == "entry1"


async def test_create_fix_flow_routes_migration_rollback_issue() -> None:
    flow = await async_create_fix_flow(
        SimpleNamespace(), "migration_rollback_available_entry2", None
    )
    assert isinstance(flow, MigrationRollbackRepairFlow)
    assert flow._entry_id == "entry2"


async def test_statistics_cleanup_init_shows_menu_with_count() -> None:
    coordinator = make_coordinator()
    coordinator.orphaned_statistics = ["sensor.a", "sensor.b"]
    entry = SimpleNamespace(runtime_data=coordinator)
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(entry)

    result = await flow.async_step_init()
    assert result["description_placeholders"] == {"count": "2"}


async def test_statistics_cleanup_init_no_coordinator_zero_count() -> None:
    entry = SimpleNamespace(runtime_data=None)
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(entry)

    result = await flow.async_step_init()
    assert result["description_placeholders"] == {"count": "0"}


async def test_statistics_cleanup_fix_clears_statistics() -> None:
    coordinator = make_coordinator()
    entry = SimpleNamespace(runtime_data=coordinator)
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(entry)

    result = await flow.async_step_fix()
    coordinator.async_clear_orphaned_statistics.assert_awaited_once()
    assert result["type"] == "create_entry"


async def test_statistics_cleanup_fix_no_coordinator_is_noop() -> None:
    entry = SimpleNamespace(runtime_data=None)
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(entry)

    result = await flow.async_step_fix()
    assert result["type"] == "create_entry"


async def test_statistics_cleanup_ignore_sets_option_and_deletes_issue() -> None:
    entry = SimpleNamespace(options={}, runtime_data=None)
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(entry)

    with patch(
        "custom_components.truenas_ce.repairs.ir.async_delete_issue"
    ) as delete_issue:
        result = await flow.async_step_ignore()

    flow.hass.config_entries.async_update_entry.assert_called_once_with(
        entry, options={CONF_STATISTICS_CLEANUP_IGNORED: True}
    )
    delete_issue.assert_called_once()
    assert result["type"] == "create_entry"


async def test_statistics_cleanup_ignore_no_entry_still_deletes_issue() -> None:
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(None)

    with patch(
        "custom_components.truenas_ce.repairs.ir.async_delete_issue"
    ) as delete_issue:
        await flow.async_step_ignore()

    flow.hass.config_entries.async_update_entry.assert_not_called()
    delete_issue.assert_called_once()


async def test_migration_rollback_init_counts_adopted_entities() -> None:
    entry = SimpleNamespace(data={MIGRATION_RECORDS: ["a", "b", "c"]})
    flow = MigrationRollbackRepairFlow("entry2")
    flow.hass = _make_hass(entry)

    result = await flow.async_step_init()
    assert result["description_placeholders"] == {"count": "3"}


async def test_migration_rollback_init_no_entry_zero_count() -> None:
    flow = MigrationRollbackRepairFlow("entry2")
    flow.hass = _make_hass(None)

    result = await flow.async_step_init()
    assert result["description_placeholders"] == {"count": "0"}


async def test_migration_rollback_step_schedules_rollback_task() -> None:
    entry = SimpleNamespace(data={})
    flow = MigrationRollbackRepairFlow("entry2")
    flow.hass = _make_hass(entry)

    with (
        patch(
            "custom_components.truenas_ce.repairs.ir.async_delete_issue"
        ) as delete_issue,
        patch(
            "custom_components.truenas_ce.repairs.async_rollback_to_legacy",
            new=AsyncMock(),
        ),
    ):
        result = await flow.async_step_rollback()

    flow.hass.async_create_task.assert_called_once()
    delete_issue.assert_called_once()
    assert result["type"] == "create_entry"


async def test_migration_rollback_step_no_entry_skips_task() -> None:
    flow = MigrationRollbackRepairFlow("entry2")
    flow.hass = _make_hass(None)

    with patch(
        "custom_components.truenas_ce.repairs.ir.async_delete_issue"
    ) as delete_issue:
        await flow.async_step_rollback()

    flow.hass.async_create_task.assert_not_called()
    delete_issue.assert_called_once()


async def test_migration_rollback_ignore_deletes_issue() -> None:
    flow = MigrationRollbackRepairFlow("entry2")
    flow.hass = _make_hass(None)

    with patch(
        "custom_components.truenas_ce.repairs.ir.async_delete_issue"
    ) as delete_issue:
        result = await flow.async_step_ignore()

    delete_issue.assert_called_once()
    assert result["type"] == "create_entry"
