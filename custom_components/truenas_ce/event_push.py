"""Generic building blocks for event-subscription-based (push) data sources.

Deliberately independent of Home Assistant and of :mod:`.api`/:mod:`.coordinator`
so both pieces can be unit tested without a running TrueNAS connection or HA
test harness:

- ``SubscriptionCircuitBreaker`` decides when a subscription is too "chatty"
  (e.g. a flapping VM/container) and should fall back to polling for a while.
- ``SubscriptionPushConsumer`` drains a subscription's queue in the background
  and delivers debounced batches to a callback immediately, instead of
  waiting for the next poll tick to call ``get_subscription_events``.

Wiring these into a concrete ``coordinator.py`` data source (per the
``truenas-event-subscriptions-plan`` backlog note) means, per source:
- construct a ``SubscriptionCircuitBreaker`` alongside the existing
  ``_<name>_sub_id``/``_<name>_event_name`` state;
- after subscribing, construct a ``SubscriptionPushConsumer`` wrapping the
  returned queue, a callback that updates ``self.ds[<domain>]`` and calls
  ``self.async_set_updated_data(self.ds)``, and an ``on_trip`` callback that
  unsubscribes and clears the subscription state so the next poll cycle falls
  back to ``api.query(...)``;
- call ``start()``/``stop()`` from the same places ``_app_stats_sub_id`` is
  set/cleared today.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

FlushCallback = Callable[[list[Any]], Awaitable[None]]
TripCallback = Callable[[], Awaitable[None]]
TaskFactory = Callable[[Coroutine[Any, Any, None], str], "asyncio.Task[None]"]


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Tuning knobs for :class:`SubscriptionCircuitBreaker`."""

    message_threshold: int = 20
    """A drained batch larger than this counts as one breaching cycle."""

    consecutive_breaches_to_trip: int = 3
    """Number of consecutive breaching cycles before the breaker trips."""

    cooldown: timedelta = timedelta(minutes=5)
    """How long the breaker stays open before a retry may be attempted."""


class SubscriptionCircuitBreaker:
    """Rate-based circuit breaker for a single event subscription.

    Guards against a "crash-loop" style subscription (many state changes in a
    short time) flooding the coordinator with messages. Call
    :meth:`record_batch` once per drained batch; when it returns ``True`` the
    caller must unsubscribe and fall back to polling until
    :meth:`should_attempt_reset` returns ``True``.
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._consecutive_breaches = 0
        self._tripped = False
        self._opened_at: datetime | None = None

    @property
    def tripped(self) -> bool:
        """Return whether the breaker is currently open (fallback active)."""
        return self._tripped

    def record_batch(self, message_count: int) -> bool:
        """Record a drained batch's size; return True if it just tripped."""
        if self._tripped:
            return False

        if message_count > self._config.message_threshold:
            self._consecutive_breaches += 1
        else:
            self._consecutive_breaches = 0

        if self._consecutive_breaches < self._config.consecutive_breaches_to_trip:
            return False

        self._trip()
        return True

    def _trip(self) -> None:
        self._tripped = True
        self._opened_at = datetime.now(UTC)
        _LOGGER.warning(
            "Subscription circuit breaker tripped after %d consecutive "
            "breaching cycles (threshold=%d messages/batch); falling back "
            "to polling for %s",
            self._consecutive_breaches,
            self._config.message_threshold,
            self._config.cooldown,
        )

    def should_attempt_reset(self) -> bool:
        """Return True once the cooldown elapsed and a retry may be tried."""
        if not self._tripped or self._opened_at is None:
            return False
        return datetime.now(UTC) - self._opened_at >= self._config.cooldown

    def reset(self) -> None:
        """Close the breaker again after a successful retry."""
        self._tripped = False
        self._opened_at = None
        self._consecutive_breaches = 0


def _default_task_factory(
    coro: Coroutine[Any, Any, None], name: str
) -> asyncio.Task[None]:
    return asyncio.create_task(coro, name=name)


class SubscriptionPushConsumer:
    """Drains a subscription queue in the background and flushes debounced
    batches immediately, instead of waiting for the next poll tick.

    Couple this 1:1 to one subscription's lifecycle: create it in
    ``start_<name>()`` right after subscribing and call :meth:`stop` in
    ``stop_<name>()``/on unload/on reconnect, mirroring the existing
    ``_app_stats_sub_id`` lifecycle handling in ``coordinator.py``.
    """

    def __init__(
        self,
        queue: asyncio.Queue[Any],
        flush_callback: FlushCallback,
        *,
        debounce: timedelta = timedelta(seconds=1.5),
        breaker: SubscriptionCircuitBreaker | None = None,
        on_trip: TripCallback | None = None,
    ) -> None:
        self._queue = queue
        self._flush_callback = flush_callback
        self._debounce_seconds = debounce.total_seconds()
        self._breaker = breaker
        self._on_trip = on_trip
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Return whether the background drain task is currently active."""
        return self._task is not None and not self._task.done()

    def start(self, task_factory: TaskFactory = _default_task_factory) -> None:
        """Start the background drain task, unless already running.

        Pass a ``task_factory`` (e.g. ``hass.async_create_background_task``)
        so HA can track/cancel the task on shutdown; a plain
        ``asyncio.create_task`` is used by default for standalone use/tests.
        """
        if self.running:
            return
        self._task = task_factory(self._run(), f"{__name__}.push_consumer")

    async def stop(self) -> None:
        """Cancel the background drain task and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                batch = await self._collect_batch()
                if self._breaker is not None and self._breaker.record_batch(len(batch)):
                    if self._on_trip is not None:
                        await self._on_trip()
                    return
                await self._flush_callback(batch)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "SubscriptionPushConsumer: unhandled error, background task stopping"
            )
            raise

    async def _collect_batch(self) -> list[Any]:
        """Wait for one message, then coalesce more within the debounce window."""
        batch = [await self._queue.get()]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._debounce_seconds
        while (remaining := deadline - loop.time()) > 0:
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), remaining))
            except TimeoutError:
                break
        return batch
