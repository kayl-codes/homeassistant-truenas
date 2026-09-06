"""TrueNAS binary sensor platform."""

from __future__ import annotations

from logging import getLogger
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .binary_sensor_types import (  # noqa: F401
    SENSOR_SERVICES,
    SENSOR_TYPES,
    TrueNASBinarySensorEntityDescription,
)
from .const import (
    CONTAINER_STOP_OPTIONS,
    VIRT_INSTANCE_STOP_OPTIONS,
)
from .entity import TrueNASEntity, async_add_entities

_LOGGER = getLogger(__name__)

# Updates are centralized in the coordinator; entity actions may run unlimited.
PARALLEL_UPDATES = 0

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

    entity_description: TrueNASBinarySensorEntityDescription

    @property
    def is_on(self) -> bool | None:
        """Return true if device is on.

        Uses .get() so a transient API failure that empties the coordinator data
        degrades the state to unknown instead of raising a KeyError mid-update.
        """
        value: bool | None = self._data.get(self.entity_description.data_is_on)
        return value


# ---------------------------
#   TrueNASVMBinarySensor
# ---------------------------
class TrueNASVMBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS VM Binary Sensor."""

    async def start(self, overcommit: bool = False) -> None:
        """Start a VM."""  # vm.start
        tmp_vm = await self.coordinator.api.query("vm.get_instance", [self._data["id"]])
        self._raise_if_api_error("start")

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

        await self.coordinator.api.query(
            "vm.start", [self._data["id"], {"overcommit": overcommit}]
        )
        self._raise_if_api_error("start")

    async def stop(self, force: bool = False) -> None:
        """Stop a VM."""
        tmp_vm = await self.coordinator.api.query("vm.get_instance", [self._data["id"]])
        self._raise_if_api_error("stop")

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

        await self.coordinator.api.query(
            "vm.stop", [self._data["id"], {"force": force, "force_after_timeout": True}]
        )
        self._raise_if_api_error("stop")

    async def restart(self) -> None:
        """Restart a VM."""  # vm.restart
        # A restart always applies (no state guard): it stops and starts again.
        await self.coordinator.api.query("vm.restart", [self._data["id"]])
        self._raise_if_api_error("restart")
        await self.coordinator.async_request_refresh()


# ---------------------------
#   TrueNASContainerBinarySensor
# ---------------------------
class TrueNASContainerBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS Container Binary Sensor.

    Containers are Incus ``virt.instance.*`` objects up to TrueNAS 25.x and
    LXC ``container.*`` objects from 26.0 on; the coordinator's
    ``supports_container_api`` picks the namespace.
    """

    async def _current_status(self) -> str | None:
        """Return the container's live status, or None if it can't be determined.

        The cached coordinator status is stale right after a stop/start (until the
        next poll), so the start/stop guards query the current state directly.
        A transient query failure returns None so the caller proceeds (fail-safe).
        """
        v26 = self.coordinator.supports_container_api()
        method = "container.query" if v26 else "virt.instance.query"
        try:
            instances = await self.coordinator.api.query(
                method, [[["id", "=", self._data["id"]]]]
            )
        except Exception:
            _LOGGER.exception(
                "Failed to query status for container %s via %s",
                self._data.get("name"),
                method,
            )
            return None
        instance = instances[0] if isinstance(instances, list) and instances else None
        if not isinstance(instance, dict):
            return None
        status = instance.get("status")
        if v26:
            # container.* nests the state: {"state": "RUNNING", ...}
            return status.get("state") if isinstance(status, dict) else None
        return status if isinstance(status, str) else None

    async def start(self) -> None:
        """Start a container."""
        # Only skip when positively running; if the status is unknown, proceed.
        if await self._current_status() == "RUNNING":
            _LOGGER.warning("Container %s is already running", self._data.get("name"))
            return

        method = (
            "container.start"
            if self.coordinator.supports_container_api()
            else "virt.instance.start"
        )
        await self.coordinator.api.query(method, [self._data["id"]])
        self._raise_if_api_error("start")
        await self.coordinator.async_request_refresh()

    async def stop(self) -> None:
        """Stop a container."""
        # Only skip when positively not running; if unknown, proceed.
        status = await self._current_status()
        if status is not None and status != "RUNNING":
            _LOGGER.warning("Container %s is not running", self._data.get("name"))
            return

        if self.coordinator.supports_container_api():
            await self.coordinator.api.query(
                "container.stop", [self._data["id"], CONTAINER_STOP_OPTIONS]
            )
        else:
            await self.coordinator.api.query(
                "virt.instance.stop", [self._data["id"], VIRT_INSTANCE_STOP_OPTIONS]
            )
        self._raise_if_api_error("stop")
        await self.coordinator.async_request_refresh()

    async def restart(self) -> None:
        """Restart a container."""
        # A restart always applies (no state guard): it stops and starts again.
        if self.coordinator.supports_container_api():
            # container.* has no restart method: wait for the stop job, then start.
            await self.coordinator.api.query(
                "container.stop", [self._data["id"], CONTAINER_STOP_OPTIONS], job=True
            )
            # Abort before starting if the stop failed; the trailing check
            # below then covers the start call.
            self._raise_if_api_error("restart")
            await self.coordinator.api.query("container.start", [self._data["id"]])
        else:
            await self.coordinator.api.query(
                "virt.instance.restart",
                [self._data["id"], VIRT_INSTANCE_STOP_OPTIONS],
            )
        self._raise_if_api_error("restart")
        await self.coordinator.async_request_refresh()


