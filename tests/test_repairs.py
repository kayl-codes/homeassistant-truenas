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
    MAX_LISTED_ORPHANS,
    MigrationRollbackRepairFlow,
    StatisticsCleanupRepairFlow,
    _async_rollback_and_log_errors,
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

    # Confirm the entry id was parsed out correctly by observing which entry
    # the flow acts on, rather than reading the private ``_entry_id`` field.
    flow.hass = _make_hass(None)
    await flow.async_step_init()
    flow.hass.config_entries.async_get_entry.assert_called_once_with("entry1")


async def test_create_fix_flow_routes_migration_rollback_issue() -> None:
    flow = await async_create_fix_flow(
        SimpleNamespace(), "migration_rollback_available_entry2", None
    )
    assert isinstance(flow, MigrationRollbackRepairFlow)

    flow.hass = _make_hass(None)
    await flow.async_step_init()
    flow.hass.config_entries.async_get_entry.assert_called_once_with("entry2")


async def test_statistics_cleanup_init_lists_ids_when_data_remains() -> None:
    """Orphans that still hold data points get the "look them up" wording.

    The dialog renders the coordinator's order as-is — the list is already
    sorted where it is built, in ``async_detect_orphaned_statistics``.
    """
    coordinator = make_coordinator()
    coordinator.orphaned_statistics = ["sensor.a", "sensor.b"]
    coordinator.async_count_orphans_with_data = AsyncMock(return_value=2)
    entry = SimpleNamespace(runtime_data=coordinator)
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(entry)

    result = await flow.async_step_init()
    assert result["step_id"] == "init"
    assert result["description_placeholders"] == {
        "count": "2",
        "with_data": "2",
        "entities": "- `sensor.a`\n- `sensor.b`",
    }


async def test_statistics_cleanup_init_uses_metadata_only_step() -> None:
    """Without any data points the dialog must not point at Developer Tools."""
    coordinator = make_coordinator()
    coordinator.orphaned_statistics = ["sensor.a"]
    coordinator.async_count_orphans_with_data = AsyncMock(return_value=0)
    entry = SimpleNamespace(runtime_data=coordinator)
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(entry)

    result = await flow.async_step_init()
    assert result["step_id"] == "init_metadata_only"
    assert result["description_placeholders"]["with_data"] == "0"
    # Metadata-only orphans are still named: the id list is the only thing the
    # user can act on once the data points are gone.
    assert result["description_placeholders"]["entities"] == "- `sensor.a`"


async def test_statistics_cleanup_init_truncates_long_id_list() -> None:
    coordinator = make_coordinator()
    coordinator.orphaned_statistics = [
        f"sensor.truenas_{index:02d}" for index in range(MAX_LISTED_ORPHANS + 5)
    ]
    coordinator.async_count_orphans_with_data = AsyncMock(return_value=1)
    entry = SimpleNamespace(runtime_data=coordinator)
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(entry)

    result = await flow.async_step_init()
    entities = result["description_placeholders"]["entities"]
    assert entities.count("\n") == MAX_LISTED_ORPHANS
    assert entities.endswith("- … (+5)")


async def test_statistics_cleanup_init_no_coordinator_zero_count() -> None:
    entry = SimpleNamespace(runtime_data=None)
    flow = StatisticsCleanupRepairFlow("entry1")
    flow.hass = _make_hass(entry)

    result = await flow.async_step_init()
    assert result["description_placeholders"] == {
        "count": "0",
        "with_data": "0",
        "entities": "",
    }


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


async def test_rollback_task_success_raises_no_failure_issue() -> None:
    """The success path must not touch the issue registry at all."""
    hass = SimpleNamespace()
    entry = SimpleNamespace(entry_id="entry2", title="TrueNAS")

    with (
        patch(
            "custom_components.truenas_ce.repairs.async_rollback_to_legacy",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.truenas_ce.repairs.ir.async_create_issue"
        ) as create_issue,
    ):
        await _async_rollback_and_log_errors(hass, entry)

    create_issue.assert_not_called()


async def test_rollback_task_failure_raises_visible_issue() -> None:
    """A failed background rollback must surface as its own Repairs issue.

    The original migration_rollback_available issue is already deleted by
    the caller before this task can finish (see
    MigrationRollbackRepairFlow.async_step_rollback), so this is the only
    remaining UI-visible signal that the rollback didn't actually happen.
    """
    hass = SimpleNamespace()
    entry = SimpleNamespace(entry_id="entry2", title="TrueNAS")

    with (
        patch(
            "custom_components.truenas_ce.repairs.async_rollback_to_legacy",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(
            "custom_components.truenas_ce.repairs.ir.async_create_issue"
        ) as create_issue,
    ):
        await _async_rollback_and_log_errors(hass, entry)

    create_issue.assert_called_once()
    _, args, kwargs = create_issue.mock_calls[0]
    assert args == (hass, "truenas_ce", "migration_rollback_failed_entry2")
    assert kwargs["is_fixable"] is False
    assert kwargs["translation_key"] == "migration_rollback_failed"
    assert kwargs["translation_placeholders"] == {"name": "TrueNAS"}
