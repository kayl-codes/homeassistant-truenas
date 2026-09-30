"""TrueNAS update platform."""

from __future__ import annotations

import asyncio
from logging import getLogger
from time import monotonic
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    APP_UPDATE_JOB_ACTIVE_STATES,
    APP_UPDATE_JOB_POLL_INTERVAL,
    APP_UPDATE_JOB_TIMEOUT,
    DOMAIN,
)
from .coordinator import TrueNASCoordinator
from .entity import TrueNASEntity, async_add_entities
from .update_types import (  # noqa: F401
    SENSOR_SERVICES,
    SENSOR_TYPES,
    TrueNASUpdateEntityDescription,
)

_LOGGER = getLogger(__name__)
DEVICE_UPDATE = "device_update"

_APP_RUNNING = "RUNNING"
_UNKNOWN_ERROR = "unknown error"
_TRACEBACK_MARKER = "Traceback (most recent call last):"
_EXCEPTION_SUFFIXES = ("Error", "Exception")
_UPDATE_FAILED_NOTIFY_PREFIX = "truenas_ce_app_update_failed"
# Stand-in job when TrueNAS no longer reports the upgrade job at all.
_JOB_OUTCOME_UNKNOWN: dict[str, Any] = {
    "state": "UNKNOWN",
    "error": (
        "TrueNAS no longer reports the upgrade job, so its outcome is unknown; "
        "please check the app in TrueNAS."
    ),
}

# Outcomes of the post-failure restart attempt, rendered in the notification.
_RESTART_NOT_NEEDED = "The app is still running."
_APP_RESTARTABLE_STATES: frozenset[str] = frozenset({"STOPPED", "CRASHED"})
_RESTART_TRIGGERED = (
    "The app was stopped by the failed update; a restart has been requested. "
    "Please verify in TrueNAS that it is running again."
)
_RESTART_TRANSITIONING = (
    "The app is currently {state}; please check in TrueNAS that it comes back up."
)
_RESTART_FAILED = (
    "The app was stopped by the failed update and could not be restarted "
    "automatically; please start it manually in TrueNAS."
)
_RESTART_UNKNOWN = "The app state could not be determined; please check it in TrueNAS."


def summarize_job_error(error: Any) -> str:
    """Reduce a TrueNAS job error to a human-readable reason.

    Failed chart migrations embed a full Python traceback. Keep the leading
    context (e.g. "[EFAULT] Failed to execute 'x' migration") and the message
    of the final exception line; plain errors are returned unchanged.
    """
    text = str(error or "").strip()
    head, marker, tail = text.partition(_TRACEBACK_MARKER)
    if not marker:
        return text

    head = head.strip().rstrip(":").strip()
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    # The exception message starts at the last "SomeError: ..." line and may
    # continue over further lines; fall back to the very last line.
    reason = lines[-1] if lines else ""
    for index in range(len(lines) - 1, -1, -1):
        if message := _exception_message(lines[index]):
            reason = " ".join([message, *lines[index + 1 :]])
            break
    return f"{head}: {reason}" if head and reason else head or reason


def _exception_message(line: str) -> str | None:
    """Return the message of a final traceback line, else ``None``.

    Matches e.g. "Exception: <reason>" or "middlewared.CallError: <reason>".
    """
    name, sep, message = line.partition(":")
    message = message.strip()
    if (
        sep
        and message
        and name.endswith(_EXCEPTION_SUFFIXES)
        and name.replace(".", "_").isidentifier()
    ):
        return message
    return None


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
    """Set up device tracker for TrueNAS component."""
    dispatcher = {
        "TrueNASUpdate": TrueNASUpdate,
        "TrueNASAppUpdate": TrueNASAppUpdate,
    }
    await async_add_entities(hass, config_entry, dispatcher)


