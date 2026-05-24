"""Programmatic scheduling (#6) and the awaiting_review task queue (#5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lazybridge import Store

from lazypulse import (
    InboundMessage,
    PulseAgent,
    approve_task,
    pending_tasks,
    reject_task,
    store_keys,
)
from lazypulse.models import Identity, PulseRecord, TrustLevel
from lazypulse.policy import PulsePolicy
from lazypulse.testing import FakeClock, MockAdapter, MockEngine


def _records(store: Store) -> list[PulseRecord]:
    return [PulseRecord.model_validate(store.read(k)) for k in list(store.keys()) if k.startswith(store_keys.TASK_PREFIX)]


def _record(store: Store, task_id: str) -> PulseRecord:
    return PulseRecord.model_validate(store.read(store_keys.task_key(task_id)))


# --- Scheduling -------------------------------------------------------- #


async def test_schedule_runs_on_next_tick() -> None:
    clock = FakeClock()
    store = Store()
    engine = MockEngine(["done"])
    pulse = PulseAgent(name="p", engine=engine, store=store, clock=clock)  # no adapters → no policy needed
    task_id = pulse.schedule("do the thing")
    report = await pulse.tick_once()
    assert report.completed == 1
    assert _record(store, task_id).status == "completed"


async def test_schedule_at_future_not_run_until_due() -> None:
    clock = FakeClock()
    store = Store()
    engine = MockEngine(["done"])
    pulse = PulseAgent(name="p", engine=engine, store=store, clock=clock)
    task_id = pulse.schedule_at("later", when=clock.now + timedelta(hours=1))

    await pulse.tick_once()
    assert _record(store, task_id).status == "scheduled"  # not yet due
    assert len(engine.calls) == 0

    clock.advance(3601)
    await pulse.tick_once()
    assert _record(store, task_id).status == "completed"


async def test_schedule_after_uses_relative_offset() -> None:
    clock = FakeClock()
    store = Store()
    pulse = PulseAgent(name="p", engine=MockEngine(["x"]), store=store, clock=clock)
    task_id = pulse.schedule_after("soon", seconds=30)
    rec = _record(store, task_id)
    assert rec.run_at == clock.now + timedelta(seconds=30)


async def test_scheduled_tasks_are_trusted_system() -> None:
    clock = FakeClock()
    store = Store()
    pulse = PulseAgent(name="p", engine=MockEngine(["x"]), store=store, clock=clock)
    task_id = pulse.schedule("trusted local task")
    rec = _record(store, task_id)
    assert rec.identity is not None and rec.identity.trust == TrustLevel.SYSTEM
    assert rec.decision == "allow"


# --- Review queue ------------------------------------------------------ #


class _OwnerSendPolicy(PulsePolicy):
    """Owner whose external sends require confirmation → awaiting_review."""

    def classify(self, inbound: InboundMessage) -> Identity:
        return Identity(sender=inbound.sender_raw, trust=TrustLevel.OWNER_VERIFIED_EMAIL)


def _send_msg(mid: str = "1") -> InboundMessage:
    return InboundMessage(
        source="mock",
        message_id=mid,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        sender_raw="me@x",
        text="send the report",
        requested_action="external_send",  # type: ignore[arg-type]
    )


async def _agent_with_pending(store: Store, clock: FakeClock) -> tuple[PulseAgent, MockEngine, str]:
    engine = MockEngine(["sent"])
    pulse = PulseAgent(
        name="p",
        engine=engine,
        store=store,
        clock=clock,
        policy=_OwnerSendPolicy(owner_emails=["me@x"]),
        adapters=[MockAdapter([_send_msg()])],
    )
    await pulse.tick_once()
    task = pending_tasks(store)[0]
    return pulse, engine, task.task_id


async def test_pending_tasks_lists_awaiting_review() -> None:
    store, clock = Store(), FakeClock()
    _, engine, _task_id = await _agent_with_pending(store, clock)
    assert len(pending_tasks(store)) == 1
    assert len(engine.calls) == 0  # not run while parked


async def test_approve_task_runs_on_next_tick() -> None:
    store, clock = Store(), FakeClock()
    pulse, engine, task_id = await _agent_with_pending(store, clock)
    assert approve_task(store, task_id) is True
    assert _record(store, task_id).status == "scheduled"
    await pulse.tick_once()
    assert _record(store, task_id).status == "completed"
    assert len(engine.calls) == 1


async def test_reject_task_marks_rejected() -> None:
    store, clock = Store(), FakeClock()
    pulse, engine, task_id = await _agent_with_pending(store, clock)
    assert reject_task(store, task_id, "not appropriate") is True
    rec = _record(store, task_id)
    assert rec.status == "rejected"
    assert rec.error == "not appropriate"
    await pulse.tick_once()
    assert len(engine.calls) == 0  # stays rejected, never runs


async def test_approve_twice_second_is_noop() -> None:
    store, clock = Store(), FakeClock()
    _, _, task_id = await _agent_with_pending(store, clock)
    assert approve_task(store, task_id) is True
    # No longer awaiting_review → second approve returns False.
    assert approve_task(store, task_id) is False


async def test_approve_unknown_task_returns_false() -> None:
    assert approve_task(Store(), "does-not-exist") is False
    assert reject_task(Store(), "does-not-exist", "reason") is False
