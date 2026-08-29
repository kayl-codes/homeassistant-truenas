"""TrueNAS update platform."""

from __future__ import annotations

import asyncio
from logging import getLogger
from time import monotonic
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
                    "error": str(self.coordinator.api.error or "unknown error"),
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
        if app_data.get("state") != "RUNNING":
            _LOGGER.error(
                "In order to upgrade the app %s, it must be in the RUNNING state.",
                self._data["id"],
            )
            return

        job_id = await self.coordinator.api.query("app.upgrade", [self._data["id"]])
        if job_id is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="app_update_failed",
                translation_placeholders={
                    "app": self._data["id"],
                    "host": self.coordinator.host,
                    "error": str(self.coordinator.api.error or "unknown error"),
                },
            )

        self._data["update_jobid"] = job_id
        self._data["update_state"] = "RUNNING"
        self._data["update_progress"] = 0
        self._data["update_description"] = ""
        self.async_write_ha_state()

        job = await self._async_track_upgrade_job()
        # Re-sync versions/state from TrueNAS now that the job is done.
        await self.coordinator.async_request_refresh()

        if job is not None and job.get("state") != "SUCCESS":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="app_update_job_failed",
                translation_placeholders={
                    "app": self._data["id"],
                    "host": self.coordinator.host,
                    "error": str(job.get("error") or job.get("state") or "unknown"),
                },
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