# ---------------------------
#   TrueNASUpdate
# ---------------------------
class TrueNASUpdate(TrueNASEntity, UpdateEntity):
    """Define an TrueNAS Update Sensor."""

    entity_description: TrueNASUpdateEntityDescription
    TYPE = DEVICE_UPDATE
    _attr_device_class = UpdateDeviceClass.FIRMWARE

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        entity_description: TrueNASUpdateEntityDescription,
        uid: str | None = None,
    ) -> None:
        """Set up device update entity."""
        super().__init__(coordinator, entity_description, uid)

        self._attr_supported_features = UpdateEntityFeature.INSTALL
        self._attr_supported_features |= UpdateEntityFeature.PROGRESS
        self._attr_title = self.entity_description.title

    @property
    def installed_version(self) -> str | None:
        """Version installed and in use."""
        return self._data.get("version")

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install."""
        return self._data.get("update_version")

    async def options_updated(self) -> None:
        """No action needed."""

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install the latest available update.

        The version parameter is currently ignored; TrueNAS API only supports
        installing the latest available firmware. TrueNAS 25.10 moved this
        from "update.update" (now settings-only) to "update.run".
        """
        method = (
            "update.run" if self.coordinator.supports_update_run() else "update.update"
        )
        job_id = await self.coordinator.api.query(method, {"reboot": True})
        if job_id is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="system_update_failed",
                translation_placeholders={
                    "host": self.coordinator.host,
                    "error": str(self.coordinator.api.error or _UNKNOWN_ERROR),
                },
            )

        self._data["update_jobid"] = job_id
        await self.coordinator.async_refresh()

    @property
    def in_progress(self) -> bool:
        """Return whether an update installation is running."""
        return self._data.get("update_state") == "RUNNING"

    @property
    def update_percentage(self) -> int | None:
        """Update installation progress percentage."""
        if self._data.get("update_state") != "RUNNING":
            return None

        return int(self._data.get("update_progress", 0))


