"""TrueNAS binary sensor platform."""

from __future__ import annotations

from logging import getLogger

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .binary_sensor_types import (  # noqa: F401
    SENSOR_SERVICES,
    SENSOR_TYPES,
)
from .const import VIRT_INSTANCE_STOP_OPTIONS
from .entity import TrueNASEntity, async_add_entities

_LOGGER = getLogger(__name__)

_LOG_SERVICE_INVALID = "Service %s (%s) invalid"
_LOG_SERVICE_NOT_RUNNING = "Service %s (%s) is not running"


# ---------------------------
#   async_setup_entry
# ---------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    _async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TrueNAS binary sensors."""
    dispatcher = {
        "TrueNASBinarySensor": TrueNASBinarySensor,
        "TrueNASVMBinarySensor": TrueNASVMBinarySensor,
        "TrueNASContainerBinarySensor": TrueNASContainerBinarySensor,
        "TrueNASServiceBinarySensor": TrueNASServiceBinarySensor,
        "TrueNASAppBinarySensor": TrueNASAppBinarySensor,
    }
    await async_add_entities(hass, config_entry, dispatcher)


# ---------------------------
#   TrueNASBinarySensor
# ---------------------------
class TrueNASBinarySensor(TrueNASEntity, BinarySensorEntity):
    """Define an TrueNAS Binary Sensor."""

    @property
    def is_on(self) -> bool | None:
        """Return true if device is on.

        Uses .get() so a transient API failure that empties the coordinator data
        degrades the state to unknown instead of raising a KeyError mid-update.
        """
        return self._data.get(self.entity_description.data_is_on)

    @property
    def icon(self) -> str | None:
        """Return the icon."""
        if self.entity_description.icon_enabled:
            if self._data.get(self.entity_description.data_is_on):
                return self.entity_description.icon_enabled
            else:
                return self.entity_description.icon_disabled

        return None


# ---------------------------
#   TrueNASVMBinarySensor
# ---------------------------
class TrueNASVMBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS VM Binary Sensor."""

    async def start(self, overcommit: bool = False):
        """Start a VM."""  # vm.start
        tmp_vm = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "vm.get_instance",
            [self._data["id"]],
        )

        state = (
            tmp_vm.get("status", {}).get("state") if isinstance(tmp_vm, dict) else None
        )
        if not state:
            _LOGGER.error("VM %s (%s) invalid", self._data["name"], self._data["id"])
            return

        if state != "STOPPED":
            _LOGGER.warning(
                "VM %s (%s) is not down", self._data["name"], self._data["id"]
            )
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "vm.start",
            [self._data["id"], {"overcommit": overcommit}],
        )

    async def stop(self):
        """Stop a VM."""
        tmp_vm = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "vm.get_instance",
            [self._data["id"]],
        )

        state = (
            tmp_vm.get("status", {}).get("state") if isinstance(tmp_vm, dict) else None
        )
        if not state:
            _LOGGER.error("VM %s (%s) invalid", self._data["name"], self._data["id"])
            return

        if state != "RUNNING":
            _LOGGER.warning(
                "VM %s (%s) is not up", self._data["name"], self._data["id"]
            )
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "vm.stop",
            [self._data["id"], {"force": True, "force_after_timeout": True}],
        )

    async def restart(self):
        """Restart a VM."""  # vm.restart
        # A restart always applies (no state guard): it stops and starts again.
        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "vm.restart",
            [self._data["id"]],
        )
        await self.coordinator.async_request_refresh()


# ---------------------------
#   TrueNASContainerBinarySensor
# ---------------------------
class TrueNASContainerBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS Container (virt instance) Binary Sensor."""

    async def _current_status(self) -> str | None:
        """Return the container's live status, or None if it can't be determined.

        The cached coordinator status is stale right after a stop/start (until the
        next poll), so the start/stop guards query the current state via
        ``virt.instance.query`` (the response shape is known: top-level ``status``).
        A transient query failure returns None so the caller proceeds (fail-safe).
        """
        try:
            instances = await self.hass.async_add_executor_job(
                self.coordinator.api.query,
                "virt.instance.query",
                [[["id", "=", self._data["id"]]]],
            )
        except Exception:
            _LOGGER.exception(
                "Failed to query status for container %s", self._data.get("name")
            )
            return None
        instance = instances[0] if isinstance(instances, list) and instances else None
        return instance.get("status") if isinstance(instance, dict) else None

    async def start(self):
        """Start a container."""  # virt.instance.start
        # Only skip when positively running; if the status is unknown, proceed.
        if await self._current_status() == "RUNNING":
            _LOGGER.warning("Container %s is already running", self._data.get("name"))
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "virt.instance.start",
            [self._data["id"]],
        )
        await self.coordinator.async_request_refresh()

    async def stop(self):
        """Stop a container."""  # virt.instance.stop
        # Only skip when positively not running; if unknown, proceed.
        status = await self._current_status()
        if status is not None and status != "RUNNING":
            _LOGGER.warning("Container %s is not running", self._data.get("name"))
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "virt.instance.stop",
            [self._data["id"], VIRT_INSTANCE_STOP_OPTIONS],
        )
        await self.coordinator.async_request_refresh()

    async def restart(self):
        """Restart a container."""  # virt.instance.restart
        # A restart always applies (no state guard): it stops and starts again.
        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "virt.instance.restart",
            [self._data["id"], VIRT_INSTANCE_STOP_OPTIONS],
        )
        await self.coordinator.async_request_refresh()


