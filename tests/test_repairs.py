"""Unit tests for repairs.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from _fakes import make_coordinator
from homeassistant.config_entries import UnknownEntry

from custom_components.truenas_ce.const import (
    CONF_STATISTICS_CLEANUP_IGNORED,
    MIGRATION_LEGACY_ENTRY_ID,
    MIGRATION_RECORDS,
)
from custom_components.truenas_ce.repairs import (
    MAX_LISTED_ORPHANS,
    LegacyEntryOrphanedRepairFlow,
    MigrationRollbackRepairFlow,
    StatisticsCleanupRepairFlow,
    _async_rollback_and_log_errors,
    async_create_fix_flow,
)

_CODE_CHECK = "custom_components.truenas_ce.repairs.async_legacy_code_installed"


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
        patch(_CODE_CHECK, new=AsyncMock(return_value=True)),
    ):
        result = await flow.async_step_rollback()

    flow.hass.async_create_task.assert_called_once()
    delete_issue.assert_called_once()
    assert result["type"] == "create_entry"


async def test_migration_rollback_step_no_entry_skips_task() -> None:
    flow = MigrationRollbackRepairFlow("entry2")
    flow.hass = _make_hass(None)

    with (
        patch(
            "custom_components.truenas_ce.repairs.ir.async_delete_issue"
        ) as delete_issue,
        patch(_CODE_CHECK, new=AsyncMock(return_value=True)),
    ):
        await flow.async_step_rollback()

    flow.hass.async_create_task.assert_not_called()
    delete_issue.assert_called_once()


async def test_migration_rollback_step_aborts_when_legacy_code_missing() -> None:
    """HACS-only uninstall (#201): explain it instead of a doomed rollback."""
    flow = MigrationRollbackRepairFlow("entry2")
    flow.hass = _make_hass(SimpleNamespace(data={}))

    with (
        patch(
            "custom_components.truenas_ce.repairs.ir.async_delete_issue"
        ) as delete_issue,
        patch(_CODE_CHECK, new=AsyncMock(return_value=False)),
    ):
        result = await flow.async_step_rollback()

    assert result["type"] == "abort"
    assert result["reason"] == "legacy_code_missing"
    flow.hass.async_create_task.assert_not_called()
    delete_issue.assert_called_once_with(
        flow.hass, "truenas_ce", "migration_rollback_available_entry2"
    )


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
            new=AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.truenas_ce.repairs.ir.async_create_issue"
        ) as create_issue,
    ):
        await _async_rollback_and_log_errors(hass, entry)

    create_issue.assert_not_called()


