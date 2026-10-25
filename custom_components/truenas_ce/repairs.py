"""Repairs for the TrueNAS integration."""

from __future__ import annotations

from logging import getLogger
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry, UnknownEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

if TYPE_CHECKING:
    # Typing-only: RepairsFlow's steps return RepairsFlowResult (HA 2026.10
    # stubs reject the generic FlowResult). Kept out of the runtime imports so
    # the annotation can never affect loading on the oldest supported release.
    from homeassistant.components.repairs import RepairsFlowResult

from .api import SECRET_SCRUB_FILTER
from .const import (
    CONF_STATISTICS_CLEANUP_IGNORED,
    DOMAIN,
    ISSUE_LEGACY_ENTRY_ORPHANED,
    ISSUE_MIGRATION_ROLLBACK,
    ISSUE_MIGRATION_ROLLBACK_FAILED,
    ISSUE_STATISTICS_ORPHANED,
    LEGACY_DOMAIN,
    MIGRATION_LEGACY_ENTRY_ID,
    MIGRATION_RECORDS,
)
from .coordinator import get_truenas_coordinator
from .helper import async_legacy_code_installed
from .migration import async_rollback_to_legacy

_LOGGER = getLogger(__name__)
_LOGGER.addFilter(SECRET_SCRUB_FILTER)

# Upper bound on ids rendered into the repair dialog. A chain of renames can
# leave dozens behind; the remainder is summarised so the dialog stays readable
# (the full list goes to the debug log during detection).
MAX_LISTED_ORPHANS = 20

# Rollback flow abort reason: the old integration's files are gone (#201).
ABORT_LEGACY_CODE_MISSING = "legacy_code_missing"


def _format_statistic_ids(statistic_ids: list[str]) -> str:
    """Render statistic ids as a Markdown list for the repair dialog."""
    shown = statistic_ids[:MAX_LISTED_ORPHANS]
    lines = [f"- `{statistic_id}`" for statistic_id in shown]
    if remaining := len(statistic_ids) - len(shown):
        # Language-neutral on purpose: placeholder content is not translated.
        lines.append(f"- … (+{remaining})")
    return "\n".join(lines)


class StatisticsCleanupRepairFlow(RepairsFlow):
    """Repair flow for orphaned statistics: delete them or ignore the issue."""

    def __init__(self, entry_id: str) -> None:
        """Remember which config entry this issue belongs to."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Show the fix/ignore menu listing the affected statistic ids.

        Metadata-only orphans (no data points left) get their own wording: they
        cannot be found in Developer Tools → Statistics, so pointing the user
        there would send them looking for something that isn't displayed.
        """
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        coordinator = get_truenas_coordinator(entry)
        if coordinator is None:
            orphans: list[str] = []
            with_data = 0
        else:
            # Already sorted by the coordinator; copied so the dialog keeps the
            # snapshot it renders even if the next poll rebuilds the list.
            orphans = list(coordinator.orphaned_statistics)
            with_data = await coordinator.async_count_orphans_with_data()

        return self.async_show_menu(
            step_id="init" if with_data else "init_metadata_only",
            menu_options=["fix", "ignore"],
            description_placeholders={
                "count": str(len(orphans)),
                "with_data": str(with_data),
                "entities": _format_statistic_ids(orphans),
            },
        )

    async def async_step_fix(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Delete the orphaned statistics."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        coordinator = get_truenas_coordinator(entry)
        if coordinator is not None:
            await coordinator.async_clear_orphaned_statistics()
        return self.async_create_entry(title="", data={})

    async def async_step_ignore(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Suppress the issue for this config entry."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None:
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, CONF_STATISTICS_CLEANUP_IGNORED: True},
            )
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_STATISTICS_ORPHANED}_{self._entry_id}"
        )
        return self.async_create_entry(title="", data={})