# ---------------------------
#   TrueNASServiceBinarySensor
# ---------------------------
class TrueNASServiceBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS Service Binary Sensor."""

    async def _get_service(self):
        """Return the latest service state from the API."""
        services = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "service.query",
            [[["id", "=", self._data["id"]]]],
        )
        return services[0] if isinstance(services, list) and services else None

    async def start(self):
        """Start a Service."""
        tmp_service = await self._get_service()

        if not isinstance(tmp_service, dict) or "state" not in tmp_service:
            _LOGGER.error(_LOG_SERVICE_INVALID, self._data["service"], self._data["id"])
            return

        if tmp_service["state"] != "STOPPED":
            _LOGGER.warning(
                "Service %s (%s) is not stopped",
                self._data["service"],
                self._data["id"],
            )
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "service.start",
            [self._data["service"]],
        )

        await self.coordinator.async_refresh()

    async def stop(self):
        """Stop a Service."""
        tmp_service = await self._get_service()

        if not isinstance(tmp_service, dict) or "state" not in tmp_service:
            _LOGGER.error(_LOG_SERVICE_INVALID, self._data["service"], self._data["id"])
            return

        if tmp_service["state"] == "STOPPED":
            _LOGGER.warning(
                _LOG_SERVICE_NOT_RUNNING,
                self._data["service"],
                self._data["id"],
            )
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "service.stop",
            [self._data["service"]],
        )
        await self.coordinator.async_refresh()

    async def restart(self):
        """Restart a Service."""
        tmp_service = await self._get_service()

        if not isinstance(tmp_service, dict) or "state" not in tmp_service:
            _LOGGER.error(_LOG_SERVICE_INVALID, self._data["service"], self._data["id"])
            return

        if tmp_service["state"] == "STOPPED":
            _LOGGER.warning(
                _LOG_SERVICE_NOT_RUNNING,
                self._data["service"],
                self._data["id"],
            )
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "service.restart",
            [self._data["service"]],
        )

        await self.coordinator.async_refresh()

    async def reload(self):
        """Reload a Service."""
        tmp_service = await self._get_service()

        if not isinstance(tmp_service, dict) or "state" not in tmp_service:
            _LOGGER.error(_LOG_SERVICE_INVALID, self._data["service"], self._data["id"])
            return

        if tmp_service["state"] == "STOPPED":
            _LOGGER.warning(
                _LOG_SERVICE_NOT_RUNNING,
                self._data["service"],
                self._data["id"],
            )
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "service.reload",
            [self._data["service"]],
        )

        await self.coordinator.async_refresh()


# ---------------------------
#   TrueNASAppsBinarySensor
# ---------------------------
class TrueNASAppBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS Applications Binary Sensor."""

    async def start(self):
        """Start an App."""
        tmp_app = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "app.get_instance",
            [self._data["id"]],
        )

        if tmp_app is None or "state" not in tmp_app:
            _LOGGER.error("App %s (%s) invalid", self._data["name"], self._data["id"])
            return

        if tmp_app["state"] == "RUNNING":
            _LOGGER.warning(
                "App %s (%s) is not down", self._data["name"], self._data["id"]
            )
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "app.start",
            [self._data["id"]],
        )

    async def stop(self):
        """Stop an App."""
        tmp_app = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "app.get_instance",
            [self._data["id"]],
        )

        if tmp_app is None or "state" not in tmp_app:
            _LOGGER.error("App %s (%s) invalid", self._data["name"], self._data["id"])
            return

        if tmp_app["state"] != "RUNNING":
            _LOGGER.warning(
                "App %s (%s) is not up", self._data["name"], self._data["id"]
            )
            return

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "app.stop",
            [self._data["id"]],
        )
