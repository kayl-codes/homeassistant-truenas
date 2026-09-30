"""Unit tests for update.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _fakes import make_coordinator
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.truenas_ce.update import (
    TrueNASAppUpdate,
    TrueNASUpdate,
    summarize_job_error,
)
from custom_components.truenas_ce.update_types import TrueNASUpdateEntityDescription

_SYSTEM_DESC = TrueNASUpdateEntityDescription(
    key="system_update", name=None, data_path="system_info", title="TrueNAS"
)
_APP_DESC = TrueNASUpdateEntityDescription(key="app_update", name=None, data_path="app")


@pytest.fixture(autouse=True)
def notifications():
    """Stub persistent notifications; the fake coordinator has no real hass."""
    with patch("custom_components.truenas_ce.update.persistent_notification") as notify:
        yield notify


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


async def test_app_update_async_install_not_running_raises_validation_error() -> None:
    update = _make_app_update({"id": "a1"})
    update.coordinator.data["app"] = {"a1": {"state": "STOPPED"}}
    with pytest.raises(ServiceValidationError) as exc_info:
        await update.async_install(version=None, backup=False)
    assert exc_info.value.translation_key == "app_update_not_running"
    assert exc_info.value.translation_placeholders == {"app": "a1", "state": "STOPPED"}
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


async def test_app_update_async_install_matches_int_id_against_str_keyed_app_map() -> (
    None
):
    """The ``id`` field copied into entity data can still be int-typed (e.g.
    TrueNAS returns a numeric app id), while ``coordinator.data["app"]`` is
    str-keyed end to end -- the RUNNING-state lookup must convert, not miss."""
    update = _make_app_update({"id": 5})
    update.coordinator.data["app"] = {"5": {"state": "RUNNING"}}
    update.coordinator.api.query.return_value = 99
    update.async_write_ha_state = MagicMock()
    update._async_track_upgrade_job = AsyncMock(return_value={"state": "SUCCESS"})
    await update.async_install(version=None, backup=False)
    update.coordinator.api.query.assert_awaited_once_with("app.upgrade", [5])


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


async def test_app_update_async_install_tracks_job_with_int_id_converted_to_str() -> (
    None
):
    """``async_refresh_app_update_job`` takes a str uid (matching the str-keyed
    ``self.ds["app"]``); the entity must convert its raw ``id`` field before
    calling it, even when that field is still int-typed at the API level."""
    update = _make_app_update({"id": 5})
    update.coordinator.data["app"] = {"5": update._data}
    update._data["state"] = "RUNNING"
    update.coordinator.api.query.return_value = 99
    update.async_write_ha_state = MagicMock()
    update.coordinator.async_refresh_app_update_job = AsyncMock(
        return_value={"state": "SUCCESS", "progress": {"percent": 100}}
    )
    with patch("custom_components.truenas_ce.update.asyncio.sleep", AsyncMock()):
        await update.async_install(version=None, backup=False)

    update.coordinator.async_refresh_app_update_job.assert_awaited_once_with("5")


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
    create_task = _capture_background_task(update)
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
    # A background watch takes over so a late outcome is still reported.
    create_task.assert_called_once()
    create_task.call_args.args[1].close()


def _capture_background_task(update: TrueNASAppUpdate) -> MagicMock:
    """Stub the config entry's background-task hook (fake entry lacks it)."""
    create_task = MagicMock()
    update.coordinator.config_entry.async_create_background_task = create_task
    return create_task


async def _run_late_watch(update: TrueNASAppUpdate, final_state: str) -> None:
    """Run the late-upgrade watch while the coordinator finishes the job."""
    update._data["update_jobid"] = 99
    listeners: list = []
    unsubscribe = MagicMock()

    def _add_listener(listener) -> MagicMock:
        listeners.append(listener)
        return unsubscribe

    update.coordinator.async_add_listener = _add_listener
    watch = asyncio.create_task(update._async_watch_late_upgrade(99))
    await asyncio.sleep(0)
    assert not watch.done()  # still waiting while the job runs

    # A later coordinator poll sees the job end and stops tracking it.
    update._data["update_state"] = final_state
    update._data["update_jobid"] = 0
    for listener in listeners:
        listener()
    await watch
    unsubscribe.assert_called_once()


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


