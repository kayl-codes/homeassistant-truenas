"""TrueNAS sensor platform."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from logging import getLogger
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_platform as ep
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util.dt import utc_from_timestamp

from .const import (
    API_CLOUDSYNC_SYNC,
    API_REPLICATION_RUN,
    API_RSYNCTASK_RUN,
    API_SNAPSHOTTASK_RUN,
    CONF_DATA_UNIT,
    CONF_DATASET_PASSPHRASES,
    DEFAULT_DATA_UNIT,
    SIGNAL_UPDATE_SENSORS,
)
from .coordinator import TrueNASCoordinator, get_truenas_coordinator
from .entity import TrueNASEntity, async_add_entities, format_unique_id
from .helper import alert_action, scaled_data_unit
from .sensor_types import (  # noqa: F401
    SENSOR_SERVICES,
    SENSOR_TYPES,
    TrueNASSensorEntityDescription,
)

_LOGGER = getLogger(__name__)
_UNKNOWN_DATASET = "<unknown>"

# Updates are centralized in the coordinator; entity actions may run unlimited.
PARALLEL_UPDATES = 0

# Middleware job polling for dataset lock/unlock operations.
JOB_POLL_INTERVAL = 1
JOB_WAIT_TIMEOUT = 30
JOB_STATES_FAILED = ("FAILED", "ABORTED")
# Tolerate a few empty lookups (the job may not be queryable immediately after
# start) before treating a persistently missing job as a failure.
JOB_MAX_MISSING_POLLS = 5


# ---------------------------
#   async_setup_entry
# ---------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    _async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up entry for TrueNAS component."""
    coordinator: TrueNASCoordinator | None = get_truenas_coordinator(config_entry)
    if coordinator is None:
        return None

    platform = ep.async_get_current_platform()

    @callback
    def _discover_app_stats_sensors(
        updated_coordinator: TrueNASCoordinator | None = None,
    ) -> None:
        _discover_app_stats(
            platform, updated_coordinator or coordinator, _async_add_entities
        )

    _discover_app_stats_sensors()
    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_UPDATE_SENSORS, _discover_app_stats_sensors
        )
    )

    dispatcher = {
        "TrueNASSensor": TrueNASSensor,
        "TrueNASAlertSensor": TrueNASAlertSensor,
        "TrueNASUptimeSensor": TrueNASUptimeSensor,
        "TrueNASCloudsyncSensor": TrueNASCloudsyncSensor,
        "TrueNASDatasetSensor": TrueNASDatasetSensor,
        "TrueNASCertExpirySensor": TrueNASCertExpirySensor,
        "TrueNASDiskSensor": TrueNASDiskSensor,
        "TrueNASRsyncSensor": TrueNASRsyncSensor,
        "TrueNASReplicationSensor": TrueNASReplicationSensor,
    }
    await async_add_entities(hass, config_entry, dispatcher)


@callback
def _discover_app_stats(
    platform: Any,
    coord: TrueNASCoordinator,
    add_entities: AddEntitiesCallback,
) -> None:
    """Discover dynamic app stats sensors for the current coordinator state."""
    app_stats_data = coord.data.get("app_stats", {})
    app_stats_entities: list[TrueNASAppStatsSensor] = []

    loaded = {
        entity.unique_id
        for entity in platform.entities.values()
        if entity.unique_id is not None
    }

    inst = coord.config_entry.data[CONF_NAME]

    for description in _app_stats_descriptions():
        for uid, app_data in app_stats_data.items():
            _maybe_discover_app_stats_sensor(
                description, uid, app_data, inst, loaded, app_stats_entities, coord
            )

    if app_stats_entities:
        add_entities(app_stats_entities)


def _app_stats_descriptions() -> list[TrueNASSensorEntityDescription]:
    """Sensor descriptions that belong to the app_stats dynamic family."""
    return [d for d in SENSOR_TYPES if d.func == "TrueNASAppStatsSensor"]


def _maybe_discover_app_stats_sensor(
    description: TrueNASSensorEntityDescription,
    uid: str,
    app_data: dict[str, Any],
    inst: str,
    loaded: set[str],
    entities: list[TrueNASAppStatsSensor],
    coord: TrueNASCoordinator,
) -> None:
    """Append a new entity if it is not already loaded."""
    if not app_data:
        return

    if description.key in (
        "app_stats_network_rx",
        "app_stats_network_tx",
    ):
        _discover_network_sensors(
            description, uid, app_data, inst, loaded, entities, coord
        )
    else:
        _discover_standard_sensor(description, uid, inst, loaded, entities, coord)


