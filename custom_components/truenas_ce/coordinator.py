"""TrueNAS Controller."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Hashable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiotruenas import TrueNASState
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    list_statistic_ids,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_NAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .api import TrueNASAPI, _summarize_payload
from .apiparser import ApiValueSpec, parse_api
from .const import (
    APP_UPDATE_JOB_ACTIVE_STATES,
    BEHAVIOR_SKIP_DISABLED_CRONJOBS,
    CONF_BEHAVIORS,
    CONF_MONITORED_GROUPS,
    CONF_POLL_INTERVAL,
    CONF_STATISTICS_CLEANUP_IGNORED,
    DEFAULT_MONITORED_GROUPS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    ERR_INVALID_KEY,
    ISSUE_MIGRATION_ROLLBACK,
    ISSUE_STATISTICS_ORPHANED,
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
from .event_push import SubscriptionCircuitBreaker, SubscriptionPushConsumer

_LOGGER = logging.getLogger(__name__)

# Job-progress fields shared by the cloudsync, replication and rsync queries.
_JOB_PROGRESS_VALS: list[ApiValueSpec] = [
    {
        "name": "time_started",
        "source": "job/time_started/$date",
        "default": 0,
        "convert": "utc_from_timestamp",
    },
    {
        "name": "time_finished",
        "source": "job/time_finished/$date",
        "default": 0,
        "convert": "utc_from_timestamp",
    },
    {"name": "job_percent", "source": "job/progress/percent", "default": 0},
    {
        "name": "job_description",
        "source": "job/progress/description",
        "default": "unknown",
    },
]

# Cloudsync and rsync report their status via the last job (job/state).
# Replication has its own persistent ``state`` object (``state/state``) — the
# value shown in the TrueNAS WebUI — and overrides this (see get_replication, #34).
_JOB_STATUS_VALS: list[ApiValueSpec] = [
    {"name": "state", "source": "job/state", "default": "unknown"},
    *_JOB_PROGRESS_VALS,
]

# Certificate expiry monitoring (certificate.query).
_CERTIFICATE_VALS: list[ApiValueSpec] = [
    {"name": "id", "default": 0},
    {"name": "name", "default": "unknown"},
    {"name": "identity", "source": "_identity", "default": "unknown"},
    {"name": "cert_type", "default": "unknown"},
    {"name": "common", "default": ""},
    {
        "name": "until",
        "default": None,
        "convert": "human_date_to_utc",
    },
    {"name": "expired", "type": "bool", "default": False},
    {"name": "renew_days", "default": 0},
]


def _assign_certificate_identities(
    certificates: list[Any], poisoned_commons: set[str]
) -> None:
    """Set each raw certificate entry's ``_identity`` key in place.

    Uses ``common`` when it is both non-empty and has never collided across
    a poll, else falls back to the always-unique ``name`` -- see
    ``get_certificates`` for why a shared ``common`` cannot be used as-is.

    A common shared by more than one certificate in this poll is added to
    ``poisoned_commons`` (mutated in place) and stays poisoned on every
    later poll, even once the collision disappears (e.g. one of the
    certificates is deleted). Without that persistence, the surviving
    certificate's identity would flip from ``name`` back to ``common`` as
    soon as it becomes unique again, changing its unique_id and orphaning
    its own recorder statistics -- the exact failure this migration exists
    to prevent (Sourcery finding on an earlier version of this fix).
    """
    common_counts: dict[str, int] = {}
    for cert in certificates:
        if isinstance(cert, dict) and cert.get("common"):
            common_counts[cert["common"]] = common_counts.get(cert["common"], 0) + 1
    poisoned_commons.update(
        common for common, count in common_counts.items() if count > 1
    )
    for cert in certificates:
        if not isinstance(cert, dict):
            continue
        common = cert.get("common")
        cert["_identity"] = (
            common if common and common not in poisoned_commons else cert.get("name")
        )


# ---------------------------
#   misc helpers
# ---------------------------
def _as_str_keyed(data: dict[Hashable, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Convert a TrueNASState endpoint map's uid-typed keys to str for self.ds.

    ``TrueNASState`` types object ids as ``Hashable`` (some uids, e.g. cronjob
    ids, are ints at the API level); ``self.ds`` has always been str-keyed
    end to end here, so convert at the boundary rather than widening self.ds's
    declared type for every not-yet-migrated endpoint.
    """
    return {str(uid): values for uid, values in data.items()}


def _is_truenas_sensor_id(statistic_id: str, device_slug: str) -> bool:
    """Return True if a recorder statistic_id looks like *this entry's* sensor.

    Entity ids vary across versions and instance names (``sensor.truenas_...``,
    ``sensor.system_truenas_...`` and custom names whose slug merges the domain
    into a longer token, e.g. ``sensor.truenasviacfnoauth_...``). Match the
    per-entry device-name slug as a substring of the id's remainder after
    ``sensor.`` rather than as an exact token or fixed prefix, so every
    orphaned variant is caught.

    ``device_slug`` must be ``slugify(config_entry.data[CONF_NAME])`` for the
    entry doing the detection, not a fixed constant: entity ids are slugged
    from the *device* name, which is user-chosen per entry (e.g. "TrueNAS
    nuc13" vs "TrueNAS x11dpu" to tell multiple instances apart). Matching a
    single global slug instead used to make every entry's coordinator flag
    every *other* entry's orphaned statistics too, since all of them contain
    the same "truenas" substring -- producing one duplicate Repairs issue per
    config entry for the same global orphan list on multi-entry installs
    (#61). An empty ``device_slug`` (e.g. a blank device name) is rejected
    outright, since an empty string would otherwise match every id.
    """
    if not device_slug or not statistic_id.startswith("sensor."):
        return False
    return device_slug in statistic_id[len("sensor.") :]


def _count_statistics_with_data(hass: HomeAssistant, statistic_ids: list[str]) -> int:
    """Return how many of the given statistic_ids still hold data points.

    Runs inside the recorder executor: one indexed ``LIMIT 1`` lookup per id, so
    the cost stays flat even for a large orphan backlog. The requested column
    types are irrelevant for the mere existence check, and the set literal must
    stay inside the loop rather than becoming a shared constant: every
    ``get_last_statistics`` call discards impossible columns from the set it is
    handed, *in place*, so a reused set would erode with each id.
    """
    return sum(
        1
        for statistic_id in statistic_ids
        if get_last_statistics(hass, 1, statistic_id, False, {"mean", "state", "sum"})
    )


# Typed alias: a TrueNAS config entry carries its coordinator as runtime_data.
type TrueNASConfigEntry = ConfigEntry[TrueNASCoordinator]


def get_truenas_coordinator(
    config_entry: ConfigEntry[Any] | None,
) -> TrueNASCoordinator | None:
    """Return the coordinator stored as ``runtime_data``, or ``None`` if unset.

    ``runtime_data`` is a bare annotation on ``ConfigEntry`` with no default, so
    direct attribute access raises ``AttributeError`` before ``__init__`` has
    assigned it (or after ``async_unload_entry`` has cleared it). Centralizing
    the ``getattr`` fallback here keeps every call site safe without repeating
    the same guard.
    """
    return getattr(config_entry, "runtime_data", None)