_MIGRATION_ERROR = (
    "[EFAULT] Failed to execute 'remove_deprecated_volumes' migration: "
    "Traceback (most recent call last):\n"
    '  File "/mnt/.ix-apps/app_configs/code-server/versions/1.1.40/migrations/'
    'remove_deprecated_volumes", line 10, in migrate\n'
    "    raise Exception(\n"
    "Exception: The deprecated storages have been removed. Before upgrading: "
    "edit the application.\n"
)
_MIGRATION_REASON = (
    "[EFAULT] Failed to execute 'remove_deprecated_volumes' migration: "
    "The deprecated storages have been removed. Before upgrading: "
    "edit the application."
)


def test_summarize_job_error_extracts_traceback_reason() -> None:
    assert summarize_job_error(_MIGRATION_ERROR) == _MIGRATION_REASON


def test_summarize_job_error_joins_multiline_exception_message() -> None:
    error = (
        "Traceback (most recent call last):\n"
        '  File "x", line 1\n'
        "middlewared.service_exception.CallError: first line\n"
        "second line\n"
    )
    assert summarize_job_error(error) == "first line second line"


def test_summarize_job_error_passes_plain_errors_through() -> None:
    assert summarize_job_error("  image pull failed \n") == "image pull failed"
    assert summarize_job_error(None) == ""


def test_summarize_job_error_without_exception_line_uses_last_line() -> None:
    error = "Job failed: Traceback (most recent call last):\n  weird tail\n"
    assert summarize_job_error(error) == "Job failed: weird tail"


def test_summarize_job_error_ignores_non_exception_colon_lines() -> None:
    error = (
        "Traceback (most recent call last):\n"
        "ValueError: real reason\n"
        "note: File x, SomeError: not an exception line\n"
    )
    assert summarize_job_error(error) == (
        "real reason note: File x, SomeError: not an exception line"
    )


_FAILED_JOB = {"state": "FAILED", "error": _MIGRATION_ERROR}


def _failing_update(
    instance: object, start_result: object = 7, job_snapshots: list | None = None
) -> TrueNASAppUpdate:
    """App update whose upgrade job fails with the migration traceback."""
    update = _install_ready_update(job_snapshots or [_FAILED_JOB])
    update._data.update({"name": "code-server", "latest_version": "1.1.40"})

    async def _query(method: str, params: object = None) -> object:
        return {
            "app.upgrade": 99,
            "app.get_instance": instance,
            "app.start": start_result,
            "core.get_jobs": [_FAILED_JOB],
        }[method]

    update.coordinator.api.query = AsyncMock(side_effect=_query)
    return update


async def _install_expecting_failure(update: TrueNASAppUpdate) -> HomeAssistantError:
    with (
        patch("custom_components.truenas_ce.update.asyncio.sleep", AsyncMock()),
        pytest.raises(HomeAssistantError) as exc_info,
    ):
        await update.async_install(version=None, backup=False)
    return exc_info.value


async def test_failed_upgrade_restarts_stopped_app_and_notifies(
    notifications: MagicMock,
) -> None:
    update = _failing_update({"state": "STOPPED"})
    err = await _install_expecting_failure(update)

    assert err.translation_key == "app_update_job_failed"
    assert err.translation_placeholders["error"] == _MIGRATION_REASON
    update.coordinator.api.query.assert_any_await("app.start", ["a1"])
    notifications.async_create.assert_called_once()
    args, kwargs = notifications.async_create.call_args
    message = args[1]
    assert _MIGRATION_REASON in message
    assert "Traceback" not in message
    assert "1.1.40" in message
    assert "restart has been requested" in message
    assert kwargs["title"] == "TrueNAS app update failed: code-server"
    assert kwargs["notification_id"] == "truenas_ce_app_update_failed_TrueNAS_a1"


