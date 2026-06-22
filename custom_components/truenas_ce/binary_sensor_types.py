"""Definitions for TrueNAS binary sensor entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntityDescription,
)
from homeassistant.helpers.entity import EntityCategory

from .const import (
    SCHEMA_SERVICE_APP_START,
    SCHEMA_SERVICE_APP_STOP,
    SCHEMA_SERVICE_CONTAINER_RESTART,
    SCHEMA_SERVICE_CONTAINER_START,
    SCHEMA_SERVICE_CONTAINER_STOP,
    SCHEMA_SERVICE_SERVICE_RELOAD,
    SCHEMA_SERVICE_SERVICE_RESTART,
    SCHEMA_SERVICE_SERVICE_START,
    SCHEMA_SERVICE_SERVICE_STOP,
    SCHEMA_SERVICE_VM_RESTART,
    SCHEMA_SERVICE_VM_START,
    SCHEMA_SERVICE_VM_STOP,
    SERVICE_APP_START,
    SERVICE_APP_STOP,
    SERVICE_CONTAINER_RESTART,
    SERVICE_CONTAINER_START,
    SERVICE_CONTAINER_STOP,
    SERVICE_SERVICE_RELOAD,
    SERVICE_SERVICE_RESTART,
    SERVICE_SERVICE_START,
    SERVICE_SERVICE_STOP,
    SERVICE_VM_RESTART,
    SERVICE_VM_START,
    SERVICE_VM_STOP,
)

DEVICE_ATTRIBUTES_POOL = (
    "path",
    "status",
    "healthy",
    "is_decrypted",
    "autotrim",
    "scrub_state",
    "scrub_start",
    "scrub_end",
    "scrub_secs_left",
    "available",
    "total",
)

DEVICE_ATTRIBUTES_VM = (
    "type",
    "cpu",
    "memory",
    "autostart",
    "image",
)

DEVICE_ATTRIBUTES_CONTAINER = (
    "type",
    "status",
    "cpu",
    "memory",
    "autostart",
    "image",
    "ip_address",
)

DEVICE_ATTRIBUTES_SERVICE = (
    "enable",
    "state",
)

DEVICE_ATTRIBUTES_APP = (
    "name",
    "version",
    "latest_version",
    "human_version",
    "update_available",
    "image_updates_available",
    "custom_app",
    "portal",
)

DEVICE_ATTRIBUTES_NETWORK = (
    "description",
    "mtu",
    "link_state",
    "active_media_type",
    "active_media_subtype",
    "link_address",
)

DEVICE_ATTRIBUTES_DIRECTORYSERVICE = (
    "type",
    "status",
    "status_msg",
    "domain",
    "kerberos_realm",
    "account_cache",
    "dns_updates",
    "site",
)


@dataclass
class TrueNASBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Class describing entities."""

    icon_enabled: str | None = None
    icon_disabled: str | None = None
    ha_group: str | None = None
    ha_connection: str | None = None
    ha_connection_value: str | None = None
    data_path: str | None = None
    data_is_on: str = "available"
    data_name: str | None = None
    data_uid: str | None = None
    data_reference: str | None = None
    data_attributes_list: list[str] = field(default_factory=list)
    func: str = "TrueNASBinarySensor"