def _unwrap_app_stats_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Unwrap collection_update envelope; return inner params/fields dict or None."""
    params = msg.get("params")
    if (
        msg.get("method") == "collection_update"
        and isinstance(params, dict)
        and isinstance(params.get("fields"), list)
    ):
        return params
    return msg if isinstance(msg.get("fields"), list) else None


class _PushSourceState:
    """Per-source push-subscription bookkeeping (sub_id + consumer + breaker).

    One instance per pushed ``coordinator.ds`` source (alerts, service, ...),
    used by the generic ``_ensure_push_subscription``/``_stop_push_subscription``
    helpers below so each new source only wires a handful of lines instead of
    duplicating the full subscribe/consume/breaker lifecycle.
    """

    def __init__(self) -> None:
        self.sub_id: str | None = None
        self.event: str | None = None
        self.consumer: SubscriptionPushConsumer | None = None
        self.breaker = SubscriptionCircuitBreaker()
        self.refresh_lock = asyncio.Lock()


# ---------------------------
#   TrueNASControllerData
# ---------------------------
class TrueNASCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """TrueNASCoordinator Class."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        """Initialize TrueNASCoordinator."""
        self.hass = hass
        self.config_entry: ConfigEntry = config_entry

        poll = int(config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        super().__init__(
            self.hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll),
        )

        self.name = config_entry.data[CONF_NAME]
        self.host = config_entry.data[CONF_HOST]
        # Computed once: a config-entry rename goes through a full entry
        # reload (new coordinator instance), so this never goes stale.
        self._device_slug = slugify(self.name)
        # Set by entity.register_system_device() in async_setup_entry, after the
        # first refresh and before platforms create entities.
        self.system_device_id: str | None = None

        self.ds: dict[str, dict[str, Any]] = {
            "interface": {},
            "disk": {},
            "pool": {},
            "dataset": {},
            "system_info": {},
            "service": {},
            "vm": {},
            "container": {},
            "directoryservices": {},
            "cloudsync": {},
            "replication": {},
            "rsynctask": {},
            "snapshottask": {},
            "scrub": {},
            "app": {},
            "app_stats": {},
            "cronjob": {},
            "ups": {},
            "alerts": {
                "count": 0,
                "messages": [],
                "critical": 0,
                "warning": 0,
                "info": 0,
                "disk_issues": False,
            },
        }

        self.api = TrueNASAPI(
            config_entry.data[CONF_HOST],
            config_entry.data[CONF_API_KEY],
            config_entry.data[CONF_VERIFY_SSL],
        )
        # Normalized TrueNAS domain state (aiotruenas.domain.state.TrueNASState),
        # incrementally taking over the parse_api(...) normalization this
        # coordinator used to do inline -- see MIGRATION_PLAN.md in the
        # aiotruenas repo. Endpoints not yet migrated still compute their own
        # self.ds[...] entries directly below.
        self.state = TrueNASState(self.api.client)

        self.datasets_hass_device_id = None
        self.last_updatecheck_update = datetime(1970, 1, 1, tzinfo=UTC)

        self._version_major: int = 0
        self._version_minor: int = 0

        # Common names that have ever been shared by more than one certificate
        # in a poll -- see ``_assign_certificate_identities`` for why this
        # must persist across polls instead of being recomputed each time.
        self._poisoned_certificate_commons: set[str] = set()

        # Orphaned recorder statistic_ids (no live entity) detected each poll.
        self.orphaned_statistics: list[str] = []

        self._app_stats_event_name: str | None = None
        self._app_stats_sub_id: str | None = None

        self._alerts_sub_id: str | None = None
        self._alerts_push_consumer: SubscriptionPushConsumer | None = None
        self._alerts_breaker = SubscriptionCircuitBreaker()

        self._service_push = _PushSourceState()
        self._pool_push = _PushSourceState()
        self._cloudsync_push = _PushSourceState()
        self._replication_push = _PushSourceState()
        self._rsync_push = _PushSourceState()
        self._vm_push = _PushSourceState()
        self._container_push = _PushSourceState()
        self._app_push = _PushSourceState()

    # ---------------------------
    #   connected
    # ---------------------------
    def connected(self) -> bool:
        """Return connected state."""
        return self.api.connected()

    # ---------------------------
    #   _is_group_monitored
    # ---------------------------
    def _is_group_monitored(self, group: str) -> bool:
        """Return True when the given sensor group is enabled in options."""
        config_entry = getattr(self, "config_entry", None)
        if config_entry is None:
            return True
        monitored = getattr(config_entry, "options", {}).get(
            CONF_MONITORED_GROUPS, DEFAULT_MONITORED_GROUPS
        )
        return group in monitored

    # ---------------------------
    #   set_optimistic_running
    # ---------------------------
    def set_optimistic_running(self, data_path: str, object_id: Any) -> None:
        """Optimistically mark a task's state as RUNNING for instant UI feedback.

        Run actions/buttons often complete a task faster than the poll interval,
        so the transient RUNNING state is gone before the next poll samples it and
        the sensor stays on its previous value with no sign the trigger worked.
        Setting RUNNING in-memory and notifying listeners gives that feedback; the
        next regular poll re-syncs the state to whatever TrueNAS reports.

        ``object_id`` is looked up as a str: callers (button/sensor ``start()``
        methods) pass the object's raw ``id`` field, which for migrated
        endpoints (e.g. rsynctask, replication, snapshottask, scrub) is still
        int-typed at the API level, while ``self.ds`` is str-keyed end to end
        (see ``_as_str_keyed``) -- the original ``object_id`` is left
        untouched for the middleware call in ``async_run_task``.
        """
        group = self.ds.get(data_path)
        uid = str(object_id)
        if isinstance(group, dict) and isinstance(group.get(uid), dict):
            group[uid]["state"] = "RUNNING"
            self.async_update_listeners()
        else:
            _LOGGER.debug(
                "set_optimistic_running: no '%s' object with id %r to mark RUNNING",
                data_path,
                object_id,
            )

    # ---------------------------
    #   async_run_task
    # ---------------------------
    async def async_run_task(self, method: str, object_id: Any, data_path: str) -> None:
        """Trigger a task's run method, then optimistically mark it RUNNING.

        Shared by the run buttons (button.py) and the *_run sensor actions
        (sensor.py) so the trigger + optimistic-state logic lives in one place.

        Raises:
            HomeAssistantError: if the middleware call itself failed. ``query()``
                swallows connection/middleware errors and returns None instead of
                raising, recording the failure in ``api.error`` -- checking that
                (rather than the return value, which is legitimately None on
                success for many of these fire-and-forget methods) is what
                distinguishes a real failure from a normal null response.
        """
        await self.api.query(method, [object_id])
        if self.api.error:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="run_task_failed",
                translation_placeholders={
                    "host": self.host,
                    "error": str(self.api.error),
                },
            )
        self.set_optimistic_running(data_path, object_id)

    # ---------------------------
    #   _async_ensure_connected
    # ---------------------------
    async def _async_ensure_connected(self) -> None:
        """Connect if needed, raising the appropriate coordinator error on failure."""
        if self.api.connected():
            return

        try:
            connected = await self.api.connect()
        except Exception as e:
            raise UpdateFailed(f"Error connecting to TrueNAS: {e}") from e

        if not connected:
            if self.api.error == ERR_INVALID_KEY:
                raise ConfigEntryAuthFailed("Invalid TrueNAS API key")
            _LOGGER.error("TrueNAS connection failed (error code: %s)", self.api.error)
            raise UpdateFailed(f"Error connecting to TrueNAS: {self.api.error}")

    # ---------------------------
    #   _async_update_data
    # ---------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        """Update TrueNAS data."""

        await self._async_ensure_connected()

        jobs = [
            self.get_systemstats,
            self.get_service,
            self.get_disk,
            self.get_dataset,
            self.get_vm,
            self.get_container,
            self.get_directoryservices,
            self.get_cloudsync,
            self.get_replication,
            self.get_rsync,
            self.get_snapshottask,
            self.get_scrub,
            self.get_app,
            self.get_app_stats,
            self.get_cronjob,
            self.get_alerts,
            self.get_certificates,
            self.get_arc,
            self.get_smb,
            self.get_ups,
        ]

        if self.api.connected():

            async def _run_job(job: Callable[[], Awaitable[None]]) -> None:
                try:
                    await job()
                except Exception as err:
                    _LOGGER.exception(
                        "Error running TrueNAS job %s: %s",
                        getattr(job, "__name__", job),
                        err,
                    )

            # get_interface (not get_systeminfo) now populates ds["interface"],
            # and virtualization detection now lives inside TrueNASState itself
            # rather than a local self._is_virtual. get_systemstats still needs
            # both ds["system_info"] and a populated ds["interface"] before it
            # runs (its rx/tx enrichment is a no-op on an empty interface map),
            # so both are run before the concurrent jobs -- otherwise the first
            # cycle would skip the interface graph, leaving RX/TX at 0 until
            # the next poll.
            await _run_job(self.get_systeminfo)

            # A middleware error leaves ds["system_info"] at its empty initial
            # value (query() returns None on failure without dropping the
            # socket). register_system_device() indexes "hostname" out of it
            # right after the first refresh, so failing fast here -- instead of
            # returning ds with that key missing -- is what turns this into a
            # retried setup instead of a crash.
            if "hostname" not in self.ds["system_info"]:
                raise UpdateFailed(
                    "Essential system information (hostname) was not received"
                    " from TrueNAS"
                )

            await _run_job(self.get_interface)

            await asyncio.gather(*(_run_job(job) for job in jobs))

            # get_pool computes pool + dataset capacity internally via
            # TrueNASState now, so it no longer depends on get_dataset()
            # having already run; kept after gather() anyway to avoid
            # reordering the job list for this migration step.
            if self.api.connected():
                await _run_job(self.get_pool)

        now = datetime.now(UTC).replace(microsecond=0)
        delta = now - self.last_updatecheck_update
        if self.api.connected() and delta.total_seconds() > 60 * 60 * 12:
            await self.get_updatecheck()
            self.last_updatecheck_update = now

        if not self.api.connected():
            raise UpdateFailed("TrueNAS disconnected")

        # Re-check orphaned recorder statistics each poll so the diagnostic
        # button and Repairs issue track the current state automatically.
        await self.async_detect_orphaned_statistics()

        # Withdraw a lingering rollback issue once the old integration is gone.
        # (The issue is only ever raised on demand by the diagnostic button.)
        self._clear_stale_migration_rollback_issue()

        return self.ds

    # ---------------------------
    #   Orphaned statistics cleanup
    # ---------------------------
    def _statistics_issue_id(self) -> str:
        """Return the per-entry Repairs issue id for orphaned statistics."""
        return f"{ISSUE_STATISTICS_ORPHANED}_{self.config_entry.entry_id}"

    async def async_detect_orphaned_statistics(self) -> None:
        """Find recorder statistic_ids of this config entry with no live entity.

        After an entity-id rename the recorder can leave the old long-term
        statistics behind when the target name already exists. We list the
        recorder-sourced statistics matching this entry's device-name slug and
        keep those whose entity is no longer in the registry. Scoping by this
        entry's own slug (not a fixed integration-wide one) matters on
        multi-entry installs: without it, every entry's coordinator would flag
        every other entry's orphans too, each raising its own duplicate
        Repairs issue for the same statistics (#61).

        Older leftovers are often *metadata-only* (their data points were purged
        long ago), which is why they can be reported here without being visible
        in Developer Tools → Statistics — see ``async_count_orphans_with_data``.
        """
        if "recorder" not in self.hass.config.components:
            return

        try:
            stat_ids = await get_instance(self.hass).async_add_executor_job(
                list_statistic_ids, self.hass
            )
        except Exception:  # noqa: BLE001 - never let detection break a poll
            _LOGGER.debug(
                "Could not list statistic ids for orphan detection", exc_info=True
            )
            return

        ent_reg = er.async_get(self.hass)
        previous = self.orphaned_statistics
        # Sorted once here so log output, the repair dialog and the change check
        # below all share one order: ``list_statistic_ids`` makes no ordering
        # promise, so an unsorted list could "change" without any orphan doing so.
        self.orphaned_statistics = sorted(
            meta["statistic_id"]
            for meta in stat_ids
            if isinstance(meta, dict)
            and meta.get("source") == "recorder"
            and isinstance(meta.get("statistic_id"), str)
            and _is_truenas_sensor_id(meta["statistic_id"], self._device_slug)
            and ent_reg.async_get(meta["statistic_id"]) is None
        )
        # Logged only on change: detection runs every poll, and the full id list
        # is what a user needs for a bug report before deleting anything.
        if self.orphaned_statistics != previous:
            _LOGGER.debug(
                "Orphaned TrueNAS statistics (%d): %s",
                len(self.orphaned_statistics),
                ", ".join(self.orphaned_statistics) or "none",
            )
        self._update_statistics_issue()

    async def async_count_orphans_with_data(self) -> int:
        """Return how many orphaned statistics still hold recorded data points.

        Long-standing orphans are frequently metadata-only: the recorder purged
        their data points long ago and only the (invisible) metadata row keeps
        them listed, so they cannot be found in Developer Tools → Statistics.
        The Repairs dialog probes this on demand to word its explanation
        correctly — never per poll, since it queries the database.

        Falls back to "all of them" when the probe fails, which is the
        conservative assumption the dialog made unconditionally before.
        """
        if not self.orphaned_statistics:
            return 0
        if "recorder" not in self.hass.config.components:
            return len(self.orphaned_statistics)

        try:
            return await get_instance(self.hass).async_add_executor_job(
                _count_statistics_with_data, self.hass, list(self.orphaned_statistics)
            )
        except Exception:  # noqa: BLE001 - the dialog must open regardless
            _LOGGER.debug(
                "Could not probe orphaned statistics for data points", exc_info=True
            )
            return len(self.orphaned_statistics)

    def _update_statistics_issue(self) -> None:
        """Create or clear the Repairs issue based on the current orphan state."""
        ignored = self.config_entry.options.get(CONF_STATISTICS_CLEANUP_IGNORED, False)
        if self.orphaned_statistics and not ignored:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._statistics_issue_id(),
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_STATISTICS_ORPHANED,
                translation_placeholders={"count": str(len(self.orphaned_statistics))},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self._statistics_issue_id())

    # ---------------------------
    #   Community-Edition migration rollback
    # ---------------------------
    def _migration_rollback_issue_id(self) -> str:
        """Return the per-entry Repairs issue id for the migration rollback."""
        return f"{ISSUE_MIGRATION_ROLLBACK}_{self.config_entry.entry_id}"

    def _rollback_possible(self) -> bool:
        """Whether a rollback to the disabled legacy entry is still possible."""
        if DOMAIN == LEGACY_DOMAIN:
            return False
        legacy_id = self.config_entry.data.get(MIGRATION_LEGACY_ENTRY_ID)
        return bool(legacy_id and self.hass.config_entries.async_get_entry(legacy_id))

    def raise_migration_rollback_issue(self) -> None:
        """Raise the rollback confirm issue on demand (from the diagnostic button).

        The issue is never shown automatically; it is only opened when the user
        presses the rollback button. Creating it is idempotent (stable id), so
        re-pressing after a dismiss simply re-opens it. No-op while a rollback is
        not possible.
        """
        if not self._rollback_possible():
            return
        count = len(self.config_entry.data.get(MIGRATION_RECORDS, []))
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._migration_rollback_issue_id(),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_MIGRATION_ROLLBACK,
            translation_placeholders={"count": str(count)},
        )

    def _clear_stale_migration_rollback_issue(self) -> None:
        """Withdraw a lingering rollback issue once a rollback is no longer possible.

        Runs each poll. It only ever *deletes* — the issue is raised solely by the
        diagnostic button — so a leftover dialog is cleared after the user removes
        the old "truenas" integration (the bridge is then permanently burned).
        """
        if DOMAIN == LEGACY_DOMAIN:
            return
        if not self._rollback_possible():
            ir.async_delete_issue(
                self.hass, DOMAIN, self._migration_rollback_issue_id()
            )

    async def async_clear_orphaned_statistics(self) -> None:
        """Delete the detected orphaned statistics and refresh entities/issue."""
        if not self.orphaned_statistics:
            return

        get_instance(self.hass).async_clear_statistics(list(self.orphaned_statistics))
        _LOGGER.info(
            "Cleared %d orphaned TrueNAS statistic(s)", len(self.orphaned_statistics)
        )
        self.orphaned_statistics = []
        ir.async_delete_issue(self.hass, DOMAIN, self._statistics_issue_id())
        # Push the empty state so the diagnostic button updates immediately.
        self.async_set_updated_data(self.ds)

    # ---------------------------
    #   get_systeminfo
    # ---------------------------
    _SYSTEM_INFO_CARRY_FIELDS = (
        "update_available",
        "update_progress",
        "update_jobid",
        "update_state",
        "update_version",
        "smb_connections",
    )
    _SYSTEM_INFO_CARRY_DEFAULTS: dict[str, Any] = {
        "update_available": False,
        "update_progress": 0,
        "update_jobid": 0,
        "update_state": "unknown",
        "update_version": "unknown",
        "smb_connections": 0,
    }

    async def get_systeminfo(self) -> None:
        """Get system info via the aiotruenas domain layer.

        Carries forward the update-job and SMB-connection-count fields that
        ``TrueNASState.get_systeminfo()`` intentionally does not own (see
        ``get_updatecheck``/the SMB merge) -- otherwise every poll would reset
        an in-progress system-update job's tracking state to defaults, exactly
        the bug already fixed for per-app upgrade jobs in ``_refresh_app``.
        """
        previous = self.ds["system_info"]
        self.ds["system_info"] = await self.state.get_systeminfo()
        for field in self._SYSTEM_INFO_CARRY_FIELDS:
            self.ds["system_info"][field] = previous.get(
                field, self._SYSTEM_INFO_CARRY_DEFAULTS[field]
            )

        if not self.api.connected():
            return

        # Ensure update_version is not unknown if no update is available
        if not self.ds["system_info"].get("update_available"):
            self.ds["system_info"]["update_version"] = self.ds["system_info"].get(
                "version", "unknown"
            )

        await self._handle_update_job()
        if not self.api.connected():
            return

        self._parse_version()

    # ---------------------------
    #   get_interface
    # ---------------------------
    async def get_interface(self) -> None:
        """Get network interfaces via the aiotruenas domain layer."""
        self.ds["interface"] = _as_str_keyed(await self.state.get_interface())

    # ---------------------------
    #   _handle_update_job
    # ---------------------------
    async def _handle_update_job(self) -> None:
        """Refresh progress/state for a running update job, if any."""
        if not self.ds["system_info"].get("update_jobid"):
            return

        self.ds["system_info"] = parse_api(
            data=self.ds["system_info"],
            source=await self.api.query(
                "core.get_jobs",
                params=[[["id", "=", self.ds["system_info"].get("update_jobid")]]],
            ),
            vals=[
                {
                    "name": "update_progress",
                    "source": "progress/percent",
                    "default": 0,
                },
                {
                    "name": "update_state",
                    "source": "state",
                    "default": "unknown",
                },
            ],
        )
        if not self.api.connected():
            return

        if self.ds["system_info"].get("update_state") != "RUNNING" or not self.ds[
            "system_info"
        ].get("update_available"):
            self.ds["system_info"]["update_progress"] = 0
            self.ds["system_info"]["update_jobid"] = 0
            self.ds["system_info"]["update_state"] = "unknown"

    # ---------------------------
    #   _parse_version
    # ---------------------------
    def _parse_version(self) -> None:
        """Parse major/minor version numbers from the reported version string.

        Prevents a "0.0.0" display and avoids misrepresenting the system version
        on malformed or missing input.
        """
        version_str = str(self.ds["system_info"].get("version", "") or "")
        clean_version = version_str.replace("TrueNAS-", "").replace("SCALE-", "")

        # Bounded quantifiers ({1,9}) avoid unbounded backtracking (Sonar S5852);
        # version components never have that many digits.
        if match := re.search(r"(\d{1,9})\.(\d{1,9})", clean_version):
            self._version_major = int(match[1])
            self._version_minor = int(match[2])
        elif clean_version:
            _LOGGER.debug(
                "Failed to parse TrueNAS version from string: %s", version_str
            )

    # ---------------------------
    #   supports_update_run
    # ---------------------------
    def supports_update_run(self) -> bool:
        """Return True if the "update.run" API method is available.

        TrueNAS 25.10 split the legacy "update.update" method: it now only
        writes update *settings*, while installing an update (with the
        optional reboot) moved to the new "update.run" method.
        """
        return (self._version_major, self._version_minor) >= (25, 10)

    # ---------------------------
    #   supports_container_api
    # ---------------------------
    def supports_container_api(self) -> bool:
        """Return True if containers live under the ``container.*`` namespace.

        TrueNAS 26.0 dropped the Incus-based ``virt.*`` API; LXC containers
        are now managed through ``container.query`` / ``container.start`` /
        ``container.stop`` (libvirt), with a different entry shape.
        """
        return (self._version_major, self._version_minor) >= (26, 0)

    # ---------------------------
    #   get_updatecheck
    # ---------------------------
    async def get_updatecheck(self) -> None:
        """Check for pending updates via the aiotruenas domain layer.

        ``TrueNASState.get_update()`` returns a standalone flat map (its
        "no update pending" resting state already matches this coordinator's
        prior defaults: ``update_state="IDLE"``, ``update_version=
        "up-to-date"``); merged into ``system_info`` here so the update
        sensors' data paths are unchanged. Falls back to the current running
        version, matching the previous local default, when no update is
        pending and ``system_info`` already has one.
        """
        update = await self.state.get_update()
        if update["update_version"] == "up-to-date" and self.ds["system_info"].get(
            "version"
        ):
            update = {**update, "update_version": self.ds["system_info"]["version"]}
        self.ds["system_info"].update(update)
        if update["update_available"]:
            _LOGGER.debug("TrueNAS Update found: %s", update["update_version"])

    # ---------------------------
    #   get_systemstats
    # ---------------------------
    async def get_systemstats(self) -> None:
        """Get system statistics via the aiotruenas domain layer.

        Mutates the same dict objects already assigned to
        ``ds["system_info"]``/``ds["interface"]`` in place (CPU/load/
        memory/ARC stats, interface rx/tx), so no explicit re-sync is
        needed here.
        """
        await self.state.get_systemstats()

    # ---------------------------
    #   get_service
    # ---------------------------
    # Verified against a live TrueNAS instance (2026-08-21): core.subscribe on
    # "service.query" is accepted and returns a real subscription id. Services
    # change rarely (start/stop of a handful of daemons), so -- like alerts --
    # any push message is treated as a pure "something changed, refetch now"
    # signal and re-runs the same full query _refresh_service already does
    # every poll tick.
    _SERVICE_EVENT = "service.query"

    async def get_service(self) -> None:
        """Refresh services, then ensure the push subscription is active."""
        await self._refresh_locked(self._service_push, self._refresh_service)
        await self._ensure_push_subscription(
            self._service_push,
            self._SERVICE_EVENT,
            self._on_service_push,
            label="service",
        )

    async def _on_service_push(self, _batch: list[Any]) -> None:
        """Immediately refresh service state on push notification."""
        await self._refresh_locked(self._service_push, self._refresh_service)
        self.async_set_updated_data(self.ds)

    async def stop_service_push(self) -> None:
        """Stop the service push subscription, e.g. on unload."""
        await self._stop_push_subscription(self._service_push)

    async def _refresh_service(self) -> None:
        """Query services via the aiotruenas domain layer."""
        self.ds["service"] = _as_str_keyed(await self.state.get_service())

    # ---------------------------
    #   get_pool
    # ---------------------------
    # Verified against a live TrueNAS instance (2026-08-22): core.subscribe on
    # "pool.query" is accepted and returns a real subscription id. Pool health
    # changes (degraded/faulted, capacity, scrub state, etc.) are infrequent,
    # so -- like service -- any push message is treated as a pure "something
    # changed, refetch now" signal and re-runs the same full query
    # _refresh_pool already does every poll tick.
    _POOL_EVENT = "pool.query"

    async def get_pool(self) -> None:
        """Refresh pools, then ensure the push subscription is active."""
        await self._refresh_locked(self._pool_push, self._refresh_pool)
        await self._ensure_push_subscription(
            self._pool_push,
            self._POOL_EVENT,
            self._on_pool_push,
            label="pool",
        )

    async def _on_pool_push(self, _batch: list[Any]) -> None:
        """Immediately refresh pool state on push notification."""
        await self._refresh_locked(self._pool_push, self._refresh_pool)
        self.async_set_updated_data(self.ds)

    async def stop_pool_push(self) -> None:
        """Stop the pool push subscription, e.g. on unload."""
        await self._stop_push_subscription(self._pool_push)

    async def _refresh_pool(self) -> None:
        """Refresh pool state via the aiotruenas domain layer.

        ``TrueNASState.get_pool()`` refreshes and derives pool capacity from
        its own, internally-fetched dataset snapshot (see its docstring), so
        this no longer reads/writes ``self.ds["dataset"]`` -- that key
        remains exclusively owned by ``get_dataset()`` below, which is gated
        by the "datasets" monitored group independently of pool monitoring.
        """
        self.ds["pool"] = _as_str_keyed(await self.state.get_pool())

    # ---------------------------
    #   get_dataset
    # ---------------------------
    async def get_dataset(self) -> None:
        """Get datasets from TrueNAS."""
        if not self._is_group_monitored(MONITOR_GROUP_DATASETS):
            self.ds["dataset"] = {}
            return
        self.ds["dataset"] = _as_str_keyed(await self.state.get_dataset())

    # ---------------------------
    #   get_disk
    # ---------------------------
    async def get_disk(self) -> None:
        """Get disks (with netdata/API-fallback temperature enrichment) via
        the aiotruenas domain layer.
        """
        self.ds["disk"] = _as_str_keyed(await self.state.get_disk())

    # ---------------------------
    #   get_vm
    # ---------------------------
    # Verified against a live TrueNAS instance (2026-08-22): core.subscribe on
    # "vm.query" is accepted and returns a real subscription id. Like
    # service/alerts, any push message is treated as a pure "something
    # changed, refetch now" signal and re-runs the same full query
    # _refresh_vm already does every poll tick.
    _VM_EVENT = "vm.query"

    async def get_vm(self) -> None:
        """Refresh VMs, then ensure the push subscription is active."""
        if not self._is_group_monitored(MONITOR_GROUP_VMS):
            self.ds["vm"] = {}
            await self._stop_push_subscription(self._vm_push)
            return
        await self._refresh_locked(self._vm_push, self._refresh_vm)
        await self._ensure_push_subscription(
            self._vm_push,
            self._VM_EVENT,
            self._on_vm_push,
            label="vm",
        )

    async def _on_vm_push(self, _batch: list[Any]) -> None:
        """Immediately refresh VM state on push notification."""
        await self._refresh_locked(self._vm_push, self._refresh_vm)
        self.async_set_updated_data(self.ds)

    async def stop_vm_push(self) -> None:
        """Stop the VM push subscription, e.g. on unload."""
        await self._stop_push_subscription(self._vm_push)

    async def _refresh_vm(self) -> None:
        """Query VMs via the aiotruenas domain layer."""
        self.ds["vm"] = _as_str_keyed(await self.state.get_vm())

    # ---------------------------
    #   get_container
    # ---------------------------
    # Verified against a live TrueNAS instance (2026-08-22): core.subscribe on
    # both "container.query" and "virt.instance.query" is accepted and
    # returns a real subscription id. Like service/alerts, any push message
    # is treated as a pure "something changed, refetch now" signal and
    # re-runs the same full query _refresh_container already does every poll
    # tick. The topic is re-derived from supports_container_api() on every
    # call (not cached) so a mid-session TrueNAS upgrade is picked up.
    async def get_container(self) -> None:
        """Refresh containers, then ensure the push subscription is active."""
        if not self._is_group_monitored(MONITOR_GROUP_CONTAINERS):
            self.ds["container"] = {}
            await self._stop_push_subscription(self._container_push)
            return
        await self._refresh_locked(self._container_push, self._refresh_container)
        event = (
            "container.query"
            if self.supports_container_api()
            else "virt.instance.query"
        )
        await self._ensure_push_subscription(
            self._container_push,
            event,
            self._on_container_push,
            label="container",
        )

    async def _on_container_push(self, _batch: list[Any]) -> None:
        """Immediately refresh container state on push notification."""
        await self._refresh_locked(self._container_push, self._refresh_container)
        self.async_set_updated_data(self.ds)

    async def stop_container_push(self) -> None:
        """Stop the container push subscription, e.g. on unload."""
        await self._stop_push_subscription(self._container_push)

    async def _refresh_container(self) -> None:
        """Get container instances via the aiotruenas domain layer.

        ``TrueNASState.get_container()`` dispatches internally between
        ``container.query`` (TrueNAS 26.0+) and ``virt.instance.query``
        (legacy) based on its own version detection.
        """
        self.ds["container"] = _as_str_keyed(await self.state.get_container())

    # ---------------------------
    #   get_directoryservices
    # ---------------------------
    async def get_directoryservices(self) -> None:
        """Get Directory Services (AD/LDAP/IPA) status via the domain layer.

        Gating on whether the group is monitored stays here (an HA
        options-flow concern); ``TrueNASState.get_directoryservices()``
        always queries and normalizes, returning an empty map when no
        directory service is configured/enabled.
        """
        if not self._is_group_monitored(MONITOR_GROUP_DIRECTORY_SERVICES):
            self.ds["directoryservices"] = {}
            return
        self.ds["directoryservices"] = _as_str_keyed(
            await self.state.get_directoryservices()
        )

    # ---------------------------
    #   generic push-subscription helpers
    # ---------------------------
    # Shared by every push-subscribed source added after alerts (the pilot,
    # which keeps its own hand-rolled/tested implementation below rather than
    # being retrofitted onto this, to avoid touching already live-verified
    # code). New sources should follow the ``get_service``/``_refresh_service``
    # pattern near the end of this file: a ``_PushSourceState`` instance field,
    # an ``_EVENT`` constant, a thin ``get_<name>`` wrapper calling
    # ``_refresh_<name>`` + ``_ensure_push_subscription``, an
    # ``_on_<name>_push`` callback, and a ``stop_<name>_push`` for unload.
    async def _ensure_push_subscription(
        self,
        state: _PushSourceState,
        event: str,
        on_push: Callable[[list[Any]], Awaitable[None]],
        *,
        label: str,
    ) -> None:
        """(Re-)establish a push subscription for one source if not active."""
        if not self.api.connected():
            return

        if state.sub_id and not await self.api.is_subscribed(state.sub_id):
            await self._stop_push_subscription(state)

        if state.sub_id and state.event != event:
            _LOGGER.debug(
                "TrueNAS %s subscription topic changed (%s -> %s); resubscribing",
                label,
                state.event,
                event,
            )
            await self._stop_push_subscription(state)

        if state.sub_id:
            return

        if state.breaker.tripped:
            if not state.breaker.should_attempt_reset():
                return
            state.breaker.reset()

        try:
            sub_id, queue = await self.api.subscribe_events(event)
        except Exception as err:
            _LOGGER.exception("Failed to establish %s subscription: %s", label, err)
            return

        if not sub_id or queue is None:
            _LOGGER.debug("%s subscription failed: no sub_id/queue returned", label)
            return

        async def _on_trip() -> None:
            _LOGGER.warning(
                "TrueNAS %s subscription falling back to polling after circuit "
                "breaker trip",
                label,
            )
            await self._stop_push_subscription(state)

        consumer = SubscriptionPushConsumer(
            queue, on_push, breaker=state.breaker, on_trip=_on_trip
        )
        consumer.start(task_factory=self.hass.async_create_background_task)
        state.sub_id = sub_id
        state.event = event
        state.consumer = consumer
        _LOGGER.debug("TrueNAS %s push subscription established: %s", label, sub_id)

    def _clear_push_subscription(self, state: _PushSourceState) -> None:
        """Clear local subscription state for one source."""
        state.sub_id = None
        state.event = None
        state.consumer = None

    async def _stop_push_subscription(self, state: _PushSourceState) -> None:
        """Stop one source's push subscription, e.g. on unload/breaker trip."""
        if state.consumer is not None:
            await state.consumer.stop()
        if state.sub_id and self.api.connected():
            try:
                await self.api.unsubscribe_events(state.sub_id)
            except Exception as exc:
                _LOGGER.debug(
                    "TrueNAS failed to unsubscribe %s (%s)", state.sub_id, exc
                )
        self._clear_push_subscription(state)

    async def _refresh_locked(
        self, state: _PushSourceState, refresh: Callable[[], Awaitable[None]]
    ) -> None:
        """Serialize one source's refresh calls.

        Without this, a push notification's immediate refresh can race the
        regular 60s poll's refresh of the same source; whichever finishes
        last wins, so a slower poll can silently overwrite fresher
        push-triggered state with stale data.
        """
        async with state.refresh_lock:
            await refresh()

    # ---------------------------
    #   get_alerts
    # ---------------------------
    async def get_alerts(self) -> None:
        """Refresh alerts, then ensure the push subscription is active.

        The full ``alert.list`` query below is cheap and stays unconditional
        on every poll tick, so it doubles as the safety net if the push
        subscription is inactive/tripped -- unlike ``app.stats``, alerts has
        no cost difference between "query everything" and "read the queue",
        so a failed subscription degrades straight back to today's plain
        polling instead of needing a separate fallback path.
        """
        await self._refresh_alerts()
        await self._ensure_alerts_subscription()

    async def _refresh_alerts(self) -> None:
        """Query and aggregate alerts via the aiotruenas domain layer."""
        self.ds["alerts"] = await self.state.get_alerts()

    # ---------------------------
    #   alerts push subscription helpers
    # ---------------------------
    # Subscription lifecycle:
    #   UNSUBSCRIBED: _alerts_sub_id is None.
    #   SUBSCRIBED:   _alerts_sub_id and _alerts_push_consumer are set together.
    #   stop_alerts clears both, unconditionally.
    # Verified against a live TrueNAS instance (2026-08-21): core.subscribe on
    # "alert.list" delivers a collection_update notification (msg: "removed")
    # on alert.dismiss. The push consumer below treats arrival of any message
    # as a pure "something changed, refetch now" signal and re-runs the same
    # full query _refresh_alerts already does every poll tick, rather than
    # trying to reconstruct the aggregated counts from partial CRUD payloads.
    _ALERTS_EVENT = "alert.list"

    async def _ensure_alerts_subscription(self) -> None:
        """(Re-)establish the alerts push subscription if not already active."""
        if not self.api.connected():
            return

        if self._alerts_sub_id and not await self.api.is_subscribed(
            self._alerts_sub_id
        ):
            await self.stop_alerts()

        if self._alerts_sub_id:
            return

        if self._alerts_breaker.tripped:
            if not self._alerts_breaker.should_attempt_reset():
                return
            self._alerts_breaker.reset()

        try:
            sub_id, queue = await self.api.subscribe_events(self._ALERTS_EVENT)
        except Exception as err:
            _LOGGER.exception("Failed to establish alerts subscription: %s", err)
            return

        if not sub_id or queue is None:
            _LOGGER.debug("Alerts subscription failed: no sub_id/queue returned")
            return

        consumer = SubscriptionPushConsumer(
            queue,
            self._on_alerts_push,
            breaker=self._alerts_breaker,
            on_trip=self._on_alerts_breaker_trip,
        )
        consumer.start(task_factory=self.hass.async_create_background_task)
        self._alerts_sub_id = sub_id
        self._alerts_push_consumer = consumer
        _LOGGER.debug("TrueNAS alerts push subscription established: %s", sub_id)

    async def _on_alerts_push(self, _batch: list[Any]) -> None:
        """Immediately refresh alerts state on push notification (Sofort-Trigger)."""
        await self._refresh_alerts()
        self.async_set_updated_data(self.ds)

    async def _on_alerts_breaker_trip(self) -> None:
        """Unsubscribe and fall back to plain polling after the breaker trips."""
        _LOGGER.warning(
            "TrueNAS alerts subscription falling back to polling after circuit "
            "breaker trip"
        )
        await self.stop_alerts()

    def _clear_alerts_subscription(self) -> None:
        """Clear local alerts subscription state."""
        self._alerts_sub_id = None
        self._alerts_push_consumer = None

    async def stop_alerts(self) -> None:
        """Stop the alerts push subscription, e.g. on unload."""
        if self._alerts_push_consumer is not None:
            await self._alerts_push_consumer.stop()
        if self._alerts_sub_id and self.api.connected():
            try:
                await self.api.unsubscribe_events(self._alerts_sub_id)
            except Exception as exc:
                _LOGGER.debug(
                    "TrueNAS failed to unsubscribe alerts %s (%s)",
                    self._alerts_sub_id,
                    exc,
                )
        self._clear_alerts_subscription()

    # ---------------------------
    #   get_certificates
    # ---------------------------
    async def get_certificates(self) -> None:
        """Get TrueNAS certificates.

        Keyed by ``identity`` -- the certificate's subject common name
        (``common``), falling back to ``name`` when that is empty or shared
        by more than one certificate in this poll -- rather than ``name``
        itself. ``name`` is DB-unique in TrueNAS and stable across TrueNAS's
        own in-place scheduled auto-renewal (#61), but tools that rotate
        certificates via ``certificate.create`` (e.g. the deploy-freenas ACME
        helper) mint a fresh, timestamped ``name`` on every run, which still
        orphaned the sensor on every renewal (#113). The common name
        identifies the underlying domain and stays stable across such
        rotations -- unless it collides with another certificate's, in which
        case keying by it would silently drop one of them (each new entry
        overwriting the previous one under the same dict key), so those fall
        back to the always-unique ``name`` instead.
        """
        certificates = await self.api.query("certificate.query")
        if isinstance(certificates, list):
            _assign_certificate_identities(
                certificates, self._poisoned_certificate_commons
            )
        self.ds["certificate"] = parse_api(
            data={},
            source=certificates,
            key="_identity",
            vals=_CERTIFICATE_VALS,
        )
        now = dt_util.utcnow()
        for cert in self.ds["certificate"].values():
            if not isinstance(cert, dict):
                continue
            until = cert.get("until")
            cert["days_until_expiry"] = (
                max(0, (until - now).days) if isinstance(until, datetime) else None
            )

    # ---------------------------
    #   get_arc
    # ---------------------------
    async def get_arc(self) -> None:
        """Get ZFS ARC hit ratio via the aiotruenas domain layer."""
        self.ds["arc"] = await self.state.get_arc()

    # ---------------------------
    #   get_smb
    # ---------------------------
    async def get_smb(self) -> None:
        """Get active SMB connections via the aiotruenas domain layer.

        ``TrueNASState.get_smb()`` returns a standalone ``{"connections": N}``
        map; merged into ``system_info`` here so the ``smb_connections``
        sensor's data path is unchanged.
        """
        smb = await self.state.get_smb()
        if "connections" in smb:
            self.ds["system_info"]["smb_connections"] = smb["connections"]

    # ---------------------------
    #   get_ups
    # ---------------------------
    async def get_ups(self) -> None:
        """Get UPS readings via the aiotruenas domain layer, if a UPS is present."""
        if not self._is_group_monitored(MONITOR_GROUP_UPS):
            self.ds["ups"] = {}
            return
        self.ds["ups"] = await self.state.get_ups()

    # ---------------------------
    #   get_cloudsync
    # ---------------------------
    # Verified against a live TrueNAS instance (2026-08-22): core.subscribe on
    # "cloudsync.query" is accepted and returns a real subscription id. Like
    # service/alerts, any push message is treated as a pure "something
    # changed, refetch now" signal and re-runs the same full query
    # _refresh_cloudsync already does every poll tick.
    _CLOUDSYNC_EVENT = "cloudsync.query"

    async def get_cloudsync(self) -> None:
        """Refresh cloudsync tasks, then ensure the push subscription is active."""
        if not self._is_group_monitored(MONITOR_GROUP_CLOUDSYNC):
            self.ds["cloudsync"] = {}
            await self._stop_push_subscription(self._cloudsync_push)
            return
        await self._refresh_locked(self._cloudsync_push, self._refresh_cloudsync)
        await self._ensure_push_subscription(
            self._cloudsync_push,
            self._CLOUDSYNC_EVENT,
            self._on_cloudsync_push,
            label="cloudsync",
        )

    async def _on_cloudsync_push(self, _batch: list[Any]) -> None:
        """Immediately refresh cloudsync state on push notification."""
        await self._refresh_locked(self._cloudsync_push, self._refresh_cloudsync)
        self.async_set_updated_data(self.ds)

    async def stop_cloudsync_push(self) -> None:
        """Stop the cloudsync push subscription, e.g. on unload."""
        await self._stop_push_subscription(self._cloudsync_push)

    async def _refresh_cloudsync(self) -> None:
        """Query cloudsync tasks via the aiotruenas domain layer."""
        self.ds["cloudsync"] = _as_str_keyed(await self.state.get_cloudsync())

    # ---------------------------
    #   get_replication
    # ---------------------------
    # Verified against a live TrueNAS instance (2026-08-22): core.subscribe on
    # "replication.query" is accepted and returns a real subscription id. Like
    # service/alerts, any push message is treated as a pure "something
    # changed, refetch now" signal and re-runs the same full query
    # _refresh_replication already does every poll tick.
    _REPLICATION_EVENT = "replication.query"

    async def get_replication(self) -> None:
        """Refresh replication tasks, then ensure the push subscription is active."""
        if not self._is_group_monitored(MONITOR_GROUP_REPLICATION):
            self.ds["replication"] = {}
            await self._stop_push_subscription(self._replication_push)
            return
        await self._refresh_locked(self._replication_push, self._refresh_replication)
        await self._ensure_push_subscription(
            self._replication_push,
            self._REPLICATION_EVENT,
            self._on_replication_push,
            label="replication",
        )

    async def _on_replication_push(self, _batch: list[Any]) -> None:
        """Immediately refresh replication state on push notification."""
        await self._refresh_locked(self._replication_push, self._refresh_replication)
        self.async_set_updated_data(self.ds)

    async def stop_replication_push(self) -> None:
        """Stop the replication push subscription, e.g. on unload."""
        await self._stop_push_subscription(self._replication_push)

    async def _refresh_replication(self) -> None:
        """Query replication tasks via the aiotruenas domain layer."""
        self.ds["replication"] = _as_str_keyed(await self.state.get_replication())

    # ---------------------------
    #   get_rsync
    # ---------------------------
    # Verified against a live TrueNAS instance (2026-08-22): core.subscribe on
    # "rsynctask.query" is accepted and returns a real subscription id. Like
    # service/alerts, any push message is treated as a pure "something
    # changed, refetch now" signal and re-runs the same full query
    # _refresh_rsync already does every poll tick.
    _RSYNC_EVENT = "rsynctask.query"

    async def get_rsync(self) -> None:
        """Refresh rsync tasks, then ensure the push subscription is active."""
        if not self._is_group_monitored(MONITOR_GROUP_RSYNC):
            self.ds["rsynctask"] = {}
            await self._stop_push_subscription(self._rsync_push)
            return
        await self._refresh_locked(self._rsync_push, self._refresh_rsync)
        await self._ensure_push_subscription(
            self._rsync_push,
            self._RSYNC_EVENT,
            self._on_rsync_push,
            label="rsync",
        )

    async def _on_rsync_push(self, _batch: list[Any]) -> None:
        """Immediately refresh rsync task state on push notification."""
        await self._refresh_locked(self._rsync_push, self._refresh_rsync)
        self.async_set_updated_data(self.ds)

    async def stop_rsync_push(self) -> None:
        """Stop the rsync push subscription, e.g. on unload."""
        await self._stop_push_subscription(self._rsync_push)

    async def _refresh_rsync(self) -> None:
        """Query rsync tasks via the aiotruenas domain layer."""
        self.ds["rsynctask"] = _as_str_keyed(await self.state.get_rsync())

    # ---------------------------
    #   get_snapshottask
    # ---------------------------
    async def get_snapshottask(self) -> None:
        """Get snapshot tasks via the aiotruenas domain layer."""
        if not self._is_group_monitored(MONITOR_GROUP_SNAPSHOTS):
            self.ds["snapshottask"] = {}
            return
        self.ds["snapshottask"] = _as_str_keyed(await self.state.get_snapshottask())

    # ---------------------------
    #   get_scrub
    # ---------------------------
    async def get_scrub(self) -> None:
        """Get pool scrub tasks via the aiotruenas domain layer."""
        self.ds["scrub"] = _as_str_keyed(await self.state.get_scrub())

    # ---------------------------
    #   get_app
    # ---------------------------
    # Verified against a live TrueNAS instance (2026-08-22): core.subscribe on
    # "app.query" is accepted and returns a real subscription id. Like
    # service/alerts, any push message is treated as a pure "something
    # changed, refetch now" signal and re-runs the same full query
    # _refresh_app already does every poll tick. get_app has no monitor-group
    # gate today (unlike get_app_stats, which gates on MONITOR_GROUP_
    # CONTAINERS), so this wrapper stays ungated too.
    _APP_EVENT = "app.query"

    async def get_app(self) -> None:
        """Refresh apps, then ensure the push subscription is active."""
        await self._refresh_locked(self._app_push, self._refresh_app)
        await self._ensure_push_subscription(
            self._app_push,
            self._APP_EVENT,
            self._on_app_push,
            label="app",
        )

    async def _on_app_push(self, _batch: list[Any]) -> None:
        """Immediately refresh app state on push notification."""
        await self._refresh_locked(self._app_push, self._refresh_app)
        self.async_set_updated_data(self.ds)

    async def stop_app_push(self) -> None:
        """Stop the app push subscription, e.g. on unload."""
        await self._stop_push_subscription(self._app_push)

    _APP_UPDATE_JOB_FIELDS = (
        "update_jobid",
        "update_progress",
        "update_state",
        "update_description",
    )
    _APP_UPDATE_JOB_DEFAULTS: dict[str, Any] = {
        "update_jobid": 0,
        "update_progress": 0,
        "update_state": "unknown",
        "update_description": "",
    }

    async def _refresh_app(self) -> None:
        """Query apps via the aiotruenas domain layer, then track update jobs.

        Update-job tracking (``update_jobid``/``update_progress``/...) is not
        part of the domain layer's normalization -- it is tied to this
        integration's own HA update-entity polling. ``TrueNASState`` returns
        its own internally-cached dict on every call, which never carries
        these fields, so an in-progress job's tracking state is carried
        forward by hand from the previous ``self.ds["app"]`` snapshot instead
        of being lost (reset to "no job running") on every poll.
        """
        previous = self.ds["app"]
        self.ds["app"] = _as_str_keyed(await self.state.get_app())
        for uid, vals in self.ds["app"].items():
            carried = previous.get(uid, {})
            for field in self._APP_UPDATE_JOB_FIELDS:
                vals[field] = carried.get(field, self._APP_UPDATE_JOB_DEFAULTS[field])

        await self._refresh_app_update_jobs()

    async def _refresh_app_update_jobs(self) -> None:
        """Mirror the state of every tracked app upgrade job into ``ds["app"]``.

        The update entity polls its own job at a much higher cadence while an
        install is running; this per-poll pass is the safety net that keeps the
        entity from staying "in progress" forever (e.g. after HA restarts mid
        upgrade or if the entity's tracking loop gave up).
        """
        for uid, vals in self.ds["app"].items():
            if vals.get("update_jobid"):
                await self.async_refresh_app_update_job(uid)

    async def async_refresh_app_update_job(self, uid: str) -> dict[str, Any] | None:
        """Poll the upgrade job of app ``uid`` and store its progress.

        Writes ``update_state``, ``update_progress`` (percent) and
        ``update_description`` into the app's data. Once the job leaves an
        active state (or can no longer be found) ``update_jobid`` is reset so
        the app can be upgraded again.

        Returns the raw job dict, or ``None`` when there is nothing to track or
        the API call failed (in which case tracking state is left untouched).
        """
        vals = self.ds.get("app", {}).get(uid)
        if not vals:
            return None
        job_id = vals.get("update_jobid")
        if not job_id:
            return None

        jobs = await self.api.query("core.get_jobs", params=[[["id", "=", job_id]]])
        if jobs is None:
            # Transient API error: keep tracking, retry on the next poll.
            return None
        job = jobs[0] if jobs and isinstance(jobs[0], dict) else None
        if job is None:
            _LOGGER.warning(
                "Upgrade job %s for app %s no longer exists on %s; stopped tracking",
                job_id,
                uid,
                self.host,
            )
            self._reset_app_update_job(vals, state="unknown")
            return None

        state = str(job.get("state") or "unknown")
        progress = job.get("progress") or {}
        vals["update_state"] = state
        vals["update_progress"] = int(progress.get("percent") or 0)
        vals["update_description"] = str(progress.get("description") or "")

        if state not in APP_UPDATE_JOB_ACTIVE_STATES:
            if state != "SUCCESS":
                _LOGGER.error(
                    "Upgrade job %s for app %s on %s finished with state %s: %s",
                    job_id,
                    uid,
                    self.host,
                    state,
                    job.get("error") or "no error message",
                )
            self._reset_app_update_job(vals, state=state)
        return job

    @staticmethod
    def _reset_app_update_job(vals: dict[str, Any], *, state: str) -> None:
        """Stop tracking an app upgrade job.

        Only the job id is cleared; the final state, progress and description
        stay visible in the app data for troubleshooting until the next
        upgrade starts.
        """
        vals["update_jobid"] = 0
        vals["update_state"] = state

    # ---------------------------
    #   app stats subscription helpers
    # ---------------------------
    # Subscription lifecycle:
    #   UNSUBSCRIBED: _app_stats_sub_id is None.
    #   SUBSCRIBED:   _app_stats_sub_id and _app_stats_event_name are set together.
    #   stop_app_stats clears both, unconditionally.
    #   get_app_stats re-enters start_app_stats when is_subscribed returns False.
    def _set_app_stats_subscription(
        self, sub_id: str | None, event_name: str | None
    ) -> None:
        """Atomically set the app.stats subscription metadata."""
        self._app_stats_sub_id = sub_id
        self._app_stats_event_name = event_name

    def _clear_app_stats_subscription(self) -> None:
        """Clear the app.stats subscription metadata."""
        self._app_stats_sub_id = None
        self._app_stats_event_name = None

    def _get_app_identifier(self, app: dict[str, Any]) -> str | None:
        """Return the canonical app identifier used for stats and group membership.

        Prefers ``name``, falls back to ``app_name`` for legacy payloads.
        """
        name = app.get("name")
        if isinstance(name, str) and name:
            return name
        app_name = app.get("app_name")
        return app_name if isinstance(app_name, str) and app_name else None

    # ---------------------------
    #   start_app_stats
    # ---------------------------
    async def start_app_stats(self) -> None:
        """Initialize the app.stats subscription."""
        if not self._is_group_monitored(MONITOR_GROUP_CONTAINERS):
            _LOGGER.debug("start_app_stats: containers group not monitored, skipping")
            await self._stop_app_stats_if_active()
            self.ds["app_stats"] = {}
            return

        if not self.api.connected():
            _LOGGER.debug("start_app_stats: API not connected, skipping")
            return

        event_name = self._resolve_app_stats_event_name()
        await self._maybe_teardown_changed_app_stats_subscription(event_name)
        await self._maybe_clear_inactive_app_stats_subscription()

        if not self._app_stats_sub_id:
            _LOGGER.debug(
                "start_app_stats: no active subscription, subscribing to %s",
                event_name,
            )
            await self._subscribe_to_app_stats(event_name)
        else:
            _LOGGER.debug(
                "start_app_stats: subscription already active (%s)",
                self._app_stats_sub_id,
            )

    async def _stop_app_stats_if_active(self) -> None:
        """Stop app.stats subscription only if one is currently active."""
        if self._app_stats_sub_id:
            await self.stop_app_stats(force=True)

    def _resolve_app_stats_event_name(self) -> str:
        """Compute the app.stats event name from the current poll interval."""
        try:
            poll = int(
                getattr(self.config_entry, "options", {}).get(
                    CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                )
            )
        except (ValueError, TypeError):
            poll = DEFAULT_POLL_INTERVAL
        interval = max(poll, 2)
        return f'app.stats:{{"interval": {interval}}}'

    async def _maybe_teardown_changed_app_stats_subscription(
        self, event_name: str
    ) -> None:
        """Tear down the existing subscription if the event definition changed."""
        if self._app_stats_event_name and self._app_stats_event_name != event_name:
            await self.stop_app_stats(force=True)

    async def _maybe_clear_inactive_app_stats_subscription(self) -> None:
        """Clear local subscription state if the existing sub is no longer active."""
        if self._app_stats_sub_id and not await self.api.is_subscribed(
            self._app_stats_sub_id
        ):
            self._clear_app_stats_subscription()

    async def _subscribe_to_app_stats(self, event_name: str) -> None:
        """Attempt to establish a new app.stats subscription."""
        try:
            sub_id, queue = await self.api.subscribe_events(event_name)
            if sub_id and queue is not None:
                self._set_app_stats_subscription(sub_id, event_name)
                _LOGGER.debug("TrueNAS app.stats subscription established: %s", sub_id)
            else:
                _LOGGER.debug(
                    "TrueNAS app.stats subscription failed: no sub_id/queue returned"
                )
        except Exception as err:
            _LOGGER.exception("Failed to establish app.stats subscription: %s", err)

    # ---------------------------
    #   get_app_stats
    # ---------------------------
    async def get_app_stats(self) -> None:
        """Process buffered app.stats events and update state."""
        if not self._is_group_monitored(MONITOR_GROUP_CONTAINERS):
            _LOGGER.debug(
                "get_app_stats: containers group not monitored, clearing app_stats"
            )
            if self._app_stats_sub_id:
                await self.stop_app_stats(force=True)
            self.ds["app_stats"] = {}
            # Containers group unmonitored; tear down and clear state.
            return

        if not self._app_stats_sub_id or not await self.api.is_subscribed(
            self._app_stats_sub_id
        ):
            _LOGGER.debug(
                "get_app_stats: no active subscription, re-entering start_app_stats"
            )
            # Existing sub missing or inactive; re-enters start_app_stats.
            await self.start_app_stats()
            if not self._app_stats_sub_id:
                _LOGGER.debug(
                    "get_app_stats: subscription not established, skipping event fetch"
                )
                return

        if not self.api.connected():
            # Cannot fetch events while disconnected; skip this poll cycle.
            return

        if not self.ds.get("app"):
            # No apps to collect stats for; skip event fetch.
            return

        messages = await self.api.get_subscription_events(self._app_stats_sub_id)
        self._process_app_stats_messages(messages)

        current_app_names = self._collect_current_app_names()
        self._prune_stale_app_stats(current_app_names)
        # Remove cached app_stats entries whose app no longer exists.

    def _process_app_stats_messages(self, messages: list[dict[str, Any]]) -> None:
        """Append/update app_stats entries from buffered WebSocket messages."""
        _LOGGER.debug("Processing %d app.stats messages", len(messages))
        for msg in messages:
            params = _unwrap_app_stats_message(msg)
            if params is None:
                _LOGGER.debug(
                    "Skipping app.stats message with no unwrappable fields: %s",
                    _summarize_payload(msg),
                )
                continue
            fields_list = params.get("fields", [])
            if not isinstance(fields_list, list):
                _LOGGER.debug(
                    "Skipping app.stats message with non-list fields: %s",
                    _summarize_payload(msg),
                )
                continue
            for app in fields_list:
                self._upsert_app_stats_entry(app)

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        """Defensively coerce a value to float, returning None on invalid/missing."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _upsert_app_stats_entry(self, app: object) -> None:
        """Validate and store one app.stats entry."""
        if not isinstance(app, dict):
            _LOGGER.debug("Skipping non-dict app.stats entry: %r", app)
            return
        app_name = self._get_app_identifier(app)
        if not isinstance(app_name, str) or not app_name:
            _LOGGER.debug(
                "Skipping app.stats entry with missing/invalid app_name: %r",
                app,
            )
            return

        blkio_raw = app.get("blkio")
        if isinstance(blkio_raw, dict):
            blkio_read = self._coerce_float(blkio_raw.get("read"))
            blkio_write = self._coerce_float(blkio_raw.get("write"))
        else:
            blkio_read = blkio_write = None

        networks = self._filter_app_networks(app.get("networks", []))
        if not networks:
            networks = self._stale_app_networks(app_name)

        cpu_usage = self._coerce_float(app.get("cpu_usage"))
        memory = self._coerce_float(app.get("memory"))

        self.ds["app_stats"][app_name] = {
            "app_name": app_name,
            "cpu_usage": cpu_usage,
            "memory": memory,
            "blkio_read": blkio_read,
            "blkio_write": blkio_write,
            "networks": networks,
        }

    @staticmethod
    def _filter_app_networks(networks: Any) -> list[dict[str, Any]]:
        """Return only well-formed network entries (dicts with an interface_name)."""
        if not isinstance(networks, list):
            return []
        return [
            net
            for net in networks
            if isinstance(net, dict) and bool(net.get("interface_name"))
        ]

    def _stale_app_networks(self, app_name: str) -> list[dict[str, Any]]:
        """Return the app's last known interfaces as stale stubs.

        A stopped app reports no networks in ``app.stats``. Dropping the
        interfaces would make the orphan cleanup delete the per-interface
        sensors from the entity registry on every stop (and re-create them on
        start, losing history and customisations). Instead the interfaces are
        kept with ``None`` values and ``stale=True`` so the sensors survive and
        merely become unavailable until the app reports live traffic again.
        """
        previous = self._filter_app_networks(
            self.ds.get("app_stats", {}).get(app_name, {}).get("networks")
        )
        return [
            {
                "interface_name": net["interface_name"],
                "rx_bytes": None,
                "tx_bytes": None,
                "stale": True,
            }
            for net in previous
        ]

    def _collect_current_app_names(self) -> set[str]:
        """App names currently present in the app data."""
        current_app_names: set[str] = set()
        for vals in self.ds["app"].values():
            if isinstance(vals, dict):
                name = self._get_app_identifier(vals)
                if isinstance(name, str) and name:
                    current_app_names.add(name)
        return current_app_names

    def _prune_stale_app_stats(self, current_app_names: set[str]) -> None:
        """Remove cached app_stats entries whose app no longer exists."""
        if stale := [
            name for name in self.ds["app_stats"] if name not in current_app_names
        ]:
            _LOGGER.debug("Pruning stale app_stats entries: %s", stale)
            for app_name in stale:
                del self.ds["app_stats"][app_name]

    # ---------------------------
    #   stop_app_stats
    # ---------------------------
    # force=True by default so local subscription metadata is always cleared
    # on unload, even when the API is disconnected.
    async def stop_app_stats(self, force: bool = True) -> None:
        """Stop the app.stats subscription on unload."""
        if self._app_stats_sub_id and self.api.connected():
            try:
                await self.api.unsubscribe_events(self._app_stats_sub_id)
            except Exception as exc:
                _LOGGER.debug(
                    "TrueNAS failed to unsubscribe app.stats %s (%s)",
                    self._app_stats_sub_id,
                    exc,
                )
            self._app_stats_sub_id = None
            self._app_stats_event_name = None
        elif force:
            # Metadata is cleared unconditionally so the coordinator does not
            # believe it is still subscribed after a disconnect/reconnect cycle.
            self._app_stats_sub_id = None
            self._app_stats_event_name = None

    # ---------------------------
    #   get_cronjob
    # ---------------------------
    async def get_cronjob(self) -> None:
        """Get cronjobs via the aiotruenas domain layer.

        ``TrueNASState.get_cronjob()`` already derives ``display_name``; the
        "skip disabled" filter stays here since it is an HA options-flow
        behavior, not TrueNAS normalization.
        """
        if not self._is_group_monitored(MONITOR_GROUP_CRONJOBS):
            self.ds["cronjob"] = {}
            return
        cronjobs = _as_str_keyed(await self.state.get_cronjob())

        behaviors = self.config_entry.options.get(CONF_BEHAVIORS)
        if behaviors is not None:
            skip_disabled = BEHAVIOR_SKIP_DISABLED_CRONJOBS in behaviors
        else:
            skip_disabled = self.config_entry.options.get(
                "cronjob_skip_disabled",
                self.config_entry.data.get("cronjob_skip_disabled", True),
            )

        self.ds["cronjob"] = {
            uid: vals
            for uid, vals in cronjobs.items()
            if not skip_disabled or vals.get("enabled", True)
        }