async def _async_rollback_and_log_errors(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Run the legacy rollback, surfacing (not raising) any failure.

    Scheduled via ``async_create_task`` because the rollback removes this very
    config entry, which would tear down the repair flow mid-step if awaited
    inline. The ``migration_rollback_available`` issue is already deleted by
    the time this runs (see ``MigrationRollbackRepairFlow.async_step_rollback``),
    so neither a crash nor a "did nothing" result would otherwise have any
    UI-visible signal that the rollback didn't actually happen; raising a
    second, non-fixable issue closes that gap for both cases.

    Migration-rollback-specific: the broad ``except Exception`` is this
    coroutine's whole purpose (turning an untracked task's crash into a
    logged, actionable message), not a generic error-swallowing helper —
    don't reuse it for other background tasks.
    """
    rolled_back = False
    try:
        rolled_back = await async_rollback_to_legacy(hass, entry)
    except Exception:
        _LOGGER.exception(
            "TrueNAS CE migration rollback failed for entry %s (%s)",
            entry.entry_id,
            entry.title,
        )
    if not rolled_back:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{ISSUE_MIGRATION_ROLLBACK_FAILED}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_MIGRATION_ROLLBACK_FAILED,
            translation_placeholders={"name": entry.title},
        )


class MigrationRollbackRepairFlow(RepairsFlow):
    """Confirm-gated rollback of the Community-Edition migration.

    Replaces the old one-tap diagnostic button: the user must open the issue and
    deliberately pick "roll back" (or dismiss it), so an accidental click can no
    longer tear the integration down without confirmation.
    """

    def __init__(self, entry_id: str) -> None:
        """Remember which config entry this issue belongs to."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Show the rollback/dismiss menu with the adopted-entity count."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        count = len(entry.data.get(MIGRATION_RECORDS, [])) if entry else 0
        return self.async_show_menu(
            step_id="init",
            menu_options=["rollback", "ignore"],
            description_placeholders={"count": str(count)},
        )

    async def async_step_rollback(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Hand the adopted entities (and history) back to the legacy entry."""
        issue_id = f"{ISSUE_MIGRATION_ROLLBACK}_{self._entry_id}"
        if not await async_legacy_code_installed(self.hass):
            # Uninstalled only in HACS (#201): a rollback would re-enable an
            # entry that can never load, so say why instead of trying.
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return self.async_abort(reason=ABORT_LEGACY_CODE_MISSING)
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None:
            # Scheduled, not awaited: the rollback removes this config entry,
            # which would tear down this very flow mid-step if awaited inline.
            self.hass.async_create_task(
                _async_rollback_and_log_errors(self.hass, entry)
            )
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        return self.async_create_entry(title="", data={})

    async def async_step_ignore(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Keep TrueNAS CE and close the dialog.

        Only the issue is removed (no permanent suppression): pressing the
        rollback button again re-opens it while the legacy entry still exists.
        """
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_MIGRATION_ROLLBACK}_{self._entry_id}"
        )
        return self.async_create_entry(title="", data={})


class LegacyEntryOrphanedRepairFlow(RepairsFlow):
    """Remove the disabled legacy entry whose integration files are gone (#201).

    Uninstalling the old integration only in HACS leaves its config entry
    behind, and Home Assistant then logs "Unable to get manifest for
    integration truenas". Removing the entry is what the migration guide asks
    for anyway; a rollback is no longer possible without the files.
    """

    def __init__(self, entry_id: str, legacy_id: str | None = None) -> None:
        """Remember the issue's config entry and the legacy entry id it names."""
        self._entry_id = entry_id
        self._legacy_id = legacy_id

    def _resolve_legacy_id(self) -> str | None:
        """Prefer the id stored on the issue; fall back to this entry's data.

        The issue's copy still works when the TrueNAS CE entry was removed after
        the issue was raised (its data is gone with it then).
        """
        if self._legacy_id:
            return self._legacy_id
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        legacy_id = entry.data.get(MIGRATION_LEGACY_ENTRY_ID) if entry else None
        return legacy_id if isinstance(legacy_id, str) else None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Go straight to the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Confirm, then remove the legacy entry."""
        legacy_id = self._resolve_legacy_id()
        legacy_entry = (
            self.hass.config_entries.async_get_entry(legacy_id) if legacy_id else None
        )
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                # Taken from the entry itself, not the issue: the issue may be
                # gone by now, and {title} must never render unfilled.
                description_placeholders={
                    "title": legacy_entry.title if legacy_entry else LEGACY_DOMAIN
                },
            )

        if legacy_entry is None:
            _LOGGER.debug("Legacy TrueNAS entry %s is already gone", legacy_id)
        else:
            try:
                # Another entry than this flow's own, so awaiting it is safe.
                await self.hass.config_entries.async_remove(legacy_entry.entry_id)
            except UnknownEntry:
                _LOGGER.debug("Legacy TrueNAS entry %s already removed", legacy_id)
            else:
                _LOGGER.info("Removed the orphaned legacy TrueNAS entry %s", legacy_id)
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_LEGACY_ENTRY_ORPHANED}_{self._entry_id}"
        )
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the fix flow matching the issue id.

    Each issue id is ``<key>_<entry_id>``; the entry id is parsed back out so the
    flow can act on the right coordinator/config entry.
    """
    if issue_id.startswith(f"{ISSUE_MIGRATION_ROLLBACK}_"):
        entry_id = issue_id.removeprefix(f"{ISSUE_MIGRATION_ROLLBACK}_")
        return MigrationRollbackRepairFlow(entry_id)
    if issue_id.startswith(f"{ISSUE_LEGACY_ENTRY_ORPHANED}_"):
        entry_id = issue_id.removeprefix(f"{ISSUE_LEGACY_ENTRY_ORPHANED}_")
        legacy_id = (data or {}).get(MIGRATION_LEGACY_ENTRY_ID)
        return LegacyEntryOrphanedRepairFlow(
            entry_id, legacy_id if isinstance(legacy_id, str) else None
        )
    entry_id = issue_id.removeprefix(f"{ISSUE_STATISTICS_ORPHANED}_")
    return StatisticsCleanupRepairFlow(entry_id)
