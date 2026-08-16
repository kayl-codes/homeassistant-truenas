"""Unit tests for update.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _fakes import make_coordinator
from homeassistant.exceptions import HomeAssistantError

from custom_components.truenas_ce.update import TrueNASAppUpdate, TrueNASUpdate
from custom_components.truenas_ce.update_types import TrueNASUpdateEntityDescription

_SYSTEM_DESC = TrueNASUpdateEntityDescription(
    key="system_update", name=None, data_path="system_info", title="TrueNAS"
)
_APP_DESC = TrueNASUpdateEntityDescription(key="app_update", name=None, data_path="app")


def _make_system_update(data: dict | None = None) -> TrueNASUpdate:
    coordinator = make_coordinator(data={"system_info": {**data} if data else {}})
    return TrueNASUpdate(coordinator, _SYSTEM_DESC)


def _make_app_update(data: dict | None = None) -> TrueNASAppUpdate:
    coordinator = make_coordinator(data={"app": {"a1": (data or {})}})
    return TrueNASAppUpdate(coordinator, _APP_DESC, "a1")


def test_system_update_installed_and_latest_version() -> None:
    update = _make_system_update({"version": "25.10.4", "update_version": "25.10.5"})
    assert update.installed_version == "25.10.4"
    assert update.latest_version == "25.10.5"


def test_system_update_in_progress_and_percentage() -> None:
    update = _make_system_update({"update_state": "RUNNING", "update_progress": 42})
    assert update.in_progress is True
    assert update.update_percentage == 42


def test_system_update_not_running_has_no_percentage() -> None:
    update = _make_system_update({"update_state": "IDLE"})
    assert update.in_progress is False
    assert update.update_percentage is None


async def test_system_update_async_install_success() -> None:
    update = _make_system_update({})
    update.coordinator.supports_update_run.return_value = False
    update.coordinator.api.query.return_value = 555
    await update.async_install(version=None, backup=False)
    update.coordinator.api.query.assert_awaited_once_with(
        "update.update", {"reboot": True}
    )
    assert update._data["update_jobid"] == 555
    update.coordinator.async_refresh.assert_awaited_once()


async def test_system_update_async_install_uses_update_run_on_2510_plus() -> None:
    update = _make_system_update({})
    update.coordinator.supports_update_run.return_value = True
    update.coordinator.api.query.return_value = 555
    await update.async_install(version=None, backup=False)
    update.coordinator.api.query.assert_awaited_once_with(
        "update.run", {"reboot": True}
    )
    assert update._data["update_jobid"] == 555


async def test_system_update_async_install_failure_raises() -> None:
    update = _make_system_update({})
    update.coordinator.api.query.return_value = None
    update.coordinator.api.error = "job rejected"
    with pytest.raises(HomeAssistantError) as exc_info:
        await update.async_install(version=None, backup=False)
    assert exc_info.value.translation_key == "system_update_failed"
    assert exc_info.value.translation_placeholders == {
        "host": update.coordinator.host,
        "error": "job rejected",
    }


async def test_system_update_options_updated_is_noop() -> None:
    update = _make_system_update({})
    await update.options_updated()


def test_app_update_installed_version() -> None:
    update = _make_app_update({"version": "1.2.3"})
    assert update.installed_version == "1.2.3"


def test_app_update_latest_version_no_update_available() -> None:
    update = _make_app_update({"version": "1.2.3", "update_available": False})
    assert update.latest_version == "1.2.3"


def test_app_update_latest_version_unknown_catalog_shows_image_update() -> None:
    update = _make_app_update(
        {"version": "1.2.3", "update_available": True, "latest_version": "unknown"}
    )
    assert update.latest_version == "1.2.3 (image update)"


def test_app_update_latest_version_unknown_no_installed_shows_image_update() -> None:
    update = _make_app_update({"update_available": True, "latest_version": "unknown"})
    assert update.latest_version == "image update"


def test_app_update_latest_version_same_as_installed_shows_image_update() -> None:
    update = _make_app_update(
        {"version": "1.2.3", "update_available": True, "latest_version": "1.2.3"}
    )
    assert update.latest_version == "1.2.3 (image update)"


def test_app_update_latest_version_real_catalog_version() -> None:
    update = _make_app_update(
        {"version": "1.2.3", "update_available": True, "latest_version": "1.3.0"}
    )
    assert update.latest_version == "1.3.0"


def test_app_update_title_and_in_progress() -> None:
    update = _make_app_update({"name": "Plex", "update_jobid": 42})
    assert update.title == "Plex"
    assert update.in_progress is True


async def test_app_update_async_install_not_running_logs_and_skips() -> None:
    update = _make_app_update({"id": "a1"})
    update.coordinator.data["app"] = {"a1": {"state": "STOPPED"}}
    await update.async_install(version=None, backup=False)
    update.coordinator.api.query.assert_not_awaited()


async def test_app_update_async_install_success() -> None:
    update = _make_app_update({"id": "a1"})
    update.coordinator.data["app"] = {"a1": {"state": "RUNNING"}}
    update.coordinator.api.query.return_value = 99
    update.async_write_ha_state = MagicMock()
    update._async_track_upgrade_job = AsyncMock(return_value={"state": "SUCCESS"})
    await update.async_install(version=None, backup=False)
    update.coordinator.api.query.assert_awaited_once_with("app.upgrade", ["a1"])
    assert update._data["update_jobid"] == 99
    assert update._data["update_progress"] == 0
    update.async_write_ha_state.assert_called_once()
    update._async_track_upgrade_job.assert_awaited_once()
    update.coordinator.async_request_refresh.assert_awaited_once()


async def test_app_update_async_install_failure_raises() -> None:
    update = _make_app_update({"id": "a1"})
    update.coordinator.data["app"] = {"a1": {"state": "RUNNING"}}
    update.coordinator.api.query.return_value = None
    update.coordinator.api.error = "upgrade failed"
    with pytest.raises(HomeAssistantError) as exc_info:
        await update.async_install(version=None, backup=False)
    assert exc_info.value.translation_key == "app_update_failed"
    assert exc_info.value.translation_placeholders == {
        "app": "a1",
        "host": update.coordinator.host,
        "error": "upgrade failed",
    }


# ---------------------------
#   App update job progress tracking
# ---------------------------
def test_app_update_supports_progress_feature() -> None:
    from homeassistant.components.update import UpdateEntityFeature

    update = _make_app_update({})
    assert update.supported_features & UpdateEntityFeature.PROGRESS


def test_app_update_percentage_while_running() -> None:
    update = _make_app_update({"update_jobid": 7, "update_progress": 55})
    assert update.in_progress is True
    assert update.update_percentage == 55


def test_app_update_percentage_none_when_idle() -> None:
    update = _make_app_update({"update_jobid": 0, "update_progress": 55})
    assert update.in_progress is False
    assert update.update_percentage is None


def _install_ready_update(job_states: list[dict]) -> TrueNASAppUpdate:
    """App update whose coordinator reports the given job snapshots in order."""
    update = _make_app_update({"id": "a1"})
    update.coordinator.data["app"] = {"a1": update._data}
    update._data["state"] = "RUNNING"
    update.coordinator.api.query.return_value = 99
    update.async_write_ha_state = MagicMock()

    snapshots = iter(job_states)

    async def _refresh(uid: str) -> dict | None:
        assert uid == "a1"
        job = next(snapshots)
        if job is None:
            # Coordinator could not find the job any more and stopped tracking.
            update._data["update_jobid"] = 0
            return None
        update._data["update_state"] = job["state"]
        update._data["update_progress"] = job.get("progress", {}).get("percent", 0)
        if job["state"] not in ("RUNNING", "WAITING"):
            update._data["update_jobid"] = 0
        return job

    update.coordinator.async_refresh_app_update_job = AsyncMock(side_effect=_refresh)
    return update


async def test_app_update_async_install_tracks_job_until_success() -> None:
    update = _install_ready_update(
        [
            {"state": "RUNNING", "progress": {"percent": 10}},
            {"state": "RUNNING", "progress": {"percent": 80}},
            {"state": "SUCCESS", "progress": {"percent": 100}},
        ]
    )
    with patch("custom_components.truenas_ce.update.asyncio.sleep", AsyncMock()):
        await update.async_install(version=None, backup=False)

    update.coordinator.api.query.assert_awaited_once_with("app.upgrade", ["a1"])
    assert update.coordinator.async_refresh_app_update_job.await_count == 3
    # State is pushed after job start and after every job poll.
    assert update.async_write_ha_state.call_count >= 4
    assert update._data["update_jobid"] == 0
    assert update.in_progress is False
    update.coordinator.async_request_refresh.assert_awaited()


async def test_app_update_async_install_keeps_polling_while_waiting() -> None:
    update = _install_ready_update(
        [
            {"state": "WAITING", "progress": {"percent": 0}},
            {"state": "RUNNING", "progress": {"percent": 50}},
            {"state": "SUCCESS", "progress": {"percent": 100}},
        ]
    )
    with patch("custom_components.truenas_ce.update.asyncio.sleep", AsyncMock()):
        await update.async_install(version=None, backup=False)

    assert update.coordinator.async_refresh_app_update_job.await_count == 3
    assert update.in_progress is False


async def test_app_update_async_install_gives_up_after_timeout() -> None:
    # Job never leaves RUNNING; monotonic() jumps past the deadline on the
    # third poll (first call sets the deadline, then one call per poll).
    update = _install_ready_update(
        [{"state": "RUNNING", "progress": {"percent": 10}}] * 3
    )
    with (
        patch("custom_components.truenas_ce.update.asyncio.sleep", AsyncMock()),
        patch("custom_components.truenas_ce.update.APP_UPDATE_JOB_TIMEOUT", 100),
        patch(
            "custom_components.truenas_ce.update.monotonic",
            side_effect=[0, 50, 99, 100],
        ),
    ):
        await update.async_install(version=None, backup=False)

    assert update.coordinator.async_refresh_app_update_job.await_count == 3
    # Job is left for the coordinator poll to track: still marked in progress.
    assert update._data["update_jobid"] == 99
    assert update.in_progress is True
    assert update.async_write_ha_state.call_count == 4
    update.coordinator.async_request_refresh.assert_awaited_once()


async def test_app_update_async_install_raises_when_job_fails() -> None:
    update = _install_ready_update(
        [
            {"state": "RUNNING", "progress": {"percent": 10}},
            {"state": "FAILED", "error": "image pull failed"},
        ]
    )
    with (
        patch("custom_components.truenas_ce.update.asyncio.sleep", AsyncMock()),
        pytest.raises(HomeAssistantError) as exc_info,
    ):
        await update.async_install(version=None, backup=False)

    assert exc_info.value.translation_key == "app_update_job_failed"
    assert exc_info.value.translation_placeholders == {
        "app": "a1",
        "host": update.coordinator.host,
        "error": "image pull failed",
    }
    assert update._data["update_jobid"] == 0


async def test_app_update_async_install_stops_when_job_vanishes() -> None:
    update = _install_ready_update([{"state": "RUNNING"}, None])
    with patch("custom_components.truenas_ce.update.asyncio.sleep", AsyncMock()):
        await update.async_install(version=None, backup=False)

    assert update.coordinator.async_refresh_app_update_job.await_count == 2
    assert update._data["update_jobid"] == 0
    assert update.in_progress is False