SENSOR_TYPES: tuple[TrueNASBinarySensorEntityDescription, ...] = (
    TrueNASBinarySensorEntityDescription(
        key="disk_issues",
        name="Disk/Pool issues",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        ha_group="System",
        data_path="alerts",
        data_is_on="disk_issues",
        data_name=None,
        data_uid=None,
        data_reference=None,
    ),
    TrueNASBinarySensorEntityDescription(
        key="pool_healthy",
        name="healthy",
        icon_enabled="mdi:database",
        icon_disabled="mdi:database-off",
        device_class=None,
        entity_category=None,
        ha_group="Pools",
        data_path="pool",
        data_is_on="healthy",
        data_name="name",
        data_uid=None,
        data_reference="guid",
        data_attributes_list=DEVICE_ATTRIBUTES_POOL,
    ),
    TrueNASBinarySensorEntityDescription(
        key="vm",
        name="",
        icon_enabled="mdi:server",
        icon_disabled="mdi:server-off",
        device_class=None,
        entity_category=None,
        ha_group="VMs",
        data_path="vm",
        data_is_on="running",
        data_name="name",
        data_uid=None,
        data_reference="id",
        data_attributes_list=DEVICE_ATTRIBUTES_VM,
        func="TrueNASVMBinarySensor",
    ),
    TrueNASBinarySensorEntityDescription(
        key="container",
        name="",
        icon_enabled="mdi:cube-outline",
        icon_disabled="mdi:cube-off-outline",
        device_class=None,
        entity_category=None,
        ha_group="Containers",
        data_path="container",
        data_is_on="running",
        data_name="name",
        data_uid=None,
        data_reference="id",
        data_attributes_list=DEVICE_ATTRIBUTES_CONTAINER,
        func="TrueNASContainerBinarySensor",
    ),
    TrueNASBinarySensorEntityDescription(
        key="service",
        name="",
        icon_enabled="mdi:cog",
        icon_disabled="mdi:cog-off",
        device_class=None,
        entity_category=None,
        entity_registry_enabled_default=False,
        ha_group="Services",
        data_path="service",
        data_is_on="running",
        data_name="display_name",
        data_uid=None,
        data_reference="id",
        data_attributes_list=DEVICE_ATTRIBUTES_SERVICE,
        func="TrueNASServiceBinarySensor",
    ),
    TrueNASBinarySensorEntityDescription(
        key="app",
        name="",
        icon_enabled="mdi:server",
        icon_disabled="mdi:server-off",
        device_class=None,
        entity_category=None,
        ha_group="Apps",
        data_path="app",
        data_is_on="running",
        data_name="name",
        data_uid=None,
        data_reference="id",
        data_attributes_list=DEVICE_ATTRIBUTES_APP,
        func="TrueNASAppBinarySensor",
    ),
    TrueNASBinarySensorEntityDescription(
        key="interface",
        name="Link",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        ha_group="Network",
        data_path="interface",
        data_is_on="link_up",
        data_name="name",
        data_uid=None,
        data_reference="id",
        data_attributes_list=DEVICE_ATTRIBUTES_NETWORK,
    ),
    TrueNASBinarySensorEntityDescription(
        key="directoryservices",
        name="",
        icon_enabled="mdi:account-group",
        icon_disabled="mdi:account-group-outline",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        ha_group="Directory Services",
        data_path="directoryservices",
        data_is_on="healthy",
        data_name="domain",
        data_uid=None,
        data_reference="id",
        data_attributes_list=DEVICE_ATTRIBUTES_DIRECTORYSERVICE,
    ),
)


class BinarySensorService(NamedTuple):
    """Service definition."""

    name: str
    schema: Any
    action: str


SENSOR_SERVICES: tuple[BinarySensorService, ...] = (
    BinarySensorService(SERVICE_VM_START, SCHEMA_SERVICE_VM_START, "start"),
    BinarySensorService(SERVICE_VM_STOP, SCHEMA_SERVICE_VM_STOP, "stop"),
    BinarySensorService(SERVICE_VM_RESTART, SCHEMA_SERVICE_VM_RESTART, "restart"),
    BinarySensorService(
        SERVICE_CONTAINER_START, SCHEMA_SERVICE_CONTAINER_START, "start"
    ),
    BinarySensorService(SERVICE_CONTAINER_STOP, SCHEMA_SERVICE_CONTAINER_STOP, "stop"),
    BinarySensorService(
        SERVICE_CONTAINER_RESTART, SCHEMA_SERVICE_CONTAINER_RESTART, "restart"
    ),
    BinarySensorService(SERVICE_SERVICE_START, SCHEMA_SERVICE_SERVICE_START, "start"),
    BinarySensorService(SERVICE_SERVICE_STOP, SCHEMA_SERVICE_SERVICE_STOP, "stop"),
    BinarySensorService(
        SERVICE_SERVICE_RESTART, SCHEMA_SERVICE_SERVICE_RESTART, "restart"
    ),
    BinarySensorService(
        SERVICE_SERVICE_RELOAD, SCHEMA_SERVICE_SERVICE_RELOAD, "reload"
    ),
    BinarySensorService(SERVICE_APP_START, SCHEMA_SERVICE_APP_START, "start"),
    BinarySensorService(SERVICE_APP_STOP, SCHEMA_SERVICE_APP_STOP, "stop"),
)
