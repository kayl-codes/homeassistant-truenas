"""Definitions for TrueNAS update entities."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.components.update import UpdateEntityDescription


@dataclass
class TrueNASUpdateEntityDescription(UpdateEntityDescription):
    """Class describing entities."""

    ha_group: str | None = None
    ha_connection: str | None = None
    ha_connection_value: str | None = None
    title: str | None = None
    data_path: str | None = None
    data_attribute: str = "available"
    data_name: str | None = None
    data_uid: str | None = None
    data_reference: str | None = None
    data_attributes_list: tuple[str, ...] = field(default_factory=tuple)
    func: str | None = None


SENSOR_TYPES: tuple[TrueNASUpdateEntityDescription, ...] = (
    TrueNASUpdateEntityDescription(
        key="system_update",
        name=None,
        ha_group="System",
        title="TrueNAS",
        data_path="system_info",
        data_attribute="update_available",
        data_name=None,
        data_uid=None,
        data_reference=None,
        data_attributes_list=(
            "update_date",
            "update_profile",
            "update_train",
            "update_filename",
        ),
        func="TrueNASUpdate",
    ),
    TrueNASUpdateEntityDescription(
        key="app_update",
        name=None,
        ha_group="Apps",
        data_path="app",
        data_attribute="update_available",
        data_name="name",
        data_uid="id",
        data_reference="id",
        func="TrueNASAppUpdate",
    ),
)

SENSOR_SERVICES: tuple = ()