# ---------------------------
#   TrueNASServiceBinarySensor
# ---------------------------
class TrueNASServiceBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS Service Binary Sensor."""

    async def _get_service(self, action: str) -> dict[str, Any] | None:
        """Return the latest service state from the API."""
        services = await self.coordinator.api.query(
            "service.query", [[["id", "=", self._data["id"]]]]
        )
        self._raise_if_api_error(action)
        service: dict[str, Any] | None = (
            services[0] if isinstance(services, list) and services else None
        )
        return service

    async def start(self) -> None:
        """Start a Service."""
        tmp_service = await self._get_service("start")

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

        await self._control_service("START")
        self._raise_if_api_error("start")

        await self.coordinator.async_refresh()

    async def stop(self) -> None:
        """Stop a Service."""
        tmp_service = await self._get_service("stop")

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

        await self._control_service("STOP")
        self._raise_if_api_error("stop")
        await self.coordinator.async_refresh()

    async def restart(self) -> None:
        """Restart a Service."""
        tmp_service = await self._get_service("restart")

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

        await self._control_service("RESTART")
        self._raise_if_api_error("restart")

        await self.coordinator.async_refresh()

    async def reload(self) -> None:
        """Reload a Service."""
        tmp_service = await self._get_service("reload")

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

        await self._control_service("RELOAD")
        self._raise_if_api_error("reload")

        await self.coordinator.async_refresh()


# ---------------------------
#   TrueNASAppsBinarySensor
# ---------------------------
class TrueNASAppBinarySensor(TrueNASBinarySensor):
    """Define a TrueNAS Applications Binary Sensor."""

    async def start(self) -> None:
        """Start an App."""
        tmp_app = await self.coordinator.api.query(
            "app.get_instance", [self._data["id"]]
        )
        self._raise_if_api_error("start")

        if tmp_app is None or "state" not in tmp_app:
            _LOGGER.error("App %s (%s) invalid", self._data["name"], self._data["id"])
            return

        if tmp_app["state"] == "RUNNING":
            _LOGGER.warning(
                "App %s (%s) is not down", self._data["name"], self._data["id"]
            )
            return

        await self.coordinator.api.query("app.start", [self._data["id"]])
        self._raise_if_api_error("start")

    async def stop(self) -> None:
        """Stop an App."""
        tmp_app = await self.coordinator.api.query(
            "app.get_instance", [self._data["id"]]
        )
        self._raise_if_api_error("stop")

        if tmp_app is None or "state" not in tmp_app:
            _LOGGER.error("App %s (%s) invalid", self._data["name"], self._data["id"])
            return

        if tmp_app["state"] != "RUNNING":
            _LOGGER.warning(
                "App %s (%s) is not up", self._data["name"], self._data["id"]
            )
            return

        await self.coordinator.api.query("app.stop", [self._data["id"]])
        self._raise_if_api_error("stop")
