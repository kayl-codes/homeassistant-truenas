"""TrueNAS button platform."""

from __future__ import annotations

from logging import getLogger

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SECRET_SCRUB_FILTER
from .button_types import (  # noqa: F401
    SENSOR_SERVICES,
    SENSOR_TYPES,
    TrueNASButtonEntityDescription,
)
from .const import (
    API_POOL_SCRUB_SCRUB,
    BUTTON_MIGRATION_ROLLBACK,
    BUTTON_STATISTICS_CLEANUP,
    BUTTON_SYSTEM_REFRESH,
    DOMAIN,
    LEGACY_DOMAIN,
    MIGRATION_LEGACY_ENTRY_ID,
    SCAN_FUNCTION_SCRUB,
    SCRUB_ACTION_START,
)
from .coordinator import TrueNASCoordinator, get_truenas_coordinator
from .entity import (
    TrueNASEntity,
    async_add_entities,
    format_device_identifier,
    format_unique_id,
    resolve_entry_identity,
)
from .helper import pool_scrub_state

_LOGGER = getLogger(__name__)
_LOGGER.addFilter(SECRET_SCRUB_FILTER)

# Updates are centralized in the coordinator; entity actions may run unlimited.
PARALLEL_UPDATES = 0


# ---------------------------
#   async_setup_entry
# ---------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    _async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TrueNAS buttons."""
    dispatcher = {
        "TrueNASButton": TrueNASButton,
        "TrueNASPoolScrubButton": TrueNASPoolScrubButton,
    }
    await async_add_entities(hass, config_entry, dispatcher)

    # The orphaned-statistics cleanup button is a single diagnostic entity per
    # config entry, not tied to a TrueNAS object, so it is added directly.
    coordinator: TrueNASCoordinator | None = get_truenas_coordinator(config_entry)
    if coordinator is None:
        return
    _async_add_entities([TrueNASStatisticsCleanupButton(coordinator)])

    # On-demand data refresh: a diagnostic button that forces an immediate
    # coordinator re-poll (mirrors the system_refresh action). One per entry.
    _async_add_entities([TrueNASRefreshButton(coordinator)])

    # Migration rollback: a safe diagnostic button that only opens the Repairs
    # confirm dialog (it never rolls back directly). Created only after a
    # Community-Edition migration adopted a legacy entry — never on the original
    # "truenas" integration.
    if DOMAIN != LEGACY_DOMAIN and config_entry.data.get(MIGRATION_LEGACY_ENTRY_ID):
        _async_add_entities([TrueNASMigrationRollbackButton(coordinator)])


# ---------------------------
#   TrueNASButton
# ---------------------------
class TrueNASButton(TrueNASEntity, ButtonEntity):
    """Define a TrueNAS run button (one-tap trigger for an on-demand task)."""

    entity_description: TrueNASButtonEntityDescription

    async def async_press(self) -> None:
        """Trigger the configured JSON-RPC method for this object."""
        method = self.entity_description.api_method
        data_path = self.entity_description.data_path
        object_id = self._data.get("id")
        if not method or not data_path or object_id is None:
            _LOGGER.warning(
                "TrueNAS button %s has no api_method or object id; skipping",
                self.entity_id,
            )
            return

        # Trigger the run and show RUNNING immediately (re-synced on next poll).
        await self.coordinator.async_run_task(method, object_id, data_path)