async def test_failed_upgrade_does_not_restart_running_app(
    notifications: MagicMock,
) -> None:
    update = _failing_update({"state": "RUNNING"})
    await _install_expecting_failure(update)

    methods = [c.args[0] for c in update.coordinator.api.query.await_args_list]
    assert "app.start" not in methods
    assert "still running" in notifications.async_create.call_args.args[1]


async def test_failed_upgrade_reports_failed_restart(
    notifications: MagicMock,
) -> None:
    update = _failing_update({"state": "STOPPED"}, start_result=None)
    update.coordinator.api.error = "start refused"
    await _install_expecting_failure(update)

    assert "start it manually" in notifications.async_create.call_args.args[1]


async def test_failed_upgrade_leaves_transitioning_app_alone(
    notifications: MagicMock,
) -> None:
    update = _failing_update({"state": "DEPLOYING"})
    await _install_expecting_failure(update)

    methods = [c.args[0] for c in update.coordinator.api.query.await_args_list]
    assert "app.start" not in methods
    assert "currently DEPLOYING" in notifications.async_create.call_args.args[1]


async def test_failed_upgrade_seen_first_by_coordinator_is_not_lost(
    notifications: MagicMock,
) -> None:
    """The coordinator's poll/push pass can observe the final job state first
    and clear ``update_jobid``; the entity must still fetch the outcome."""
    update = _failing_update({"state": "STOPPED"}, job_snapshots=[None])
    err = await _install_expecting_failure(update)

    assert err.translation_placeholders["error"] == _MIGRATION_REASON
    update.coordinator.api.query.assert_any_await(
        "core.get_jobs", params=[[["id", "=", 99]]]
    )
    update.coordinator.api.query.assert_any_await("app.start", ["a1"])
    notifications.async_create.assert_called_once()


async def test_failed_upgrade_with_unknown_app_state_skips_restart(
    notifications: MagicMock,
) -> None:
    update = _failing_update(None)
    await _install_expecting_failure(update)

    methods = [c.args[0] for c in update.coordinator.api.query.await_args_list]
    assert "app.start" not in methods
    assert "could not be determined" in notifications.async_create.call_args.args[1]


async def test_successful_upgrade_dismisses_previous_failure_notification(
    notifications: MagicMock,
) -> None:
    update = _install_ready_update([{"state": "SUCCESS"}])
    with patch("custom_components.truenas_ce.update.asyncio.sleep", AsyncMock()):
        await update.async_install(version=None, backup=False)

    notifications.async_dismiss.assert_called_once_with(
        update.hass, "truenas_ce_app_update_failed_TrueNAS_a1"
    )
    notifications.async_create.assert_not_called()


async def test_app_update_vanished_job_reports_unknown_outcome(
    notifications: MagicMock,
) -> None:
    """A job TrueNAS no longer reports must not be treated as a success."""
    update = _install_ready_update([{"state": "RUNNING"}, None])
    update.coordinator.api.query = AsyncMock(
        side_effect=lambda method, params=None: {
            "app.upgrade": 99,
            "core.get_jobs": [],
            "app.get_instance": {"state": "STOPPED"},
            "app.start": 7,
        }[method]
    )
    err = await _install_expecting_failure(update)

    assert update.coordinator.async_refresh_app_update_job.await_count == 2
    assert update._data["update_jobid"] == 0
    assert update.in_progress is False
    assert err.translation_key == "app_update_job_failed"
    assert "outcome is unknown" in err.translation_placeholders["error"]
    # No evidence the upgrade failed: the app's state is never touched.
    methods = [c.args[0] for c in update.coordinator.api.query.await_args_list]
    assert "app.get_instance" not in methods
    assert "app.start" not in methods
    message = notifications.async_create.call_args.args[1]
    assert "outcome is unknown" in message
    assert "left untouched" in message
    assert "has an unknown outcome" in message
    assert "Reason reported by TrueNAS" not in message
    title = notifications.async_create.call_args.kwargs["title"]
    assert title == "TrueNAS app update outcome unknown: a1"


