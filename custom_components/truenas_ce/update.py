"""TrueNAS update platform."""

from __future__ import annotations

from logging import getLogger
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import TrueNASCoordinator
from .entity import TrueNASEntity, async_add_entities
from .update_types import SENSOR_SERVICES, SENSOR_TYPES  # noqa: F401

_LOGGER = getLogger(__name__)
DEVICE_UPDATE = "device_update"


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

    TYPE = DEVICE_UPDATE
    _attr_device_class = UpdateDeviceClass.FIRMWARE

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        entity_description,
        uid: str | None = None,
    ):
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

    async def async_install(self, _version: str, backup: bool, **kwargs: Any) -> None:
        """Install the latest available update.

        The version parameter is currently ignored; TrueNAS API only supports
        installing the latest available firmware via update.update.
        """
        job_id = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "update.update",
            {"reboot": True},
        )
        if job_id is None:
            _LOGGER.error("Failed to start TrueNAS system update")
            return

        self._data["update_jobid"] = job_id
        await self.coordinator.async_refresh()

    @property
    def in_progress(self) -> int | bool:
        """Update installation progress."""
        if self._data.get("update_state") != "RUNNING":
            return False

        return int(self._data.get("update_progress", 0))


# ---------------------------
#   TrueNASAppUpdate
# ---------------------------
class TrueNASAppUpdate(TrueNASEntity, UpdateEntity):
    """Define an TrueNAS App Update Sensor."""

    TYPE = DEVICE_UPDATE

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        entity_description,
        uid: str | None = None,
    ):
        """Set up device update entity."""
        super().__init__(coordinator, entity_description, uid)

        self._attr_supported_features = UpdateEntityFeature.INSTALL

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
        installed = self._data.get("version")
        if not self._data.get("update_available"):
            return installed

        latest = self._data.get("latest_version")
        if not latest or latest in ("unknown", installed):
            return f"{installed} (image update)" if installed else "image update"
        return latest

    async def async_install(self, _version: str, backup: bool, **kwargs: Any) -> None:
        """Install an update."""
        app_data = self.coordinator.data.get("app", {}).get(self._data["id"], {})
        if app_data.get("state") != "RUNNING":
            _LOGGER.error(
                "In order to upgrade the app %s, it must be in the RUNNING state.",
                self._data["id"],
            )
            return

        job_id = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "app.upgrade",
            [self._data["id"]],
        )
        if job_id is None:
            _LOGGER.error("Failed to start TrueNAS app update for %s", self._data["id"])
            return

        self._data["update_jobid"] = job_id
        await self.coordinator.async_refresh()

    @property
    def in_progress(self) -> bool:
        """Return if update is in progress."""
        return bool(self._data.get("update_jobid"))

    @property
    def title(self) -> str | None:
        """Return the title of the entity."""
        return self._data.get("name")
