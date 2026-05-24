"""PulseAgent construction, attribute propagation, and lifecycle."""

from __future__ import annotations

import time
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


def test_adapters_without_policy_raises() -> None:
    with pytest.raises(ValueError, match="adapters but no policy"):
        PulseAgent(name="pulse", engine=MockEngine(), store=Store(), adapters=[MockAdapter([_msg()])])


def test_adapters_without_policy_allowed_with_unsafe_flag() -> None:
    PulseAgent(
        name="pulse", engine=MockEngine(), store=Store(), adapters=[MockAdapter([_msg()])], unsafe_allow_all=True
    )


def test_no_adapters_no_policy_is_fine() -> None:
    # A schedule-only agent has no external intake, so no policy is required.
    PulseAgent(name="pulse", engine=MockEngine(), store=Store())


def test_duplicate_adapter_names_rejected() -> None:
    # Reply routing keys on the adapter name, so two adapters sharing a name
    # would silently send a completed task's reply through the wrong client.
    with pytest.raises(ValueError, match="share name"):
        PulseAgent(
            name="pulse",
            engine=MockEngine(),
            store=Store(),
            unsafe_allow_all=True,
            adapters=[MockAdapter([], name="dup"), MockAdapter([], name="dup")],
        )


def test_max_concurrent_configured() -> None:
    a = PulseAgent(name="a", engine=MockEngine(), store=Store(), max_concurrent_inbound=2)
    b = PulseAgent(name="b", engine=MockEngine(), store=Store(), max_concurrent_inbound=5)
    assert a._max_concurrent == 2 and b._max_concurrent == 5


def test_start_twice_raises() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store(), tick_seconds=10)
    pulse.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            pulse.start()
    finally:
        pulse.stop()


def test_stop_without_start_is_noop() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store())
    pulse.stop()  # must not raise
    assert not pulse.is_running()


def test_stop_twice_is_noop() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store(), tick_seconds=10)
    pulse.start()
    pulse.stop()
    pulse.stop()  # second stop is a no-op
    assert not pulse.is_running()


def test_running_context_manager_starts_and_stops() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store(), tick_seconds=10)
    with pulse.running():
        assert pulse.is_running()
    assert not pulse.is_running()


def test_running_cleans_up_on_exception() -> None:
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=Store(), tick_seconds=10)
    with pytest.raises(ValueError), pulse.running():
        assert pulse.is_running()
        raise ValueError("boom")
    assert not pulse.is_running()


async def test_no_policy_allows_message() -> None:
    store = Store()
    pulse = PulseAgent(
        unsafe_allow_all=True,
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


def test_running_loop_processes_message_end_to_end() -> None:
    store = Store()
    engine = MockEngine(["handled"])
    pulse = PulseAgent(
        unsafe_allow_all=True,
        name="pulse",
        engine=engine,
        store=store,
        adapters=[MockAdapter([_msg()])],
        tick_seconds=0.02,
    )
    with pulse.running():
        deadline = time.monotonic() + 2.0
        while not engine.calls and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.05)
    rec = _only_record(store)
    assert rec.status == "completed"


def test_tick_then_background_loop_no_cross_loop_error() -> None:
    # tick() runs on a throwaway loop; start() runs its own loop in a thread.
    # A per-instance Semaphore would bind to the first loop and blow up in the
    # second — the per-tick semaphore must avoid that entirely.
    store = Store()
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["one", "two"]),
        store=store,
        adapters=[MockAdapter([_msg("1")])],
        unsafe_allow_all=True,
        tick_seconds=0.02,
    )
    pulse.tick()  # loop A
    scheduled_id = pulse.schedule("a second task")
    with pulse.running():  # loop B (background thread)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            rec = PulseRecord.model_validate(store.read(store_keys.task_key(scheduled_id)))
            if rec.status == "completed":
                break
            time.sleep(0.02)
    rec = PulseRecord.model_validate(store.read(store_keys.task_key(scheduled_id)))
    assert rec.status == "completed"


def test_one_shot_tick_is_synchronous() -> None:
    store = Store()
    pulse = PulseAgent(
        unsafe_allow_all=True,
        name="pulse",
        engine=MockEngine(["done"]),
        store=store,
        adapters=[MockAdapter([_msg()])],
    )
    report = pulse.tick()  # sync, no await
    assert report.completed == 1
    assert _only_record(store).status == "completed"


async def test_idempotent_message_not_run_twice() -> None:
    store = Store()
    engine = MockEngine(["done"])
    # Two (distinctly named) adapters emitting the SAME message id → one task.
    pulse = PulseAgent(
        unsafe_allow_all=True,
        name="pulse",
        engine=engine,
        store=store,
        adapters=[MockAdapter([_msg("dup")], name="a"), MockAdapter([_msg("dup")], name="b")],
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
    pulse = PulseAgent(name="pulse", engine=MockEngine(), store=store, adapters=[MockAdapter([msg])], unsafe_allow_all=True)
    await pulse.tick_once()
    rec = _only_record(store)
    assert rec.action_class.value == "destructive"
