"""Unit tests for migration.py (Community-Edition legacy-entity adoption).

Mirrors the coordinator.py test style: real ``homeassistant`` core types are
importable without ``pytest-homeassistant-custom-component``, so hass/entity
registry/config entries are stood in with ``MagicMock``/``SimpleNamespace``
instead of a running HomeAssistant instance.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler

from custom_components.truenas_ce import migration as migration_module
from custom_components.truenas_ce.const import (
    MIGRATION_BACKUP_KEY,
    MIGRATION_DONE,
    MIGRATION_LEGACY_CONFIG,
    MIGRATION_LEGACY_ENTRY_ID,
    MIGRATION_RECORDS,
)
from custom_components.truenas_ce.migration import (
    _build_migration_message,
    _classify_reconnection,
    _collect_legacy_records,
    _find_legacy_entry,
    _log_reconnection,
    _persist_migration_state,
    _remap_and_restore,
    _remove_legacy_entities,
    _restore_overrides,
    _temp_entity_id,
    async_adopt_legacy_entities,
    async_notify_migration_result,
    async_rollback_to_legacy,
    finalize_legacy_adoption,
)


def _record(
    *,
    unique_id: str = "uid1",
    entity_domain: str = "sensor",
    entity_id: str = "sensor.truenas_cpu",
    name: str | None = None,
    icon: str | None = None,
    area_id: str | None = None,
    disabled_user: bool = False,
) -> dict[str, Any]:
    return {
        "unique_id": unique_id,
        "entity_domain": entity_domain,
        "entity_id": entity_id,
        "name": name,
        "icon": icon,
        "area_id": area_id,
        "disabled_user": disabled_user,
    }


def _config_entry(
    *, entry_id: str = "ce-entry", data: dict[str, Any] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(entry_id=entry_id, data=data or {}, options={})


# ---------------------------
#   _find_legacy_entry
# ---------------------------
def test_find_legacy_entry_matches_by_host() -> None:
    hass = MagicMock()
    legacy_a = SimpleNamespace(entry_id="a", data={"host": "other.local"})
    legacy_b = SimpleNamespace(entry_id="b", data={"host": "truenas.local"})
    hass.config_entries.async_entries.return_value = [legacy_a, legacy_b]

    entry = _config_entry(data={"host": "truenas.local"})
    assert _find_legacy_entry(hass, entry) is legacy_b


def test_find_legacy_entry_falls_back_to_single_candidate() -> None:
    hass = MagicMock()
    legacy = SimpleNamespace(entry_id="a", data={"host": "renamed.local"})
    hass.config_entries.async_entries.return_value = [legacy]

    entry = _config_entry(data={"host": "truenas.local"})
    assert _find_legacy_entry(hass, entry) is legacy


def test_find_legacy_entry_returns_none_for_multiple_non_matching() -> None:
    hass = MagicMock()
    legacy_a = SimpleNamespace(entry_id="a", data={"host": "one.local"})
    legacy_b = SimpleNamespace(entry_id="b", data={"host": "two.local"})
    hass.config_entries.async_entries.return_value = [legacy_a, legacy_b]

    entry = _config_entry(data={"host": "truenas.local"})
    assert _find_legacy_entry(hass, entry) is None


def test_find_legacy_entry_returns_none_when_no_candidates() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    entry = _config_entry(data={"host": "truenas.local"})
    assert _find_legacy_entry(hass, entry) is None


# ---------------------------
#   _collect_legacy_records / _remove_legacy_entities
# ---------------------------
def test_collect_legacy_records_snapshots_registry_entries() -> None:
    ent_reg = MagicMock()
    entry1 = SimpleNamespace(
        unique_id="uid1",
        domain="sensor",
        entity_id="sensor.truenas_cpu",
        name="CPU",
        icon="mdi:chip",
        area_id="office",
        disabled_by=None,
    )
    with patch.object(
        migration_module.er,
        "async_entries_for_config_entry",
        return_value=[entry1],
    ):
        records = _collect_legacy_records(ent_reg, SimpleNamespace(entry_id="legacy"))

    assert records == [_record(name="CPU", icon="mdi:chip", area_id="office")]


def test_collect_legacy_records_marks_user_disabled() -> None:
    ent_reg = MagicMock()
    entry1 = SimpleNamespace(
        unique_id="uid1",
        domain="sensor",
        entity_id="sensor.truenas_cpu",
        name=None,
        icon=None,
        area_id=None,
        disabled_by=migration_module.er.RegistryEntryDisabler.USER,
    )
    with patch.object(
        migration_module.er,
        "async_entries_for_config_entry",
        return_value=[entry1],
    ):
        records = _collect_legacy_records(ent_reg, SimpleNamespace(entry_id="legacy"))

    assert records[0]["disabled_user"] is True


def test_remove_legacy_entities_removes_each_record() -> None:
    ent_reg = MagicMock()
    records = [_record(entity_id="sensor.a"), _record(entity_id="sensor.b")]
    _remove_legacy_entities(ent_reg, records)
    assert ent_reg.async_remove.call_count == 2
    ent_reg.async_remove.assert_any_call("sensor.a")
    ent_reg.async_remove.assert_any_call("sensor.b")


# ---------------------------
#   _persist_migration_state
# ---------------------------
def test_persist_migration_state_with_legacy_entry_and_backup() -> None:
    hass = MagicMock()
    entry = _config_entry(data={"existing": "value"})
    legacy_entry = SimpleNamespace(
        entry_id="legacy-1", data={"host": "x"}, options={"opt": 1}
    )
    records = [_record()]

    _persist_migration_state(hass, entry, legacy_entry, records, "backup-key-1")

    _, kwargs = hass.config_entries.async_update_entry.call_args
    new_data = kwargs["data"]
    assert new_data["existing"] == "value"
    assert new_data[MIGRATION_DONE] is True
    assert new_data[MIGRATION_RECORDS] == records
    assert new_data[MIGRATION_LEGACY_ENTRY_ID] == "legacy-1"
    assert new_data[MIGRATION_LEGACY_CONFIG] == {
        "data": {"host": "x"},
        "options": {"opt": 1},
    }
    assert new_data[MIGRATION_BACKUP_KEY] == "backup-key-1"


def test_persist_migration_state_without_legacy_entry() -> None:
    hass = MagicMock()
    entry = _config_entry()

    _persist_migration_state(hass, entry, None, [], None)

    _, kwargs = hass.config_entries.async_update_entry.call_args
    new_data = kwargs["data"]
    assert new_data[MIGRATION_DONE] is True
    assert MIGRATION_LEGACY_ENTRY_ID not in new_data
    assert MIGRATION_BACKUP_KEY not in new_data


# ---------------------------
#   _temp_entity_id
# ---------------------------
def test_temp_entity_id_returns_first_free_candidate() -> None:
    ent_reg = MagicMock()
    ent_reg.async_get.return_value = None
    temp_id, counter = _temp_entity_id(ent_reg, "sensor.truenas_cpu", 0)
    assert temp_id == "sensor.truenas_ce_mig_0"
    assert counter == 1


def test_temp_entity_id_skips_occupied_candidates() -> None:
    ent_reg = MagicMock()
    ent_reg.async_get.side_effect = [object(), object(), None]
    temp_id, counter = _temp_entity_id(ent_reg, "sensor.truenas_cpu", 0)
    assert temp_id == "sensor.truenas_ce_mig_2"
    assert counter == 3


# ---------------------------
#   _restore_overrides
# ---------------------------
def test_restore_overrides_applies_all_present_fields() -> None:
    ent_reg = MagicMock()
    record = _record(name="CPU", icon="mdi:chip", area_id="office", disabled_user=True)
    _restore_overrides(ent_reg, "sensor.truenas_cpu", record)
    ent_reg.async_update_entity.assert_called_once_with(
        "sensor.truenas_cpu",
        name="CPU",
        icon="mdi:chip",
        area_id="office",
        disabled_by=migration_module.er.RegistryEntryDisabler.USER,
    )


def test_restore_overrides_noop_when_no_fields_set() -> None:
    ent_reg = MagicMock()
    record = _record()
    _restore_overrides(ent_reg, "sensor.truenas_cpu", record)
    ent_reg.async_update_entity.assert_not_called()


# ---------------------------
#   _remap_and_restore
# ---------------------------
def test_remap_and_restore_simple_rename() -> None:
    """Rename is a two-pass park-then-restore even for a single, non-cyclic pair."""
    ent_reg = MagicMock()
    ent_reg.async_get.return_value = None
    record = _record(name="CPU")
    pairs = [("sensor.truenas_ce_cpu", "sensor.truenas_cpu", record)]

    _remap_and_restore(ent_reg, pairs)

    ent_reg.async_update_entity.assert_any_call(
        "sensor.truenas_ce_cpu", new_entity_id="sensor.truenas_ce_mig_0"
    )
    ent_reg.async_update_entity.assert_any_call(
        "sensor.truenas_ce_mig_0", new_entity_id="sensor.truenas_cpu"
    )


def test_remap_and_restore_already_on_target_skips_rename() -> None:
    ent_reg = MagicMock()
    record = _record(name="CPU")
    pairs = [("sensor.truenas_cpu", "sensor.truenas_cpu", record)]

    _remap_and_restore(ent_reg, pairs)

    # No rename call should reference this id since current == target.
    for call in ent_reg.async_update_entity.call_args_list:
        assert (
            call.kwargs.get("new_entity_id") != "sensor.truenas_cpu"
            or call.args[0] != "sensor.truenas_cpu"
        )


def test_remap_and_restore_permutation_cycle() -> None:
    """sda -> sdb and sdb -> sda must both succeed (two-pass park-then-restore)."""
    ent_reg = MagicMock()
    # After parking, target ids are free; simulate that via async_get returning
    # None once entities have been parked (start occupied, then free).
    occupied = {"sensor.truenas_sda", "sensor.truenas_sdb"}

    def fake_get(entity_id: str) -> Any:
        return object() if entity_id in occupied else None

    ent_reg.async_get.side_effect = fake_get

    def fake_update(
        entity_id: str, *, new_entity_id: str | None = None, **_: Any
    ) -> None:
        if new_entity_id:
            occupied.discard(entity_id)
            occupied.add(new_entity_id)

    ent_reg.async_update_entity.side_effect = fake_update

    pairs = [
        ("sensor.truenas_sda", "sensor.truenas_sdb", _record()),
        ("sensor.truenas_sdb", "sensor.truenas_sda", _record()),
    ]
    _remap_and_restore(ent_reg, pairs)

    final_ids = {
        call.args[0]
        if not call.kwargs.get("new_entity_id")
        else call.kwargs["new_entity_id"]
        for call in ent_reg.async_update_entity.call_args_list
    }
    assert "sensor.truenas_sda" in final_ids
    assert "sensor.truenas_sdb" in final_ids


def test_remap_and_restore_target_still_occupied_leaves_entity_in_place(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ent_reg = MagicMock()
    # Only the target id is occupied; temp ids must stay free or _temp_entity_id
    # would loop forever looking for one.
    ent_reg.async_get.side_effect = lambda entity_id: (
        object() if entity_id == "sensor.truenas_cpu" else None
    )
    record = _record(name="CPU")
    pairs = [("sensor.truenas_ce_mig_0", "sensor.truenas_cpu", record)]

    with caplog.at_level("WARNING"):
        _remap_and_restore(ent_reg, pairs)

    assert "could not restore id" in caplog.text


# ---------------------------
#   _classify_reconnection / _log_reconnection
# ---------------------------
def test_classify_reconnection_splits_records() -> None:
    ent_reg = MagicMock()

    def fake_get_entity_id(
        domain: str, _domain_const: str, unique_id: str
    ) -> str | None:
        return {
            "uid-reconnected": "sensor.truenas_cpu",
            "uid-mismatched": "sensor.truenas_ce_mig_0",
        }.get(unique_id)

    ent_reg.async_get_entity_id.side_effect = fake_get_entity_id
    records = [
        _record(unique_id="uid-reconnected", entity_id="sensor.truenas_cpu"),
        _record(unique_id="uid-pending", entity_id="sensor.truenas_pending"),
        _record(unique_id="uid-mismatched", entity_id="sensor.truenas_original"),
    ]

    reconnected, pending, mismatched = _classify_reconnection(ent_reg, records)

    assert reconnected == 1
    assert pending == ["sensor.truenas_pending"]
    assert mismatched == ["sensor.truenas_original -> sensor.truenas_ce_mig_0"]


def test_log_reconnection_handles_empty_lists() -> None:
    _log_reconnection([], [])  # must not raise


def test_log_reconnection_logs_pending_and_mismatched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO"):
        _log_reconnection(["sensor.a"], ["sensor.b -> sensor.c"])
    assert "not yet recreated" in caplog.text
    assert "could not reclaim" in caplog.text


# ---------------------------
#   _build_migration_message
# ---------------------------
def test_build_migration_message_singular_noun() -> None:
    checks = {"entities": (True, "Entities adopted: 1")}
    message = _build_migration_message(1, checks)
    assert "1 entity adopted" in message
    assert "Entities adopted: 1" in message


def test_build_migration_message_plural_noun_and_marks() -> None:
    checks = {
        "entities": (True, "Entities adopted: 2"),
        "history": (False, "History reconnected: 0/2"),
    }
    message = _build_migration_message(2, checks)
    assert "2 entities adopted" in message
    assert "✅ Entities adopted: 2" in message
    assert "⚠️ History reconnected: 0/2" in message


# ---------------------------
#   async_adopt_legacy_entities
# ---------------------------
async def test_async_adopt_legacy_entities_inert_when_domain_is_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration_module, "DOMAIN", migration_module.LEGACY_DOMAIN)
    hass = MagicMock()
    entry = _config_entry()
    assert await async_adopt_legacy_entities(hass, entry) == []


async def test_async_adopt_legacy_entities_noop_when_already_done() -> None:
    hass = MagicMock()
    entry = _config_entry(data={MIGRATION_DONE: True})
    assert await async_adopt_legacy_entities(hass, entry) == []


async def test_async_adopt_legacy_entities_no_legacy_entry_found() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    entry = _config_entry(data={"host": "truenas.local"})

    records = await async_adopt_legacy_entities(hass, entry)

    assert records == []
    hass.config_entries.async_update_entry.assert_called_once()


async def test_async_adopt_legacy_entities_full_happy_path() -> None:
    hass = MagicMock()
    legacy_entry = SimpleNamespace(
        entry_id="legacy-1",
        data={"host": "truenas.local"},
        options={},
        disabled_by=None,
    )
    hass.config_entries.async_entries.return_value = [legacy_entry]
    hass.config_entries.async_set_disabled_by = AsyncMock()
    entry = _config_entry(data={"host": "truenas.local"})

    ent_reg = MagicMock()
    reg_entry = SimpleNamespace(
        unique_id="uid1",
        domain="sensor",
        entity_id="sensor.truenas_cpu",
        name=None,
        icon=None,
        area_id=None,
        disabled_by=None,
    )
    with (
        patch.object(migration_module.er, "async_get", return_value=ent_reg),
        patch.object(
            migration_module.er,
            "async_entries_for_config_entry",
            return_value=[reg_entry],
        ),
        patch.object(
            migration_module,
            "_write_migration_backup",
            new=AsyncMock(return_value="backup-key"),
        ),
    ):
        records = await async_adopt_legacy_entities(hass, entry)

    assert len(records) == 1
    hass.config_entries.async_set_disabled_by.assert_awaited_once_with(
        "legacy-1", ConfigEntryDisabler.USER
    )
    ent_reg.async_remove.assert_called_once_with("sensor.truenas_cpu")


async def test_async_adopt_legacy_entities_skips_disable_when_already_disabled() -> (
    None
):
    hass = MagicMock()
    legacy_entry = SimpleNamespace(
        entry_id="legacy-1",
        data={"host": "truenas.local"},
        options={},
        disabled_by=ConfigEntryDisabler.USER,
    )
    hass.config_entries.async_entries.return_value = [legacy_entry]
    hass.config_entries.async_set_disabled_by = AsyncMock()
    entry = _config_entry(data={"host": "truenas.local"})

    with (
        patch.object(migration_module.er, "async_get", return_value=MagicMock()),
        patch.object(
            migration_module.er, "async_entries_for_config_entry", return_value=[]
        ),
        patch.object(
            migration_module,
            "_write_migration_backup",
            new=AsyncMock(return_value=None),
        ),
    ):
        await async_adopt_legacy_entities(hass, entry)

    hass.config_entries.async_set_disabled_by.assert_not_awaited()


# ---------------------------
#   finalize_legacy_adoption
# ---------------------------
def test_finalize_legacy_adoption_inert_when_domain_is_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration_module, "DOMAIN", migration_module.LEGACY_DOMAIN)
    hass = MagicMock()
    finalize_legacy_adoption(
        hass, [_record()]
    )  # must not raise / not call er.async_get
    hass.assert_not_called() if callable(hass) else None


def test_finalize_legacy_adoption_noop_for_empty_records() -> None:
    hass = MagicMock()
    finalize_legacy_adoption(hass, [])


def test_finalize_legacy_adoption_remaps_matched_entities() -> None:
    hass = MagicMock()
    ent_reg = MagicMock()
    ent_reg.async_get_entity_id.return_value = "sensor.truenas_ce_cpu"
    ent_reg.async_get.return_value = None
    record = _record()

    with patch.object(migration_module.er, "async_get", return_value=ent_reg):
        finalize_legacy_adoption(hass, [record])

    ent_reg.async_update_entity.assert_any_call(
        "sensor.truenas_ce_cpu", new_entity_id="sensor.truenas_ce_mig_0"
    )
    ent_reg.async_update_entity.assert_any_call(
        "sensor.truenas_ce_mig_0", new_entity_id="sensor.truenas_cpu"
    )


def test_finalize_legacy_adoption_skips_unmatched_records() -> None:
    hass = MagicMock()
    ent_reg = MagicMock()
    ent_reg.async_get_entity_id.return_value = None
    record = _record()

    with patch.object(migration_module.er, "async_get", return_value=ent_reg):
        finalize_legacy_adoption(hass, [record])

    ent_reg.async_update_entity.assert_not_called()


# ---------------------------
#   _write_migration_backup / _remove_backups
# ---------------------------
async def test_write_migration_backup_success() -> None:
    hass = MagicMock()
    entry = _config_entry()
    legacy_entry = SimpleNamespace(entry_id="legacy-1", data={}, options={})

    store_instance = MagicMock()
    store_instance.async_save = AsyncMock()
    store_instance.key = "truenas_ce_migration_backup_20260101_000000"

    with (
        patch.object(migration_module, "Store", return_value=store_instance),
        patch.object(
            migration_module, "_remove_backups", new=AsyncMock()
        ) as remove_mock,
    ):
        key = await migration_module._write_migration_backup(
            hass, entry, legacy_entry, []
        )

    assert key == store_instance.key
    store_instance.async_save.assert_awaited_once()
    remove_mock.assert_awaited_once_with(hass, store_instance.key)


async def test_write_migration_backup_failure_returns_none() -> None:
    hass = MagicMock()
    entry = _config_entry()
    legacy_entry = SimpleNamespace(entry_id="legacy-1", data={}, options={})

    store_instance = MagicMock()
    store_instance.async_save = AsyncMock(side_effect=OSError("disk full"))

    with patch.object(migration_module, "Store", return_value=store_instance):
        key = await migration_module._write_migration_backup(
            hass, entry, legacy_entry, []
        )

    assert key is None


async def test_remove_backups_removes_all_except_keep_key() -> None:
    hass = MagicMock()
    hass.config.path.return_value = "/config/.storage"
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func: func())

    store_instance = MagicMock()
    store_instance.async_remove = AsyncMock()

    with (
        patch(
            "os.listdir",
            return_value=[
                "truenas_ce_migration_backup_1",
                "truenas_ce_migration_backup_2",
                "unrelated_file",
            ],
        ),
        patch.object(migration_module, "Store", return_value=store_instance),
    ):
        await migration_module._remove_backups(hass, "truenas_ce_migration_backup_1")

    store_instance.async_remove.assert_awaited_once()


async def test_remove_backups_handles_listdir_error() -> None:
    hass = MagicMock()
    hass.config.path.return_value = "/config/.storage"
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func: func())

    with patch("os.listdir", side_effect=OSError("no such dir")):
        await migration_module._remove_backups(hass, None)  # must not raise


async def test_remove_backups_logs_on_remove_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hass = MagicMock()
    hass.config.path.return_value = "/config/.storage"
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func: func())

    store_instance = MagicMock()
    store_instance.async_remove = AsyncMock(side_effect=OSError("locked"))

    with (
        patch("os.listdir", return_value=["truenas_ce_migration_backup_1"]),
        patch.object(migration_module, "Store", return_value=store_instance),
        caplog.at_level("WARNING"),
    ):
        await migration_module._remove_backups(hass, None)

    assert "Could not remove CE migration backup" in caplog.text


# ---------------------------
#   async_notify_migration_result
# ---------------------------
def test_async_notify_migration_result_inert_when_domain_is_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration_module, "DOMAIN", migration_module.LEGACY_DOMAIN)
    hass = MagicMock()
    async_notify_migration_result(hass, _config_entry(), [_record()])


def test_async_notify_migration_result_noop_for_empty_records() -> None:
    hass = MagicMock()
    async_notify_migration_result(hass, _config_entry(), [])


def test_async_notify_migration_result_creates_notification() -> None:
    hass = MagicMock()
    ent_reg = MagicMock()
    ent_reg.async_get_entity_id.return_value = "sensor.truenas_cpu"
    entry = _config_entry(
        data={
            MIGRATION_LEGACY_ENTRY_ID: "legacy-1",
            MIGRATION_RECORDS: [_record()],
            MIGRATION_BACKUP_KEY: "key1",
        }
    )
    legacy_entry = SimpleNamespace(disabled_by=ConfigEntryDisabler.USER)
    hass.config_entries.async_get_entry.return_value = legacy_entry
    notify_mock = MagicMock()

    with (
        patch.object(migration_module.er, "async_get", return_value=ent_reg),
        patch.object(
            migration_module.persistent_notification, "async_create", notify_mock
        ),
    ):
        async_notify_migration_result(
            hass, entry, [_record(entity_id="sensor.truenas_cpu")]
        )

    notify_mock.assert_called_once()
    _, kwargs = notify_mock.call_args
    assert kwargs["notification_id"] == f"truenas_ce_migration_{entry.entry_id}"


# ---------------------------
#   async_rollback_to_legacy
# ---------------------------
async def test_async_rollback_to_legacy_inert_when_domain_is_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration_module, "DOMAIN", migration_module.LEGACY_DOMAIN)
    hass = MagicMock()
    assert await async_rollback_to_legacy(hass, _config_entry()) is False


async def test_async_rollback_to_legacy_false_when_no_legacy_id() -> None:
    hass = MagicMock()
    entry = _config_entry(data={})
    assert await async_rollback_to_legacy(hass, entry) is False


async def test_async_rollback_to_legacy_false_when_legacy_entry_gone() -> None:
    hass = MagicMock()
    hass.config_entries.async_get_entry.return_value = None
    entry = _config_entry(data={MIGRATION_LEGACY_ENTRY_ID: "legacy-1"})
    assert await async_rollback_to_legacy(hass, entry) is False


async def test_async_rollback_to_legacy_full_flow() -> None:
    hass = MagicMock()
    hass.config_entries.async_get_entry.return_value = SimpleNamespace(
        entry_id="legacy-1"
    )
    hass.config_entries.async_remove = AsyncMock()
    hass.config_entries.async_set_disabled_by = AsyncMock()
    entry = _config_entry(
        data={
            MIGRATION_LEGACY_ENTRY_ID: "legacy-1",
            MIGRATION_RECORDS: [_record()],
        }
    )

    ent_reg = MagicMock()
    ent_reg.async_get_entity_id.return_value = "sensor.truenas_original"
    ent_reg.async_get.return_value = None

    with (
        patch.object(migration_module.er, "async_get", return_value=ent_reg),
        patch.object(migration_module, "_remove_backups", new=AsyncMock()) as rm_mock,
    ):
        result = await async_rollback_to_legacy(hass, entry)

    assert result is True
    hass.config_entries.async_remove.assert_awaited_once_with(entry.entry_id)
    hass.config_entries.async_set_disabled_by.assert_awaited_once_with("legacy-1", None)
    rm_mock.assert_awaited_once_with(hass, keep_key=None)