# ---------------------------
#   TrueNASAppUpdate
# ---------------------------
class TrueNASAppUpdate(TrueNASEntity, UpdateEntity):
    """Define an TrueNAS App Update Sensor."""

    entity_description: TrueNASUpdateEntityDescription
    TYPE = DEVICE_UPDATE

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        entity_description: TrueNASUpdateEntityDescription,
        uid: str | None = None,
    ) -> None:
        """Set up device update entity."""
        super().__init__(coordinator, entity_description, uid)

        self._attr_supported_features = (
            UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
        )

    @property
    def installed_version(self) -> str | None:
        """Version installed and in use."""
        return self._data.get("version")

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install.

        Home Assistant shows an update when latest != installed. For custom/
        compose apps there is no catalog version (latest_version is "unknown"),
        so reflect availability explicitly when an image update is pending.
        """
        installed: str | None = self._data.get("version")
        if not self._data.get("update_available"):
            return installed

        latest: str | None = self._data.get("latest_version")
        if not latest or latest in ("unknown", installed):
            return f"{installed} (image update)" if installed else "image update"
        return latest

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install an update."""
        app_data = self.coordinator.data.get("app", {}).get(str(self._data["id"]), {})
        if app_data.get("state") != _APP_RUNNING:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="app_update_not_running",
                translation_placeholders={
                    "app": str(self._data["id"]),
                    "state": str(app_data.get("state") or "unknown"),
                },
            )

        job_id = await self.coordinator.api.query("app.upgrade", [self._data["id"]])
        if job_id is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="app_update_failed",
                translation_placeholders={
                    "app": self._data["id"],
                    "host": self.coordinator.host,
                    "error": str(self.coordinator.api.error or _UNKNOWN_ERROR),
                },
            )

        self._data["update_jobid"] = job_id
        self._data["update_state"] = "RUNNING"
        self._data["update_progress"] = 0
        self._data["update_description"] = ""
        self.async_write_ha_state()

        job = await self._async_track_upgrade_job()
        if job is None and self._data.get("update_jobid"):
            # Tracking timed out while the job is still running: keep watching
            # it in the background so a late failure is still reported.
            self.coordinator.config_entry.async_create_background_task(
                self.hass,
                self._async_watch_late_upgrade(job_id),
                f"{DOMAIN} app upgrade watch {self._data['id']}",
            )
            await self.coordinator.async_request_refresh()
            return
        if job is None:
            # The coordinator's own poll/push pass saw the final state first and
            # stopped tracking; fetch the outcome so a failure is not lost.
            job = await self._async_resolve_untracked_job(job_id)
        # Re-sync versions/state from TrueNAS now that the job is done.
        await self.coordinator.async_request_refresh()

        reason = await self._async_handle_upgrade_outcome(job)
        if reason is None:
            return
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="app_update_job_failed",
            translation_placeholders={
                "app": self._data["id"],
                "host": self.coordinator.host,
                "error": reason,
            },
        )

    @property
    def _notification_id(self) -> str:
        """Stable notification id so repeated failures replace, not stack."""
        return (
            f"{_UPDATE_FAILED_NOTIFY_PREFIX}_"
            f"{self.coordinator.config_entry.entry_id}_{self._data['id']}"
        )

    async def _async_fetch_job(self, job_id: Any) -> dict[str, Any] | None:
        """Look up an upgrade job directly by id, or ``None`` if unavailable."""
        jobs = await self.coordinator.api.query(
            "core.get_jobs", params=[[["id", "=", job_id]]]
        )
        if isinstance(jobs, list) and jobs and isinstance(jobs[0], dict):
            return jobs[0]
        _LOGGER.warning(
            "Could not look up upgrade job %s for app %s on %s: %s",
            job_id,
            self._data["id"],
            self.coordinator.host,
            self.coordinator.api.error or "job not found",
        )
        return None

    async def _async_handle_upgrade_outcome(self, job: dict[str, Any]) -> str | None:
        """Act on a finished upgrade job.

        Returns ``None`` on success; on failure restarts the app if needed,
        notifies the user and returns the shortened failure reason.
        """
        if job.get("state") == "SUCCESS":
            persistent_notification.async_dismiss(self.hass, self._notification_id)
            return None
        reason = summarize_job_error(job.get("error")) or str(
            job.get("state") or "unknown"
        )
        restart_outcome = await self._async_restart_after_failed_upgrade()
        self._notify_update_failed(
            reason, restart_outcome, from_truenas=job is not _JOB_OUTCOME_UNKNOWN
        )
        return reason

    async def _async_watch_late_upgrade(self, job_id: Any) -> None:
        """Report the outcome of an upgrade that outlived the install call.

        The coordinator keeps mirroring the job on every poll and clears
        ``update_jobid`` once it ends (or vanishes); the outcome is then
        handled exactly like one seen within the install call.
        """
        uid = str(self._data["id"])
        finished = asyncio.Event()

        @callback
        def _check_job() -> None:
            app = self.coordinator.data.get("app", {}).get(uid)
            if not app or not app.get("update_jobid"):
                finished.set()

        unsubscribe = self.coordinator.async_add_listener(_check_job)
        try:
            _check_job()
            await finished.wait()
        finally:
            unsubscribe()

        if uid not in self.coordinator.data.get("app", {}):
            _LOGGER.warning(
                "App %s disappeared from %s while its upgrade job %s was running; "
                "its outcome cannot be reported",
                uid,
                self.coordinator.host,
                job_id,
            )
            return
        job = await self._async_resolve_untracked_job(job_id)
        if job.get("state") in APP_UPDATE_JOB_ACTIVE_STATES:
            _LOGGER.warning(
                "Lost track of upgrade job %s for app %s on %s while it was still "
                "%s; its outcome cannot be reported",
                job_id,
                uid,
                self.coordinator.host,
                job.get("state"),
            )
            return
        if await self._async_handle_upgrade_outcome(job) is not None:
            _LOGGER.warning(
                "Upgrade job %s for app %s on %s failed after the install call "
                "had returned; the user was notified",
                job_id,
                uid,
                self.coordinator.host,
            )

    async def _async_resolve_untracked_job(self, job_id: Any) -> dict[str, Any]:
        """Final outcome of a job the coordinator already stopped tracking.

        Falls back to the terminal state the coordinator recorded, so a
        transient lookup error cannot turn a successful upgrade into a failure;
        only a genuinely unknown outcome yields the ``UNKNOWN`` stand-in.
        """
        if job := await self._async_fetch_job(job_id):
            return job
        known = str(self._data.get("update_state") or "unknown")
        if known == "unknown" or known in APP_UPDATE_JOB_ACTIVE_STATES:
            return _JOB_OUTCOME_UNKNOWN
        return {"state": known}

    async def _async_restart_after_failed_upgrade(self) -> str:
        """Bring the app back up if the failed upgrade left it stopped.

        The install pre-check guarantees the app was RUNNING before the
        upgrade, so starting it again restores the pre-update state and never
        starts an app the user stopped on purpose. Apps still transitioning
        (DEPLOYING/STOPPING) are left alone.
        """
        app_id = self._data["id"]
        instance = await self.coordinator.api.query("app.get_instance", [app_id])
        if not isinstance(instance, dict) or "state" not in instance:
            _LOGGER.warning(
                "Could not determine the state of app %s on %s after its "
                "upgrade failed: %s",
                app_id,
                self.coordinator.host,
                self.coordinator.api.error or "invalid response",
            )
            return _RESTART_UNKNOWN
        state = str(instance["state"])
        if state == _APP_RUNNING:
            return _RESTART_NOT_NEEDED
        if state not in _APP_RESTARTABLE_STATES:
            return _RESTART_TRANSITIONING.format(state=state)

        _LOGGER.warning(
            "App %s on %s is %s after its upgrade failed; starting it again",
            app_id,
            self.coordinator.host,
            state,
        )
        if await self.coordinator.api.query("app.start", [app_id]) is None:
            _LOGGER.error(
                "Failed to restart app %s on %s after its upgrade failed: %s",
                app_id,
                self.coordinator.host,
                self.coordinator.api.error or _UNKNOWN_ERROR,
            )
            return _RESTART_FAILED
        await self.coordinator.async_request_refresh()
        return _RESTART_TRIGGERED

    def _notify_update_failed(
        self, reason: str, restart_outcome: str, *, from_truenas: bool = True
    ) -> None:
        """Tell the user why the upgrade failed and what happened to the app."""
        name = self._data.get("name") or self._data["id"]
        target = self._data.get("latest_version")
        target_text = f" to version {target}" if target and target != "unknown" else ""
        label = "Reason reported by TrueNAS" if from_truenas else "Details"
        persistent_notification.async_create(
            self.hass,
            (
                f"The update of app **{name}**{target_text} on "
                f"{self.coordinator.host} failed.\n\n"
                f"**{label}:**\n{reason}\n\n"
                f"{restart_outcome}"
            ),
            title=f"TrueNAS app update failed: {name}",
            notification_id=self._notification_id,
        )

    async def _async_track_upgrade_job(self) -> dict[str, Any] | None:
        """Poll the running upgrade job and push progress to HA until it ends.

        Returns the final job dict, or ``None`` if the job vanished or tracking
        timed out (the coordinator keeps mirroring the job on its own cadence).
        """
        deadline = monotonic() + APP_UPDATE_JOB_TIMEOUT
        while self._data.get("update_jobid"):
            await asyncio.sleep(APP_UPDATE_JOB_POLL_INTERVAL)
            job = await self.coordinator.async_refresh_app_update_job(
                str(self._data["id"])
            )
            self.async_write_ha_state()
            if job is not None and job.get("state") not in APP_UPDATE_JOB_ACTIVE_STATES:
                return job
            if monotonic() >= deadline:
                _LOGGER.warning(
                    "Upgrade job %s for app %s is still running after %s s; "
                    "leaving progress tracking to the regular poll",
                    self._data.get("update_jobid"),
                    self._data["id"],
                    APP_UPDATE_JOB_TIMEOUT,
                )
                return None
        return None

    @property
    def in_progress(self) -> bool:
        """Return if update is in progress."""
        return bool(self._data.get("update_jobid"))

    @property
    def update_percentage(self) -> int | None:
        """Update installation progress percentage."""
        if not self.in_progress:
            return None
        return int(self._data.get("update_progress") or 0)

    @property
    def title(self) -> str | None:
        """Return the title of the entity."""
        return self._data.get("name")
