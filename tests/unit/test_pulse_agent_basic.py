"""PulseAgent construction, attribute propagation, and lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from lazybridge import Store

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.models import PulseRecord
from lazypulse.testing import MockAdapter, MockEngine


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _msg(mid: str = "1", text: str = "hello") -> InboundMessage:
    return InboundMessage(source="mock", message_id=mid, received_at=_now(), text=text)


def _only_record(store: Store) -> PulseRecord:
    recs = [PulseRecord.model_validate(store.read(k)) for k in list(store.keys()) if k.startswith(store_keys.TASK_PREFIX)]
    assert len(recs) == 1, f"expected one task record, got {len(recs)}"
    return recs[0]


def test_init_propagates_agent_kwargs() -> None:
    store = Store()
    engine = MockEngine()
    pulse = PulseAgent(name="pulse", engine=engine, store=store, output=str)
    assert pulse.name == "pulse"
    assert pulse.engine is engine
    assert pulse.store is store
    assert pulse.output is str
    # subclass of Agent — inherits the run method unchanged
    from lazybridge import Agent

    assert isinstance(pulse, Agent)


def test_store_is_required() -> None:
    with pytest.raises(ValueError, match="requires store"):
        PulseAgent(name="pulse", engine=MockEngine())


def test_init_creates_independent_semaphores() -> None:
    a = PulseAgent(name="a", engine=MockEngine(), store=Store(), max_concurrent_inbound=2)
    b = PulseAgent(name="b", engine=MockEngine(), store=Store(), max_concurrent_inbound=5)
    assert a._sema is not b._sema
    assert a._max_concurrent == 2 and b._max_concurrent == 5


async def test_start_twice_raises() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store(), tick_seconds=10)
    await pulse.start()
    with pytest.raises(RuntimeError, match="already started"):
        await pulse.start()
    await pulse.stop()


async def test_stop_without_start_is_noop() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store())
    await pulse.stop()  # must not raise
    assert not pulse.is_running()


async def test_stop_twice_is_noop() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store(), tick_seconds=10)
    await pulse.start()
    await pulse.stop()
    await pulse.stop()  # second stop is a no-op
    assert not pulse.is_running()


async def test_running_context_manager_starts_and_stops() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store(), tick_seconds=10)
    async with pulse.running():
        assert pulse.is_running()
    assert not pulse.is_running()


async def test_running_cleans_up_on_exception() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store(), tick_seconds=10)
    with pytest.raises(ValueError):
        async with pulse.running():
            assert pulse.is_running()
            raise ValueError("boom")
    assert not pulse.is_running()


async def test_no_policy_allows_message() -> None:
    store = Store()
    pulse = PulseAgent(
        name="pulse",
        engine=MockEngine(["done"]),
        store=store,
        adapters=[MockAdapter([_msg()])],
    )
    report = await pulse.tick_once()
    assert report.drained == 1
    assert report.completed == 1
    rec = _only_record(store)
    assert rec.status == "completed"
    assert rec.worker_text == "done"


async def test_running_loop_processes_message_end_to_end() -> None:
    store = Store()
    engine = MockEngine(["handled"])
    pulse = PulseAgent(
        name="pulse",
        engine=engine,
        store=store,
        adapters=[MockAdapter([_msg()])],
        tick_seconds=0.02,
    )
    async with pulse.running():
        for _ in range(50):
            await asyncio.sleep(0.02)
            if engine.calls:
                break
    await asyncio.sleep(0.05)
    rec = _only_record(store)
    assert rec.status == "completed"


async def test_idempotent_message_not_run_twice() -> None:
    store = Store()
    engine = MockEngine(["done"])
    # Two adapters emitting the SAME message id → one task only.
    pulse = PulseAgent(
        name="pulse",
        engine=engine,
        store=store,
        adapters=[MockAdapter([_msg("dup")]), MockAdapter([_msg("dup")])],
    )
    await pulse.tick_once()
    recs = [k for k in list(store.keys()) if k.startswith(store_keys.TASK_PREFIX)]
    assert len(recs) == 1
    assert len(engine.calls) == 1


async def test_requested_action_propagates_to_record() -> None:
    store = Store()
    msg = InboundMessage(
        source="mock", message_id="x", received_at=_now(), text="rm -rf", requested_action="destructive"
    )
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=store, adapters=[MockAdapter([msg])])
    await pulse.tick_once()
    rec = _only_record(store)
    assert rec.action_class.value == "destructive"
