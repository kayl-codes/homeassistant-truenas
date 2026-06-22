"""Definitions for TrueNAS button entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple

from homeassistant.components.button import ButtonEntityDescription

from .const import (
    API_CLOUDSYNC_SYNC,
    API_REPLICATION_RUN,
    API_RSYNCTASK_RUN,
    API_SNAPSHOTTASK_RUN,
)

# Run buttons mirror the on-demand "*_run" actions, but as one-tap controls on the
# task's device page. Each carries the JSON-RPC method to call with the object id.


@dataclass
class TrueNASButtonEntityDescription(ButtonEntityDescription):
    """Class describing TrueNAS button entities."""

    api_method: str | None = None
    ha_group: str | None = None
    ha_connection: str | None = None
    ha_connection_value: str | None = None
    data_path: str | None = None
    data_name: str | None = None
    data_uid: str | None = None
    data_reference: str | None = None
    data_attributes_list: list[str] = field(default_factory=list)
    func: str = "TrueNASButton"


ICON_RUN = "mdi:play-circle-outline"


SENSOR_TYPES: tuple[TrueNASButtonEntityDescription, ...] = (
    TrueNASButtonEntityDescription(
        key="snapshottask_run",
        name="Run",
        icon=ICON_RUN,
        api_method=API_SNAPSHOTTASK_RUN,
        ha_group="Snapshot tasks",
        data_path="snapshottask",
        data_name="dataset",
        data_uid=None,
        data_reference="id",
    ),
    TrueNASButtonEntityDescription(
        key="rsync_run",
        name="Run",
        icon=ICON_RUN,
        api_method=API_RSYNCTASK_RUN,
        ha_group="Rsync tasks",
        data_path="rsynctask",
        data_name="desc",
        data_uid=None,
        data_reference="id",
    ),
    TrueNASButtonEntityDescription(
        key="replication_run",
        name="Run",
        icon=ICON_RUN,
        api_method=API_REPLICATION_RUN,
        ha_group="Replication",
        data_path="replication",
        data_name="name",
        data_uid=None,
        data_reference="id",
    ),
    TrueNASButtonEntityDescription(
        key="cloudsync_run",
        name="Run",
        icon=ICON_RUN,
        api_method=API_CLOUDSYNC_SYNC,
        ha_group="Cloudsync",
        data_path="cloudsync",
        data_name="description",
        data_uid=None,
        data_reference="id",
    ),
)


class ButtonService(NamedTuple):
    """Service definition."""

    name: str
    schema: Any
    action: str


SENSOR_SERVICES: tuple[ButtonService, ...] = ()