def _coordinator_finished_update(recorded_state: str) -> TrueNASAppUpdate:
    """Coordinator saw the terminal state first; the direct lookup then fails."""
    update = _install_ready_update([])

    async def _refresh(uid: str) -> None:
        update._data["update_state"] = recorded_state
        update._data["update_jobid"] = 0

    update.coordinator.async_refresh_app_update_job = AsyncMock(side_effect=_refresh)
    update.coordinator.api.query = AsyncMock(
        side_effect=lambda method, params=None: {
            "app.upgrade": 99,
            "core.get_jobs": None,
            "app.get_instance": {"state": "RUNNING"},
        }[method]
    )
    return update


async def test_untracked_job_lookup_error_keeps_recorded_success(
    notifications: MagicMock,
) -> None:
    """A transient lookup error must not turn a successful upgrade into a failure."""
    update = _coordinator_finished_update("SUCCESS")
    with patch("custom_components.truenas_ce.update.asyncio.sleep", AsyncMock()):
        await update.async_install(version=None, backup=False)

    notifications.async_create.assert_not_called()
    notifications.async_dismiss.assert_called_once()


async def test_untracked_job_lookup_error_keeps_recorded_failure(
    notifications: MagicMock,
) -> None:
    update = _coordinator_finished_update("FAILED")
    err = await _install_expecting_failure(update)

    assert err.translation_placeholders["error"] == "FAILED"
    assert "Reason reported by TrueNAS" in notifications.async_create.call_args.args[1]


async def test_late_upgrade_failure_is_reported_after_timeout(
    notifications: MagicMock,
) -> None:
    """A job that fails after the install call gave up must still be handled."""
    update = _failing_update({"state": "STOPPED"})
    await _run_late_watch(update, "FAILED")

    update.coordinator.api.query.assert_any_await(
        "core.get_jobs", params=[[["id", "=", 99]]]
    )
    update.coordinator.api.query.assert_any_await("app.start", ["a1"])
    message = notifications.async_create.call_args.args[1]
    assert _MIGRATION_REASON in message
    assert "restart has been requested" in message


async def test_late_upgrade_success_dismisses_failure_notification(
    notifications: MagicMock,
) -> None:
    update = _install_ready_update([])
    update.coordinator.api.query = AsyncMock(
        return_value=[{"state": "SUCCESS", "id": 99}]
    )
    await _run_late_watch(update, "SUCCESS")

    notifications.async_create.assert_not_called()
    notifications.async_dismiss.assert_called_once_with(
        update.hass, "truenas_ce_app_update_failed_TrueNAS_a1"
    )


async def test_late_watch_stops_quietly_when_app_disappears(
    notifications: MagicMock,
) -> None:
    """An app removed mid-upgrade must not produce a bogus failure report."""
    update = _failing_update({"state": "STOPPED"})
    update._data["update_jobid"] = 99
    listeners: list = []
    update.coordinator.async_add_listener = lambda listener: (
        listeners.append(listener) or MagicMock()
    )
    watch = asyncio.create_task(update._async_watch_late_upgrade(99))
    await asyncio.sleep(0)

    update.coordinator.data["app"] = {}
    for listener in listeners:
        listener()
    await watch

    update.coordinator.api.query.assert_not_awaited()
    notifications.async_create.assert_not_called()


async def test_late_watch_does_not_report_a_still_running_job(
    notifications: MagicMock,
) -> None:
    update = _install_ready_update([])
    update.coordinator.api.query = AsyncMock(return_value=[{"state": "RUNNING"}])
    await _run_late_watch(update, "unknown")

    methods = [c.args[0] for c in update.coordinator.api.query.await_args_list]
    assert "app.start" not in methods
    notifications.async_create.assert_not_called()
    notifications.async_dismiss.assert_not_called()