async def test_rollback_task_false_result_raises_visible_issue() -> None:
    """A ``False`` result (nothing torn down) must surface just like a crash.

    ``async_rollback_to_legacy`` returns ``False`` without raising when there
    is nothing to roll back or the legacy entry failed to set up; since the
    ``migration_rollback_available`` issue is already deleted by the caller,
    a silent ``False`` would otherwise leave the user with no feedback at all.
    """
    hass = SimpleNamespace()
    entry = SimpleNamespace(entry_id="entry2", title="TrueNAS")

    with (
        patch(
            "custom_components.truenas_ce.repairs.async_rollback_to_legacy",
            new=AsyncMock(return_value=False),
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


# ---------------------------
#   LegacyEntryOrphanedRepairFlow (#201)
# ---------------------------
_LEGACY = SimpleNamespace(entry_id="legacy-1", title="Old NAS")


def _legacy_flow(legacy: SimpleNamespace | None) -> LegacyEntryOrphanedRepairFlow:
    entry = SimpleNamespace(data={MIGRATION_LEGACY_ENTRY_ID: "legacy-1"})
    flow = LegacyEntryOrphanedRepairFlow("entry3")
    flow.hass = _make_hass(None)
    flow.hass.config_entries.async_get_entry = MagicMock(
        side_effect={"entry3": entry, "legacy-1": legacy}.get
    )
    flow.hass.config_entries.async_remove = AsyncMock()
    return flow


async def test_create_fix_flow_routes_legacy_entry_orphaned_issue() -> None:
    flow = await async_create_fix_flow(
        SimpleNamespace(), "legacy_entry_orphaned_entry3", None
    )
    assert isinstance(flow, LegacyEntryOrphanedRepairFlow)

    flow.hass = _make_hass(None)
    await flow.async_step_init()
    flow.hass.config_entries.async_get_entry.assert_called_once_with("entry3")


async def test_legacy_entry_orphaned_init_shows_confirm_with_title() -> None:
    result = await _legacy_flow(_LEGACY).async_step_init()

    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"title": "Old NAS"}


async def test_legacy_entry_orphaned_init_title_falls_back_when_entry_gone() -> None:
    """{title} must never render unfilled, even if the entry vanished."""
    result = await _legacy_flow(None).async_step_init()

    assert result["description_placeholders"] == {"title": "truenas"}


async def test_legacy_entry_orphaned_confirm_removes_legacy_entry() -> None:
    flow = _legacy_flow(_LEGACY)
    order = MagicMock()
    order.attach_mock(flow.hass.config_entries.async_remove, "remove")
    with patch(
        "custom_components.truenas_ce.repairs.ir.async_delete_issue"
    ) as delete_issue:
        order.attach_mock(delete_issue, "delete_issue")
        result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    assert order.mock_calls == [
        call.remove("legacy-1"),
        call.delete_issue(flow.hass, "truenas_ce", "legacy_entry_orphaned_entry3"),
    ]
    # Call order alone would pass for a never-awaited coroutine too.
    flow.hass.config_entries.async_remove.assert_awaited_once_with("legacy-1")


async def test_legacy_entry_orphaned_confirm_already_removed_only_clears_issue() -> (
    None
):
    """The user deleted the entry by hand meanwhile: nothing left to remove."""
    flow = _legacy_flow(None)
    with patch(
        "custom_components.truenas_ce.repairs.ir.async_delete_issue"
    ) as delete_issue:
        result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    flow.hass.config_entries.async_remove.assert_not_awaited()
    delete_issue.assert_called_once()


async def test_legacy_entry_orphaned_confirm_tolerates_removal_race() -> None:
    """Removed between the check and async_remove: same end state, no error."""
    flow = _legacy_flow(_LEGACY)
    flow.hass.config_entries.async_remove.side_effect = UnknownEntry("legacy-1")
    with patch(
        "custom_components.truenas_ce.repairs.ir.async_delete_issue"
    ) as delete_issue:
        result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    delete_issue.assert_called_once()


async def test_legacy_entry_orphaned_confirm_without_own_entry_finishes() -> None:
    flow = LegacyEntryOrphanedRepairFlow("entry3")
    flow.hass = _make_hass(None)
    flow.hass.config_entries.async_remove = AsyncMock()
    with patch("custom_components.truenas_ce.repairs.ir.async_delete_issue"):
        result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    flow.hass.config_entries.async_remove.assert_not_awaited()


async def test_create_fix_flow_passes_legacy_id_from_issue_data() -> None:
    """The id on the issue survives removal of the TrueNAS CE entry itself."""
    flow = await async_create_fix_flow(
        SimpleNamespace(),
        "legacy_entry_orphaned_entry3",
        {MIGRATION_LEGACY_ENTRY_ID: "legacy-1"},
    )
    flow.hass = _make_hass(None)
    # Own entry gone: only the legacy entry still resolves.
    flow.hass.config_entries.async_get_entry = MagicMock(
        side_effect={"legacy-1": _LEGACY}.get
    )
    flow.hass.config_entries.async_remove = AsyncMock()
    with patch(
        "custom_components.truenas_ce.repairs.ir.async_delete_issue"
    ) as delete_issue:
        result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    flow.hass.config_entries.async_remove.assert_awaited_once_with("legacy-1")
    delete_issue.assert_called_once_with(
        flow.hass, "truenas_ce", "legacy_entry_orphaned_entry3"
    )


@pytest.mark.parametrize(
    ("issue_data", "removed"),
    [
        # The id on the issue wins over the one in the entry's data.
        ({MIGRATION_LEGACY_ENTRY_ID: "legacy-x"}, "legacy-x"),
        # A non-str value on the issue is ignored: fall back to the entry.
        ({MIGRATION_LEGACY_ENTRY_ID: 123}, "legacy-1"),
    ],
)
async def test_create_fix_flow_legacy_id_precedence(
    issue_data: dict[str, object], removed: str
) -> None:
    flow = await async_create_fix_flow(
        SimpleNamespace(), "legacy_entry_orphaned_entry3", issue_data
    )
    entry = SimpleNamespace(data={MIGRATION_LEGACY_ENTRY_ID: "legacy-1"})
    legacy_x = SimpleNamespace(entry_id="legacy-x", title="Other NAS")
    flow.hass = _make_hass(None)
    flow.hass.config_entries.async_get_entry = MagicMock(
        side_effect={"entry3": entry, "legacy-1": _LEGACY, "legacy-x": legacy_x}.get
    )
    flow.hass.config_entries.async_remove = AsyncMock()
    with patch("custom_components.truenas_ce.repairs.ir.async_delete_issue"):
        await flow.async_step_confirm({})

    flow.hass.config_entries.async_remove.assert_awaited_once_with(removed)
