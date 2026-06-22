"""Definitions for TrueNAS switch entities."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.components.switch import SwitchEntityDescription


@dataclass
class TrueNASSwitchEntityDescription(SwitchEntityDescription):
    """Class describing entities."""

    ha_group: str | None = None
    ha_connection: str | None = None
    ha_connection_value: str | None = None
    data_path: str | None = None
    data_is_on: str = "running"
    data_name: str | None = None
    data_uid: str | None = None
    data_reference: str | None = None
    data_attributes_list: list[str] = field(default_factory=list)
    func: str = "TrueNASServiceSwitch"


SENSOR_TYPES: tuple[TrueNASSwitchEntityDescription, ...] = (
    TrueNASSwitchEntityDescription(
        key="service_switch",
        name=None,
        icon="mdi:cog",
        ha_group="Services",
        data_path="service",
        data_is_on="running",
        data_name="display_name",
        data_uid=None,
        data_reference="id",
        data_attributes_list=["enable", "state"],
        func="TrueNASServiceSwitch",
    ),
    TrueNASSwitchEntityDescription(
        key="cloudsync_switch",
        name="Enabled",
        icon="mdi:calendar-check",
        ha_group="Cloudsync",
        data_path="cloudsync",
        data_is_on="enabled",
        data_name="description",
        data_uid=None,
        data_reference="id",
        func="TrueNASCloudsyncSwitch",
    ),
)

SENSOR_SERVICES: tuple = ()
