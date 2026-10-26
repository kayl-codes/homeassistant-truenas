"""Unit tests for button.py."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from _fakes import make_coordinator
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.truenas_ce.button import (
    TrueNASButton,
    TrueNASMigrationRollbackButton,
    TrueNASPoolScrubButton,
    TrueNASRefreshButton,
    TrueNASStatisticsCleanupButton,
)
from custom_components.truenas_ce.button_types import (
    SENSOR_TYPES,
    TrueNASButtonEntityDescription,
)
from custom_components.truenas_ce.const import DOMAIN

_RUN_DESC = TrueNASButtonEntityDescription(
    key="rsynctask_run", name="Run", data_path="rsynctask", api_method="rsynctask.run"
)


def _make_button(*, data: dict | None = None, description=None) -> TrueNASButton:
    coordinator = make_coordinator(data={"rsynctask": {"t1": (data or {})}})
    return TrueNASButton(coordinator, description or _RUN_DESC, "t1")


async def test_async_press_missing_method_logs_and_skips() -> None:
    desc = TrueNASButtonEntityDescription(
        key="k", name="Run", data_path="rsynctask", api_method=None
    )
    button = _make_button(data={"id": 1}, description=desc)
    await button.async_press()
    button.coordinator.async_run_task.assert_not_awaited()


async def test_async_press_missing_object_id_logs_and_skips() -> None:
    button = _make_button(data={})
    await button.async_press()
    button.coordinator.async_run_task.assert_not_awaited()


async def test_async_press_triggers_run_task() -> None:
    button = _make_button(data={"id": 7})
    await button.async_press()
    button.coordinator.async_run_task.assert_awaited_once_with(
        "rsynctask.run", 7, "rsynctask"
    )


_SCRUB_DESCRIPTIONS = {d.key: d for d in SENSOR_TYPES if d.data_path == "scrub"}


def _make_scrub_button(
    *, data: dict | None = None, key: str = "scrub_run", pool: dict | None = None
) -> TrueNASPoolScrubButton:
    desc = _SCRUB_DESCRIPTIONS[key]
    pools = {"guid1": {"name": "tank", **pool}} if pool is not None else {}
    coordinator = make_coordinator(data={"scrub": {"t1": (data or {})}, "pool": pools})
    return TrueNASPoolScrubButton(coordinator, desc, "t1")


async def test_scrub_button_missing_pool_name_skips() -> None:
    button = _make_scrub_button(data={"id": 1, "enabled": True})
    await button.async_press()
    button.coordinator.api.query.assert_not_awaited()


async def test_scrub_button_runs_when_schedule_disabled() -> None:
    """A disabled scrub schedule must not block a manual scrub (#203)."""
    button = _make_scrub_button(data={"pool_name": "tank", "id": 1, "enabled": False})
    button.coordinator.api.query.return_value = 123
    await button.async_press()
    button.coordinator.api.query.assert_awaited_once_with(
        "pool.scrub.scrub", ["tank", "START"]
    )
    button.coordinator.set_optimistic_running.assert_called_once_with("scrub", 1)


async def test_scrub_button_success_sets_optimistic_running() -> None:
    button = _make_scrub_button(data={"pool_name": "tank", "id": 5, "enabled": True})
    button.coordinator.api.query.return_value = 123
    await button.async_press()
    button.coordinator.api.query.assert_awaited_once_with(
        "pool.scrub.scrub", ["tank", "START"]
    )
    button.coordinator.set_optimistic_running.assert_called_once_with("scrub", 5)
    # "Run scrub" has no state check: one pool refresh, after the action.
    button.coordinator.async_refresh_pools.assert_awaited_once()


async def test_scrub_button_failure_raises() -> None:
    button = _make_scrub_button(data={"pool_name": "tank", "id": 5, "enabled": True})
    button.coordinator.api.query.return_value = None
    button.coordinator.api.error = "middleware down"
    with pytest.raises(HomeAssistantError) as exc_info:
        await button.async_press()
    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "scrub_failed"
    assert exc_info.value.translation_placeholders == {
        "pool": "tank",
        "host": button.coordinator.host,
        "error": "middleware down",
    }


_TASK = {"pool_name": "tank", "id": 5, "enabled": True}
_RUNNING = {"scan_function": "SCRUB", "scrub_state": "SCANNING", "scrub_pause": None}
_PAUSED = {**_RUNNING, "scrub_pause": "2026-10-25T21:00:00Z"}
_FINISHED = {"scan_function": "SCRUB", "scrub_state": "FINISHED"}
_RESILVERING = {**_RUNNING, "scan_function": "RESILVER"}


@pytest.mark.parametrize(
    ("key", "pool", "action"),
    [
        ("scrub_pause", _RUNNING, "PAUSE"),
        ("scrub_resume", _PAUSED, "START"),
        ("scrub_stop", _RUNNING, "STOP"),
        ("scrub_stop", _PAUSED, "STOP"),
    ],
)
async def test_scrub_control_button_sends_action(
    key: str, pool: dict, action: str
) -> None:
    """Pause/resume/stop send their pool.scrub.scrub action (#204)."""
    button = _make_scrub_button(data=_TASK, key=key, pool=pool)
    button.coordinator.api.query.return_value = 123
    await button.async_press()
    button.coordinator.api.query.assert_awaited_once_with(
        "pool.scrub.scrub", ["tank", action]
    )
    # Once to check the current state, once to show the result.
    assert button.coordinator.async_refresh_pools.await_count == 2
    # Only "Run scrub" marks the task RUNNING; resume is a START too.
    button.coordinator.set_optimistic_running.assert_not_called()


@pytest.mark.parametrize(
    ("key", "pool"),
    [
        ("scrub_pause", _PAUSED),
        ("scrub_pause", _FINISHED),
        # Resume is START: on a pool without a paused scrub it would begin a new one.
        ("scrub_resume", _RUNNING),
        ("scrub_resume", _FINISHED),
        ("scrub_stop", _FINISHED),
        # A resilver reports SCANNING too, but pool.scrub.scrub cannot touch it.
        ("scrub_pause", _RESILVERING),
        ("scrub_stop", _RESILVERING),
        # No pool data for the task's pool.
        ("scrub_stop", None),
    ],
)
async def test_scrub_control_button_rejects_wrong_state(
    key: str, pool: dict | None
) -> None:
    button = _make_scrub_button(data=_TASK, key=key, pool=pool)
    with pytest.raises(ServiceValidationError) as exc_info:
        await button.async_press()
    assert exc_info.value.translation_key == f"{key}_not_possible"
    assert exc_info.value.translation_placeholders == {"pool": "tank"}
    button.coordinator.api.query.assert_not_awaited()


@pytest.mark.parametrize(
    ("key", "pool"),
    [("scrub_pause", _RUNNING), ("scrub_resume", _PAUSED), ("scrub_stop", _PAUSED)],
)
async def test_scrub_control_button_failure_raises(key: str, pool: dict) -> None:
    button = _make_scrub_button(data=_TASK, key=key, pool=pool)
    button.coordinator.api.query.return_value = None
    button.coordinator.api.error = "middleware down"
    with pytest.raises(HomeAssistantError) as exc_info:
        await button.async_press()
    assert exc_info.value.translation_key == "scrub_failed"
    # The action was sent, and TrueNAS did not take it.
    action = _SCRUB_DESCRIPTIONS[key].scrub_action
    button.coordinator.api.query.assert_awaited_once_with(
        "pool.scrub.scrub", ["tank", action]
    )
    # Only the pre-check refresh ran; no result to show.
    button.coordinator.async_refresh_pools.assert_awaited_once()


@pytest.mark.parametrize(
    ("stale", "error"),
    [(True, "TrueNAS returned no usable pool data"), (False, "TimeoutError")],
)
@pytest.mark.parametrize(
    ("key", "pool"),
    [("scrub_pause", _RUNNING), ("scrub_resume", _PAUSED), ("scrub_stop", _PAUSED)],
)
async def test_scrub_control_button_refuses_unreadable_state(
    key: str, pool: dict, stale: bool, error: str
) -> None:
    """Without a fresh pool state, no action is sent (#204)."""
    button = _make_scrub_button(data=_TASK, key=key, pool=pool)
    if stale:
        # pool.query failed without raising: the old snapshot is kept.
        button.coordinator.state.stale_endpoints = frozenset({"pool"})
    else:
        button.coordinator.async_refresh_pools.side_effect = TimeoutError
    with pytest.raises(HomeAssistantError) as exc_info:
        await button.async_press()
    assert exc_info.value.translation_key == "scrub_failed"
    assert exc_info.value.translation_placeholders["pool"] == "tank"
    assert exc_info.value.translation_placeholders["error"] == error
    button.coordinator.api.query.assert_not_awaited()


async def test_scrub_control_button_ignores_an_earlier_poll_failure() -> None:
    """Only this refresh decides; a failed poll before it does not block."""
    button = _make_scrub_button(data=_TASK, key="scrub_pause", pool=_RUNNING)
    button.coordinator.is_data_path_failing.return_value = True
    button.coordinator.api.query.return_value = 123
    await button.async_press()
    button.coordinator.api.query.assert_awaited_once()


async def test_scrub_control_button_tolerates_failed_refresh_after_action() -> None:
    """TrueNAS took the action; a failed re-read afterwards is no failed press."""
    button = _make_scrub_button(data=_TASK, key="scrub_pause", pool=_RUNNING)
    button.coordinator.api.query.return_value = 123
    button.coordinator.async_refresh_pools.side_effect = [None, TimeoutError]
    await button.async_press()
    button.coordinator.api.query.assert_awaited_once_with(
        "pool.scrub.scrub", ["tank", "PAUSE"]
    )


async def test_scrub_run_button_tolerates_failed_refresh_after_start() -> None:
    """Run scrub: a failed re-read after the start still marks the task RUNNING."""
    button = _make_scrub_button(data=_TASK)
    button.coordinator.api.query.return_value = 123
    button.coordinator.async_refresh_pools.side_effect = TimeoutError
    await button.async_press()
    button.coordinator.api.query.assert_awaited_once_with(
        "pool.scrub.scrub", ["tank", "START"]
    )
    button.coordinator.set_optimistic_running.assert_called_once_with("scrub", 5)


async def test_scrub_control_button_tolerates_stale_pools_after_action(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stale pools after the action are logged, not raised: TrueNAS took it."""
    button = _make_scrub_button(data=_TASK, key="scrub_pause", pool=_RUNNING)
    coordinator = button.coordinator
    coordinator.api.query.return_value = 123

    async def _stale_once_sent() -> None:
        # Fresh for the state check, stale for the re-read after the action.
        if coordinator.api.query.await_count:
            coordinator.state.stale_endpoints = frozenset({"pool"})

    coordinator.async_refresh_pools.side_effect = _stale_once_sent
    await button.async_press()
    assert coordinator.async_refresh_pools.await_count == 2
    assert "could not re-read the pools" in caplog.text


async def test_scrub_lock_is_released_after_a_failed_press() -> None:
    """A press that fails must not block the next one."""
    button = _make_scrub_button(data=_TASK, key="scrub_stop", pool=_RUNNING)
    query = button.coordinator.api.query
    query.return_value = None  # TrueNAS refuses the first STOP
    with pytest.raises(HomeAssistantError) as exc_info:
        await button.async_press()
    assert exc_info.value.translation_key == "scrub_failed"
    assert not button.coordinator.scrub_lock.locked()
    query.return_value = 123
    async with asyncio.timeout(1):
        await button.async_press()
    assert query.await_count == 2


async def test_scrub_control_buttons_do_not_overlap() -> None:
    """Stop and resume pressed together run one after the other (#204)."""
    stop = _make_scrub_button(data=_TASK, key="scrub_stop", pool=_PAUSED)
    coordinator = stop.coordinator
    resume = TrueNASPoolScrubButton(
        coordinator, _SCRUB_DESCRIPTIONS["scrub_resume"], "t1"
    )
    pool = coordinator.data["pool"]["guid1"]

    async def _send(_method: str, params: list) -> int:
        # Yield while the action is in flight, then let TrueNAS apply it.
        await asyncio.sleep(0)
        if params[1] == "STOP":
            pool.update(scrub_state="CANCELED", scrub_pause=None)
        return 123

    coordinator.api.query.side_effect = _send
    results = await asyncio.gather(
        stop.async_press(), resume.async_press(), return_exceptions=True
    )
    assert results[0] is None
    # Resume saw the stopped scrub and did not start a new one.
    assert isinstance(results[1], ServiceValidationError)
    coordinator.api.query.assert_awaited_once_with("pool.scrub.scrub", ["tank", "STOP"])


async def test_scrub_resume_checks_the_refreshed_state() -> None:
    """A scrub stopped on TrueNAS since the last poll must not be restarted."""
    button = _make_scrub_button(data=_TASK, key="scrub_resume", pool=_PAUSED)
    pools = button.coordinator.data["pool"]

    async def _stopped_meanwhile() -> None:
        pools["guid1"].update(scrub_state="CANCELED", scrub_pause=None)

    button.coordinator.async_refresh_pools.side_effect = _stopped_meanwhile
    with pytest.raises(ServiceValidationError):
        await button.async_press()
    button.coordinator.api.query.assert_not_awaited()


async def test_scrub_control_button_picks_the_tasks_pool() -> None:
    """The state check uses the task's pool, not just the first one."""
    button = _make_scrub_button(data=_TASK, key="scrub_pause", pool=_FINISHED)
    pools = button.coordinator.data["pool"]
    button.coordinator.data["pool"] = {"guid0": {"name": "other", **_RUNNING}, **pools}
    with pytest.raises(ServiceValidationError):
        await button.async_press()
    button.coordinator.api.query.assert_not_awaited()


def test_scrub_control_messages_exist() -> None:
    """Every state-checked scrub button has its "<key>_not_possible" message."""
    strings = json.loads(
        (
            Path(__file__).parents[1] / "custom_components/truenas_ce/strings.json"
        ).read_text(encoding="utf-8")
    )
    checked = [d.key for d in _SCRUB_DESCRIPTIONS.values() if d.scrub_states]
    assert checked == ["scrub_pause", "scrub_resume", "scrub_stop"]
    for key in checked:
        assert f"{key}_not_possible" in strings["exceptions"]


def test_statistics_cleanup_button_init_and_available() -> None:
    coordinator = make_coordinator()
    button = TrueNASStatisticsCleanupButton(coordinator)
    assert button.unique_id == "truenas-statistics_cleanup"
    assert button.available is False
    coordinator.orphaned_statistics = ["sensor.truenas_old"]
    assert button.available is True


async def test_statistics_cleanup_button_press_clears_statistics() -> None:
    coordinator = make_coordinator()
    button = TrueNASStatisticsCleanupButton(coordinator)
    await button.async_press()
    coordinator.async_clear_orphaned_statistics.assert_awaited_once()


def test_migration_rollback_button_available_follows_coordinator() -> None:
    """Availability is the coordinator's verdict (legacy entry *and* files, #201)."""
    coordinator = make_coordinator()
    button = TrueNASMigrationRollbackButton(coordinator)
    assert button.available is False
    coordinator.rollback_possible.return_value = True
    assert button.available is True


async def test_migration_rollback_button_press_raises_issue() -> None:
    coordinator = make_coordinator()
    button = TrueNASMigrationRollbackButton(coordinator)
    await button.async_press()
    coordinator.raise_migration_rollback_issue.assert_called_once()


async def test_refresh_button_press_refreshes_coordinator() -> None:
    coordinator = make_coordinator()
    button = TrueNASRefreshButton(coordinator)
    await button.async_press()
    coordinator.async_refresh.assert_awaited_once()
