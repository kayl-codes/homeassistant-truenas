"""Unit tests for ``custom_components.truenas_ce.event_push``.

Pure-asyncio tests (no Home Assistant import needed), loaded via the ``ep``
fixture like ``test_apiparser.py`` loads ``apiparser.py`` -- see
``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import ModuleType


# ---------------------------
#   SubscriptionCircuitBreaker
# ---------------------------
def _breaker(ep: ModuleType, **overrides):
    config = ep.CircuitBreakerConfig(
        message_threshold=overrides.pop("message_threshold", 5),
        consecutive_breaches_to_trip=overrides.pop("consecutive_breaches_to_trip", 3),
        cooldown=overrides.pop("cooldown", timedelta(minutes=5)),
    )
    return ep.SubscriptionCircuitBreaker(config)


def test_breaker_stays_closed_under_threshold(ep: ModuleType) -> None:
    breaker = _breaker(ep)
    for _ in range(10):
        assert breaker.record_batch(1) is False
    assert breaker.tripped is False


def test_breaker_trips_after_consecutive_breaches(ep: ModuleType) -> None:
    breaker = _breaker(ep, consecutive_breaches_to_trip=3)
    assert breaker.record_batch(10) is False
    assert breaker.record_batch(10) is False
    assert breaker.record_batch(10) is True
    assert breaker.tripped is True


def test_breaker_resets_streak_on_non_breaching_batch(ep: ModuleType) -> None:
    breaker = _breaker(ep, consecutive_breaches_to_trip=3)
    assert breaker.record_batch(10) is False
    assert breaker.record_batch(10) is False
    assert breaker.record_batch(1) is False  # under threshold, resets the streak
    assert breaker.record_batch(10) is False
    assert breaker.record_batch(10) is False
    assert breaker.tripped is False


def test_breaker_ignores_further_batches_once_tripped(ep: ModuleType) -> None:
    breaker = _breaker(ep, consecutive_breaches_to_trip=1)
    assert breaker.record_batch(10) is True
    assert breaker.record_batch(10) is False  # already open, no re-trip signal
    assert breaker.tripped is True


def test_breaker_should_attempt_reset_respects_cooldown(ep: ModuleType) -> None:
    breaker = _breaker(
        ep, consecutive_breaches_to_trip=1, cooldown=timedelta(seconds=999)
    )
    breaker.record_batch(10)
    assert breaker.tripped is True
    assert breaker.should_attempt_reset() is False


async def test_breaker_should_attempt_reset_after_cooldown(ep: ModuleType) -> None:
    breaker = _breaker(
        ep, consecutive_breaches_to_trip=1, cooldown=timedelta(milliseconds=1)
    )
    breaker.record_batch(10)
    await asyncio.sleep(0.02)
    assert breaker.should_attempt_reset() is True


def test_breaker_reset_closes_and_clears_streak(ep: ModuleType) -> None:
    breaker = _breaker(ep, consecutive_breaches_to_trip=2)
    breaker.record_batch(10)
    breaker.record_batch(10)
    assert breaker.tripped is True
    breaker.reset()
    assert breaker.tripped is False
    assert breaker.record_batch(10) is False  # streak was cleared, needs to build up


# ---------------------------
#   SubscriptionPushConsumer
# ---------------------------
async def test_consumer_flushes_after_debounce_window(ep: ModuleType) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    flushed: list[list] = []

    async def flush(batch: list) -> None:
        flushed.append(batch)

    consumer = ep.SubscriptionPushConsumer(
        queue, flush, debounce=timedelta(milliseconds=20)
    )
    consumer.start()
    try:
        await queue.put({"id": 1})
        await asyncio.sleep(0.05)
        assert flushed == [[{"id": 1}]]
    finally:
        await consumer.stop()


async def test_consumer_coalesces_messages_within_debounce_window(
    ep: ModuleType,
) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    flushed: list[list] = []

    async def flush(batch: list) -> None:
        flushed.append(batch)

    consumer = ep.SubscriptionPushConsumer(
        queue, flush, debounce=timedelta(milliseconds=50)
    )
    consumer.start()
    try:
        await queue.put({"id": 1})
        await asyncio.sleep(0.01)
        await queue.put({"id": 2})
        await asyncio.sleep(0.1)
        assert flushed == [[{"id": 1}, {"id": 2}]]
    finally:
        await consumer.stop()


async def test_consumer_trip_stops_without_flushing_and_calls_on_trip(
    ep: ModuleType,
) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    flushed: list[list] = []
    tripped = asyncio.Event()

    async def flush(batch: list) -> None:
        flushed.append(batch)

    async def on_trip() -> None:
        tripped.set()

    breaker = ep.SubscriptionCircuitBreaker(
        ep.CircuitBreakerConfig(message_threshold=0, consecutive_breaches_to_trip=1)
    )
    consumer = ep.SubscriptionPushConsumer(
        queue,
        flush,
        debounce=timedelta(milliseconds=10),
        breaker=breaker,
        on_trip=on_trip,
    )
    consumer.start()
    try:
        await queue.put({"id": 1})
        await asyncio.wait_for(tripped.wait(), timeout=1)
        assert not flushed
        assert breaker.tripped is True
        await asyncio.sleep(0.02)
        assert consumer.running is False
    finally:
        await consumer.stop()


async def test_consumer_stop_is_idempotent_and_cancels_cleanly(
    ep: ModuleType,
) -> None:
    queue: asyncio.Queue = asyncio.Queue()

    async def flush(batch: list) -> None:
        pass

    consumer = ep.SubscriptionPushConsumer(queue, flush)
    consumer.start()
    assert consumer.running is True
    await consumer.stop()
    assert consumer.running is False
    await consumer.stop()  # second stop() must be a no-op, not raise


async def test_consumer_start_is_a_noop_while_already_running(
    ep: ModuleType,
) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    calls: list[str] = []

    async def flush(batch: list) -> None:
        pass

    def task_factory(coro, name):
        calls.append(name)
        return asyncio.create_task(coro, name=name)

    consumer = ep.SubscriptionPushConsumer(queue, flush)
    consumer.start(task_factory=task_factory)
    consumer.start(task_factory=task_factory)
    assert len(calls) == 1  # second start() must not spawn a second task
    await consumer.stop()


async def test_consumer_custom_task_factory_is_used(ep: ModuleType) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    calls: list[str] = []

    async def flush(batch: list) -> None:
        pass

    def task_factory(coro, name):
        calls.append(name)
        return asyncio.create_task(coro, name=name)

    consumer = ep.SubscriptionPushConsumer(queue, flush)
    consumer.start(task_factory=task_factory)
    try:
        assert len(calls) == 1
    finally:
        await consumer.stop()