# ---------------------------
#   TrueNASPoolScrubButton
# ---------------------------
class TrueNASPoolScrubButton(TrueNASButton):
    """Scrub button: sends a pool.scrub.scrub action (start/pause/resume/stop).

    pool.scrub.scrub acts on the pool by name, so it runs whether or not the
    scheduled scrub task is enabled -- a disabled schedule only stops TrueNAS
    from starting scrubs on its own, not a manual one (#203).

    Pause, resume and stop (#204) first check the pool's current scrub state:
    resume is START again, which on a pool without a paused scrub would
    silently begin a new one.
    """

    async def async_press(self) -> None:
        """Send this button's scrub action for the task's pool."""
        pool_name = self._data.get("pool_name")
        task_id = self._data.get("id")
        if not pool_name or task_id is None:
            _LOGGER.debug(
                "TrueNAS scrub button %s missing pool_name or task id; skipping",
                self.entity_id,
            )
            return
        action = self.entity_description.scrub_action
        required_states = self.entity_description.scrub_states
        if required_states:
            # Check the current state, not the last poll's (see
            # coordinator.async_refresh_pools).
            await self.coordinator.async_refresh_pools()
            if self._scrub_state(pool_name) not in required_states:
                # One message per button, e.g. "scrub_pause_not_possible".
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key=f"{self.entity_description.key}_not_possible",
                    translation_placeholders={"pool": pool_name},
                )
        result = await self.coordinator.api.query(
            API_POOL_SCRUB_SCRUB, [pool_name, action]
        )
        if result is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="scrub_failed",
                translation_placeholders={
                    "pool": pool_name,
                    "host": self.coordinator.host,
                    "error": str(self.coordinator.api.error or "unknown error"),
                },
            )
        _LOGGER.debug(
            "TrueNAS scrub button %s: sent %s for pool %r",
            self.entity_id,
            action,
            pool_name,
        )
        # Only "Run scrub" marks the task RUNNING; resume is a START too, but
        # continues the paused scrub rather than starting a run.
        if action == SCRUB_ACTION_START and not required_states:
            self.coordinator.set_optimistic_running(
                self.entity_description.data_path or "", task_id
            )
        # Re-read the pool so the scrub state and progress follow right away.
        await self.coordinator.async_refresh_pools()

    def _scrub_state(self, pool_name: str) -> str | None:
        """Return the pool's scrub state, or None if no scrub is on record.

        A resilver also reports SCANNING but cannot be paused or stopped
        through pool.scrub.scrub, so only a scan of function SCRUB counts.
        """
        pools = self.coordinator.data.get("pool")
        if not isinstance(pools, dict):
            return None
        pool = next(
            (
                pool
                for pool in pools.values()
                if isinstance(pool, dict) and pool.get("name") == pool_name
            ),
            None,
        )
        if pool is None or pool.get("scan_function") != SCAN_FUNCTION_SCRUB:
            return None
        return pool_scrub_state(pool)


# ---------------------------
#   TrueNASStatisticsCleanupButton
# ---------------------------
class TrueNASStatisticsCleanupButton(
    CoordinatorEntity[TrueNASCoordinator], ButtonEntity
):
    """Diagnostic button to delete orphaned recorder statistics.

    Available only while orphans are actually present — independent of whether
    the Repairs issue has been ignored.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = BUTTON_STATISTICS_CLEANUP

    def __init__(self, coordinator: TrueNASCoordinator) -> None:
        """Initialize the cleanup button."""
        super().__init__(coordinator)
        identity = resolve_entry_identity(coordinator.config_entry)
        self._attr_unique_id = format_unique_id(identity, BUTTON_STATISTICS_CLEANUP)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, format_device_identifier(identity))},
        )

    @property
    def available(self) -> bool:
        """Available only while orphaned statistics exist."""
        return bool(self.coordinator.orphaned_statistics)

    async def async_press(self) -> None:
        """Clear the orphaned statistics."""
        await self.coordinator.async_clear_orphaned_statistics()


# ---------------------------
#   TrueNASMigrationRollbackButton
# ---------------------------
class TrueNASMigrationRollbackButton(
    CoordinatorEntity[TrueNASCoordinator], ButtonEntity
):
    """Diagnostic button that opens the Community-Edition rollback confirmation.

    It is intentionally non-destructive: pressing it only raises the rollback
    Repairs issue (a confirm dialog), so an accidental press can never tear the
    integration down — the actual rollback happens only after the user confirms
    in Repairs. Available, and the issue re-raisable, only while the disabled
    legacy "truenas" entry and the old integration's files still exist.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = BUTTON_MIGRATION_ROLLBACK

    def __init__(self, coordinator: TrueNASCoordinator) -> None:
        """Initialize the rollback button."""
        super().__init__(coordinator)
        identity = resolve_entry_identity(coordinator.config_entry)
        self._attr_unique_id = format_unique_id(identity, BUTTON_MIGRATION_ROLLBACK)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, format_device_identifier(identity))},
        )

    @property
    def available(self) -> bool:
        """Available only while a rollback to the legacy entry is still possible."""
        return self.coordinator.rollback_possible()

    async def async_press(self) -> None:
        """Open the rollback confirmation by raising its Repairs issue."""
        self.coordinator.raise_migration_rollback_issue()


# ---------------------------
#   TrueNASRefreshButton
# ---------------------------
class TrueNASRefreshButton(CoordinatorEntity[TrueNASCoordinator], ButtonEntity):
    """Diagnostic button to force an immediate coordinator re-poll of TrueNAS.

    Mirrors the ``system_refresh`` action: it triggers the same update cycle that
    otherwise runs on the poll interval, so the data can be refreshed on demand
    from the UI without waiting for the next poll.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = BUTTON_SYSTEM_REFRESH

    def __init__(self, coordinator: TrueNASCoordinator) -> None:
        """Initialize the refresh button."""
        super().__init__(coordinator)
        identity = resolve_entry_identity(coordinator.config_entry)
        self._attr_unique_id = format_unique_id(identity, BUTTON_SYSTEM_REFRESH)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, format_device_identifier(identity))},
        )

    async def async_press(self) -> None:
        """Force an immediate coordinator re-poll."""
        await self.coordinator.async_refresh()