def _discover_network_sensors(
    description: TrueNASSensorEntityDescription,
    uid: str,
    app_data: dict[str, Any],
    inst: str,
    loaded: set[str],
    entities: list[TrueNASAppStatsSensor],
    coord: TrueNASCoordinator,
) -> None:
    """Create network interface sensors for one app entry."""
    networks = app_data.get("networks", [])
    if not isinstance(networks, list):
        return
    for net in networks:
        if not isinstance(net, dict):
            continue
        interface_name = net.get("interface_name")
        if not interface_name:
            continue
        unique_id = format_unique_id(inst, description.key, f"{uid}::{interface_name}")
        if unique_id in loaded:
            continue
        entities.append(
            TrueNASAppStatsSensor(coord, description, f"{uid}::{interface_name}")
        )
        loaded.add(unique_id)


def _discover_standard_sensor(
    description: TrueNASSensorEntityDescription,
    uid: str,
    inst: str,
    loaded: set[str],
    entities: list[TrueNASAppStatsSensor],
    coord: TrueNASCoordinator,
) -> None:
    """Create a single standard app stats sensor if not already loaded."""
    unique_id = format_unique_id(inst, description.key, uid)
    if unique_id in loaded:
        return
    entities.append(TrueNASAppStatsSensor(coord, description, uid))
    loaded.add(unique_id)


