"""TrueNAS sensor platform."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from logging import getLogger
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util.dt import utc_from_timestamp

from .const import (
    API_CLOUDSYNC_SYNC,
    API_REPLICATION_RUN,
    API_RSYNCTASK_RUN,
    API_SNAPSHOTTASK_RUN,
    CONF_DATA_UNIT,
    DEFAULT_DATA_UNIT,
)
from .coordinator import TrueNASCoordinator
from .entity import TrueNASEntity, async_add_entities
from .helper import scaled_data_unit
from .sensor_types import (  # noqa: F401
    SENSOR_SERVICES,
    SENSOR_TYPES,
)

_LOGGER = getLogger(__name__)

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
    dispatcher = {
        "TrueNASSensor": TrueNASSensor,
        "TrueNASUptimeSensor": TrueNASUptimeSensor,
        "TrueNASCloudsyncSensor": TrueNASCloudsyncSensor,
        "TrueNASDatasetSensor": TrueNASDatasetSensor,
        "TrueNASRsyncSensor": TrueNASRsyncSensor,
        "TrueNASReplicationSensor": TrueNASReplicationSensor,
        "TrueNASSnapshotTaskSensor": TrueNASSnapshotTaskSensor,
    }
    await async_add_entities(hass, config_entry, dispatcher)


# ---------------------------
#   TrueNASSensor
# ---------------------------
class TrueNASSensor(TrueNASEntity, SensorEntity):
    """Define an TrueNAS sensor."""

    def __init__(
        self,
        coordinator: TrueNASCoordinator,
        entity_description,
        uid: str | None = None,
    ):
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
                self._data.get(self.entity_description.data_attribute)
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
        return self._data.get(self.entity_description.data_attribute)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit the value is expressed in."""
        if self.entity_description.native_unit_of_measurement:
            if self.entity_description.native_unit_of_measurement.startswith("data__"):
                uom = self.entity_description.native_unit_of_measurement[6:]
                if uom in self._data:
                    return self._data[uom]

            return self.entity_description.native_unit_of_measurement

        return None


# ---------------------------
#   TrueNASUptimeSensor
# ---------------------------
class TrueNASUptimeSensor(TrueNASSensor):
    """Define an TrueNAS Uptime sensor."""

    @property
    def native_value(self) -> StateType | date | datetime | Decimal:
        """Return the value reported by the sensor."""
        val = self._data.get(self.entity_description.data_attribute)
        if isinstance(val, (int, float)) and val > 0:
            return utc_from_timestamp(val)
        return None

    async def restart(self) -> None:
        """Restart TrueNAS systen."""
        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "system.reboot",
            ["Home Assistant Integration"],
        )

    async def stop(self) -> None:
        """Shutdown TrueNAS systen."""
        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "system.shutdown",
            ["Home Assistant Integration"],
        )


# ---------------------------
#   TrueNASDatasetSensor
# ---------------------------
class TrueNASDatasetSensor(TrueNASSensor):
    """Define an TrueNAS Dataset sensor."""

    def _action_error(self, action: str, reason: str) -> str:
        """Build a uniform error message for a dataset action."""
        dataset_name = self._data.get("name", "<unknown>")
        return (
            f"Failed to {action} dataset {dataset_name} "
            f"on {self.coordinator.host}: {reason}"
        )

    async def _poll_job(self, job_id: int) -> dict | None:
        """Fetch a single middleware job by id."""
        jobs = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "core.get_jobs",
            [[["id", "=", job_id]]],
        )
        if isinstance(jobs, list):
            jobs = jobs[0] if jobs else None
        return jobs if isinstance(jobs, dict) else None

    def _job_finished(self, job: dict, action: str) -> bool:
        """Return True if the job succeeded, raise on failure, False if running."""
        state = job.get("state")
        if state == "SUCCESS":
            return True
        if state in JOB_STATES_FAILED:
            reason = job.get("error") or job.get("exception") or "unknown error"
            raise HomeAssistantError(self._action_error(action, reason))
        return False

    async def _wait_for_job(self, job_id: int, action: str) -> dict:
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

    async def _run_dataset_job(self, method: str, payload: list, action: str) -> Any:
        """Start a dataset middleware job, wait for it, and return its result."""
        job_id = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            method,
            payload,
        )
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
        result = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "pool.snapshot.create",
            payload,
        )
        if result is None:
            await self.hass.async_add_executor_job(
                self.coordinator.api.query,
                "zfs.snapshot.create",
                payload,
            )

    def _raise_if_not_encrypted(self, action: str) -> None:
        """Reject lock/unlock on a dataset that is not encrypted.

        Short-circuits before any middleware call: only encrypted datasets can
        be locked/unlocked, so a non-encrypted target is a user error.
        """
        if not self._data.get("encrypted"):
            name = self._data.get("name", "<unknown>")
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

    async def unlock(
        self, passphrase: str, recursive: bool = False, force: bool = False
    ) -> None:
        """Unlock a dataset using the provided passphrase.

        Args:
            passphrase: The dataset passphrase.
            recursive: Unlock datasets recursively.
            force: Force the unlock operation.
        """
        self._raise_if_not_encrypted("unlock")
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
                        "passphrase": passphrase,
                        "recursive": recursive,
                        "force": force,
                    }
                ],
            },
        ]
        result = await self._run_dataset_job("pool.dataset.unlock", payload, "unlock")
        self._raise_on_unlock_failure(result, "unlock")
        await self.coordinator.async_refresh()


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
        jobs = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "cloudsync.query",
            [[["id", "=", self._data["id"]]]],
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
        jobs = await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "cloudsync.query",
            [[["id", "=", self._data["id"]]]],
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

        await self.hass.async_add_executor_job(
            self.coordinator.api.query,
            "cloudsync.abort",
            [self._data["id"]],
        )