# ---------------------------
#   TrueNASSensor
# ---------------------------
class TrueNASSensor(TrueNASEntity, SensorEntity):
    """Define an TrueNAS sensor."""

    entity_description: TrueNASSensorEntityDescription

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        entity_description: TrueNASSensorEntityDescription,
        uid: str | None = None,
    ) -> None:
        super().__init__(coordinator, entity_description, uid)
        self._attr_suggested_unit_of_measurement = (
            self.entity_description.suggested_unit_of_measurement
        )

        if self._attr_suggested_unit_of_measurement in (
            UnitOfInformation.GIGABYTES,
            UnitOfInformation.GIBIBYTES,
        ):
            data_unit = self.coordinator.config_entry.options.get(
                CONF_DATA_UNIT,
                self.coordinator.config_entry.data.get(
                    CONF_DATA_UNIT, DEFAULT_DATA_UNIT
                ),
            )
            value = (
                self._data.get(self.entity_description.data_attribute or "")
                if self._data
                else None
            )
            unit, precision = scaled_data_unit(value, data_unit == "GiB")
            self._attr_suggested_unit_of_measurement = unit
            if precision is not None:
                self._attr_suggested_display_precision = precision

    @property
    def native_value(self) -> StateType | date | datetime | Decimal:
        """Return the value reported by the sensor.

        Uses .get() so a transient API failure that empties the coordinator data
        degrades the state to unknown instead of raising a KeyError mid-update.
        """
        value: StateType | date | datetime | Decimal = self._data.get(
            self.entity_description.data_attribute or ""
        )
        return value

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit the value is expressed in."""
        if self.entity_description.native_unit_of_measurement:
            if self.entity_description.native_unit_of_measurement.startswith("data__"):
                uom = self.entity_description.native_unit_of_measurement[6:]
                if uom in self._data:
                    data_uom: str | None = self._data[uom]
                    return data_uom

            return self.entity_description.native_unit_of_measurement

        return None


# ---------------------------
#   TrueNASCertExpirySensor
# ---------------------------
_DAYS_PER_YEAR = 365.25


class TrueNASCertExpirySensor(TrueNASSensor):
    """Certificate expiry sensor with adaptive unit (years ≥ 365 d, else days)."""

    @property
    def native_value(self) -> StateType:
        """Return days or fractional years depending on magnitude."""
        days: float | None = self._data.get(
            self.entity_description.data_attribute or ""
        )
        if days is None:
            return None
        if days >= 365:
            return round(days / _DAYS_PER_YEAR, 1)
        return days

    @property
    def native_unit_of_measurement(self) -> UnitOfTime:
        """Switch unit between days and years based on the raw value."""
        days: float | None = self._data.get(
            self.entity_description.data_attribute or ""
        )
        if days is not None and days >= 365:
            return UnitOfTime.YEARS
        return UnitOfTime.DAYS


# ---------------------------
#   TrueNASDiskSensor
# ---------------------------
_DISK_TYPE_ICONS = {
    "HDD": "mdi:harddisk",
    "SSD": "mdi:chip",
    "SED": "mdi:chip",
}


class TrueNASDiskSensor(TrueNASSensor):
    """Disk temperature sensor.

    force_update ensures HA records every poll value even when the temperature
    is stable, so the history graph never appears frozen.
    """

    _attr_force_update = True

    @property
    def icon(self) -> str:
        """Return an icon based on the disk type (HDD / SSD / NVMe).

        TrueNAS reports NVMe drives as type=SSD, so devname prefix takes
        priority over the type field.
        """
        if (self._data.get("devname") or "").lower().startswith("nvme"):
            return "mdi:expansion-card-variant"
        disk_type = (self._data.get("type") or "").upper()
        return _DISK_TYPE_ICONS.get(disk_type, "mdi:harddisk")


# ---------------------------
#   TrueNASUptimeSensor
# ---------------------------
class TrueNASUptimeSensor(TrueNASSensor):
    """Define an TrueNAS Uptime sensor."""

    @property
    def native_value(self) -> StateType | date | datetime | Decimal:
        """Return the value reported by the sensor."""
        val = self._data.get(self.entity_description.data_attribute or "")
        if isinstance(val, (int, float)) and val > 0:
            return utc_from_timestamp(val)
        return None

    async def restart(self) -> None:
        """Restart TrueNAS systen."""
        await self.coordinator.api.query(
            "system.reboot", ["Home Assistant Integration"]
        )

    async def stop(self) -> None:
        """Shutdown TrueNAS systen."""
        await self.coordinator.api.query(
            "system.shutdown", ["Home Assistant Integration"]
        )

    async def refresh(self) -> None:
        """Force an immediate coordinator re-poll of TrueNAS.

        Triggers the same update cycle that otherwise runs on the poll interval,
        so automations can act on current data without waiting for the next poll.
        """
        await self.coordinator.async_refresh()


# ---------------------------
#   TrueNASAlertSensor
# ---------------------------
class TrueNASAlertSensor(TrueNASSensor):
    """Define a TrueNAS Alert sensor with dismiss/restore actions."""

    async def dismiss(self, **kwargs: Any) -> None:
        """Dismiss a TrueNAS alert by its UUID."""
        uuid = kwargs.get("uuid")
        if not uuid:
            raise ServiceValidationError("Missing required parameter: uuid")
        await alert_action(self.coordinator, uuid, "dismiss")

    async def restore(self, **kwargs: Any) -> None:
        """Restore (un-dismiss) a previously dismissed TrueNAS alert by UUID."""
        uuid = kwargs.get("uuid")
        if not uuid:
            raise ServiceValidationError("Missing required parameter: uuid")
        await alert_action(self.coordinator, uuid, "restore")


# ---------------------------
#   TrueNASDatasetSensor
# ---------------------------
class TrueNASDatasetSensor(TrueNASSensor):
    """Define an TrueNAS Dataset sensor."""

    def _action_error(self, action: str, reason: str) -> str:
        """Build a uniform error message for a dataset action."""
        dataset_name = self._data.get("name", _UNKNOWN_DATASET)
        return (
            f"Failed to {action} dataset {dataset_name} "
            f"on {self.coordinator.host}: {reason}"
        )

    async def _poll_job(self, job_id: int) -> dict[str, Any] | None:
        """Fetch a single middleware job by id."""
        jobs = await self.coordinator.api.query(
            "core.get_jobs", [[["id", "=", job_id]]]
        )
        if isinstance(jobs, list):
            jobs = jobs[0] if jobs else None
        return jobs if isinstance(jobs, dict) else None

    def _job_finished(self, job: dict[str, Any], action: str) -> bool:
        """Return True if the job succeeded, raise on failure, False if running."""
        state = job.get("state")
        if state == "SUCCESS":
            return True
        if state in JOB_STATES_FAILED:
            reason = job.get("error") or job.get("exception") or "unknown error"
            raise HomeAssistantError(self._action_error(action, reason))
        return False

    async def _wait_for_job(self, job_id: int, action: str) -> dict[str, Any]:
        """Poll a middleware job until it succeeds, fails or times out."""
        missing = 0
        try:
            # asyncio.timeout() raises TimeoutError (the builtin is asyncio's
            # TimeoutError on py3.11+; the alias is avoided per ruff UP041).
            async with asyncio.timeout(JOB_WAIT_TIMEOUT):
                while True:
                    job = await self._poll_job(job_id)
                    if job is None:
                        missing += 1
                        if missing >= JOB_MAX_MISSING_POLLS:
                            raise HomeAssistantError(
                                self._action_error(action, f"job {job_id} not found")
                            )
                    elif self._job_finished(job, action):
                        return job
                    else:
                        missing = 0
                    await asyncio.sleep(JOB_POLL_INTERVAL)
        except TimeoutError as err:
            raise HomeAssistantError(
                self._action_error(action, "timed out waiting for completion")
            ) from err

    async def _run_dataset_job(
        self, method: str, payload: list[Any], action: str
    ) -> Any:
        """Start a dataset middleware job, wait for it, and return its result."""
        job_id = await self.coordinator.api.query(method, payload)
        if not isinstance(job_id, int):
            raise HomeAssistantError(self._action_error(action, "invalid job id"))
        job = await self._wait_for_job(job_id, action)
        return job.get("result")

    def _raise_on_unlock_failure(self, result: Any, action: str) -> None:
        """Raise if pool.dataset.unlock reported per-dataset failures.

        A wrong passphrase makes the job *succeed* (state SUCCESS) but lists the
        dataset under ``failed`` with an error, so the job state alone is not
        enough to tell the user it actually worked.
        """
        if not isinstance(result, dict):
            return
        failed = result.get("failed")
        if not isinstance(failed, dict) or not failed:
            return
        reasons = "; ".join(
            f"{name}: {info.get('error', 'unknown error')}"
            if isinstance(info, dict)
            else str(name)
            for name, info in failed.items()
        )
        raise HomeAssistantError(self._action_error(action, reasons))

    async def snapshot(self) -> None:
        """Create dataset snapshot."""
        ts = datetime.now().isoformat(sep="_", timespec="microseconds")
        payload = {"dataset": f"{self._data['name']}", "name": f"custom-{ts}"}
        result = await self.coordinator.api.query("pool.snapshot.create", payload)
        if result is None:
            await self.coordinator.api.query("zfs.snapshot.create", payload)

    def _raise_if_not_encrypted(self, action: str) -> None:
        """Reject lock/unlock on a dataset that is not encrypted.

        Short-circuits before any middleware call: only encrypted datasets can
        be locked/unlocked, so a non-encrypted target is a user error.
        """
        if not self._data.get("encrypted"):
            name = self._data.get("name", _UNKNOWN_DATASET)
            raise ServiceValidationError(
                f"Dataset {name} is not encrypted and cannot be {action}ed"
            )

    def _log_already(self, state: str) -> None:
        """Log that a dataset is already in the requested lock state."""
        _LOGGER.debug(
            "Dataset id=%s Name=%s Locked=%s Encrypted=%s is already %s",
            self._data.get("id"),
            self._data.get("name"),
            self._data.get("locked"),
            self._data.get("encrypted"),
            state,
        )

    async def lock(self, force_umount: bool = False) -> None:
        """Lock a dataset.

        Args:
            force_umount: Force umount dataset mountpoints before locking.
        """
        self._raise_if_not_encrypted("lock")
        # async_refresh (not async_request_refresh) so the locked state is read
        # fresh here: request_refresh is debounced and may still return stale data
        # when automations toggle datasets in quick succession.
        await self.coordinator.async_refresh()
        if self._data.get("locked", True):
            self._log_already("locked")
            return

        payload = [self._data.get("id"), {"force_umount": force_umount}]
        await self._run_dataset_job("pool.dataset.lock", payload, "lock")
        await self.coordinator.async_refresh()

    def _stored_passphrase(self) -> str | None:
        """Return the stored passphrase for this dataset, or None."""
        dataset_name = self._data.get("name")
        if not dataset_name:
            return None
        stored = self.coordinator.config_entry.data.get(CONF_DATASET_PASSPHRASES, {})
        return stored.get(dataset_name) if isinstance(stored, dict) else None

    async def unlock(
        self,
        passphrase: str | None = None,
        recursive: bool = False,
        force: bool = False,
    ) -> None:
        """Unlock a dataset.

        Uses ``passphrase`` if supplied, otherwise falls back to the passphrase
        stored in the config entry via ``passphrase_set``.
        """
        self._raise_if_not_encrypted("unlock")

        effective_passphrase = (
            passphrase if passphrase is not None else self._stored_passphrase()
        )
        if not effective_passphrase:
            dataset_name = self._data.get("name", _UNKNOWN_DATASET)
            raise ServiceValidationError(
                f"No passphrase provided or stored for dataset {dataset_name}. "
                "Call passphrase_set first or supply the passphrase in the action call."
            )

        # See lock(): async_refresh forces fresh data before the idempotency check.
        await self.coordinator.async_refresh()
        if not self._data.get("locked", True):
            self._log_already("unlocked")
            return

        payload = [
            self._data.get("id"),
            {
                # Top-level "recursive" is what actually unlocks the child tree
                # (the per-dataset flag alone does not), verified against 25.04.
                "recursive": recursive,
                "datasets": [
                    {
                        "name": self._data.get("name"),
                        "passphrase": effective_passphrase,
                        "recursive": recursive,
                        "force": force,
                    }
                ],
            },
        ]
        result = await self._run_dataset_job("pool.dataset.unlock", payload, "unlock")
        self._raise_on_unlock_failure(result, "unlock")
        await self.coordinator.async_refresh()

    async def passphrase_set(self, passphrase: str) -> None:
        """Store a passphrase for this dataset in the config entry."""
        dataset_name = self._data.get("name")
        if not dataset_name:
            raise ServiceValidationError(
                "Cannot store passphrase: dataset name is unknown"
            )
        existing = self.coordinator.config_entry.data.get(CONF_DATASET_PASSPHRASES, {})
        updated = (
            {**existing, dataset_name: passphrase}
            if isinstance(existing, dict)
            else {dataset_name: passphrase}
        )
        self.hass.config_entries.async_update_entry(
            self.coordinator.config_entry,
            data={
                **self.coordinator.config_entry.data,
                CONF_DATASET_PASSPHRASES: updated,
            },
        )
        _LOGGER.debug("Stored passphrase for dataset %s", dataset_name)


# ---------------------------
#   TrueNASRsyncSensor
# ---------------------------
class TrueNASRsyncSensor(TrueNASSensor):
    """Define a TrueNAS Rsync task sensor."""

    async def start(self) -> None:
        """Run an rsync task."""
        if self._data.get("state") in ("RUNNING", "WAITING"):
            _LOGGER.warning(
                "Rsync task %s (%s) is already running",
                self._data.get("desc"),
                self._data.get("id"),
            )
            return

        await self.coordinator.async_run_task(
            API_RSYNCTASK_RUN, self._data["id"], "rsynctask"
        )


# ---------------------------
#   TrueNASReplicationSensor
# ---------------------------
class TrueNASReplicationSensor(TrueNASSensor):
    """Define a TrueNAS Replication task sensor."""

    async def start(self) -> None:
        """Run a replication task."""
        if self._data.get("state") in ("RUNNING", "WAITING"):
            _LOGGER.warning(
                "Replication %s (%s) is already running",
                self._data.get("name"),
                self._data.get("id"),
            )
            return

        await self.coordinator.async_run_task(
            API_REPLICATION_RUN, self._data["id"], "replication"
        )


# ---------------------------
#   TrueNASSnapshotTaskSensor
# ---------------------------
class TrueNASSnapshotTaskSensor(TrueNASSensor):
    """Define a TrueNAS periodic snapshot task sensor."""

    async def start(self) -> None:
        """Run a periodic snapshot task on demand."""
        if self._data.get("state") == "RUNNING":
            _LOGGER.warning(
                "Snapshot task %s (%s) is already running",
                self._data.get("dataset"),
                self._data.get("id"),
            )
            return

        await self.coordinator.async_run_task(
            API_SNAPSHOTTASK_RUN, self._data["id"], "snapshottask"
        )


# ---------------------------
#   TrueNASCloudsyncSensor
# ---------------------------
class TrueNASCloudsyncSensor(TrueNASSensor):
    """Define an TrueNAS Cloudsync sensor."""

    async def start(self) -> None:
        """Run cloudsync job."""
        jobs = await self.coordinator.api.query(
            "cloudsync.query", [[["id", "=", self._data["id"]]]]
        )
        tmp_job = jobs[0] if isinstance(jobs, list) and jobs else None

        if not isinstance(tmp_job, dict) or "job" not in tmp_job:
            _LOGGER.error(
                "Cloudsync job %s (%s) invalid",
                self._data["description"],
                self._data["id"],
            )
            return
        job_state = tmp_job.get("job")
        state = job_state.get("state") if isinstance(job_state, dict) else None
        if state in ["WAITING", "RUNNING"]:
            _LOGGER.warning(
                "Cloudsync job %s (%s) is already running",
                self._data["description"],
                self._data["id"],
            )
            return

        await self.coordinator.async_run_task(
            API_CLOUDSYNC_SYNC, self._data["id"], "cloudsync"
        )

    async def stop(self) -> None:
        """Abort cloudsync job."""
        jobs = await self.coordinator.api.query(
            "cloudsync.query", [[["id", "=", self._data["id"]]]]
        )
        tmp_job = jobs[0] if isinstance(jobs, list) and jobs else None

        if not isinstance(tmp_job, dict) or "job" not in tmp_job:
            _LOGGER.error(
                "Cloudsync job %s (%s) invalid",
                self._data["description"],
                self._data["id"],
            )
            return
        job_state = tmp_job.get("job")
        state = job_state.get("state") if isinstance(job_state, dict) else None
        if state not in ["WAITING", "RUNNING"]:
            _LOGGER.warning(
                "Cloudsync job %s (%s) is not running",
                self._data["description"],
                self._data["id"],
            )
            return

        await self.coordinator.api.query("cloudsync.abort", [self._data["id"]])


# ---------------------------
#   TrueNASAppStatsSensor
# ---------------------------
class TrueNASAppStatsSensor(TrueNASEntity, SensorEntity):
    """Define a TrueNAS App Statistics sensor."""

    entity_description: TrueNASSensorEntityDescription

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        entity_description: TrueNASSensorEntityDescription,
        uid: str | None = None,
    ) -> None:
        """Initialize the app stats sensor."""
        super().__init__(coordinator, entity_description, uid)

    def _refresh_data(self) -> None:
        """Refresh cached data specifically from the app_stats directory structure."""
        if (
            self._uid
            and self.entity_description.key
            in ("app_stats_network_rx", "app_stats_network_tx")
            and "::" in self._uid
        ):
            app_name, interface_name = self._uid.rsplit("::", 1)
            main_data = self.coordinator.data.get("app_stats", {}).get(app_name, {})

            if main_data and isinstance(main_data.get("networks"), list):
                for net in main_data["networks"]:
                    if (
                        isinstance(net, dict)
                        and net.get("interface_name") == interface_name
                    ):
                        self._data = {
                            **main_data,
                            "interface_name": interface_name,
                            "rx_bytes": net.get("rx_bytes"),
                            "tx_bytes": net.get("tx_bytes"),
                        }
                        return
            self._data = {}
        else:
            self._data = self.coordinator.data.get("app_stats", {}).get(self._uid, {})

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return super().available

    @property
    def unique_id(self) -> str:
        """Return a truly unique id, preventing conflict between apps sharing eth0."""
        return format_unique_id(self._inst, self.entity_description.key, self._uid)

    @property
    def name(self) -> str | None:
        """Return the dynamic friendly name for the entity."""
        app_name = self._data.get("app_name", self._uid)
        if not self._data and isinstance(self._uid, str) and "::" in self._uid:
            app_name = self._uid.rsplit("::", 1)[0]

        if self.entity_description.key in (
            "app_stats_network_rx",
            "app_stats_network_tx",
        ):
            interface_name = self._data.get("interface_name")
            if not interface_name:
                _LOGGER.debug(
                    "App stats network sensor %s: interface_name missing from _data, "
                    "falling back to uid parse, _data=%s",
                    self._uid,
                    self._data,
                )
                if self._uid and "::" in self._uid:
                    interface_name = self._uid.rsplit("::", 1)[1]
                else:
                    return None
            return f"{app_name} {interface_name} {self.entity_description.name}"

        return f"{app_name} {self.entity_description.name}"

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if not self._data:
            return None

        description = self.entity_description
        data_attribute = description.data_attribute
        if data_attribute is None:
            return None

        val = self._data.get(data_attribute)
        if val is None:
            return None

        if description.key in (
            "app_stats_network_rx",
            "app_stats_network_tx",
        ):
            try:
                return float(val) / 1024.0
            except (ValueError, TypeError):
                return val

        return val

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return extra state attributes for advanced card telemetry."""
        attrs = dict(super().extra_state_attributes or {})
        if not self._data:
            return attrs

        app_name = self._data.get("app_name")
        if app_name is not None:
            attrs["app_name"] = app_name
            if "interface_name" in self._data:
                interface_name = self._data.get("interface_name")
                if interface_name is not None:
                    attrs["interface_name"] = interface_name

        return attrs
