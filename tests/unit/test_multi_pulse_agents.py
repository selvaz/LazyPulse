"""Multiple PulseAgents sharing one Store must not race or double-run."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.models import PulseRecord
from lazypulse.review import StoreReviewerUI, pending_reviews, respond
from lazypulse.testing import FakeClock, MockAdapter, MockEngine


def _msg(mid: str) -> InboundMessage:
    return InboundMessage(source="mock", message_id=mid, received_at=datetime(2026, 1, 1, tzinfo=UTC), text="hi")


def _records(store: Store) -> list[PulseRecord]:
    return [PulseRecord.model_validate(store.read(k)) for k in list(store.keys()) if k.startswith(store_keys.TASK_PREFIX)]


async def test_two_agents_each_own_adapter_share_store() -> None:
    store = Store()
    clock = FakeClock()
    e1, e2 = MockEngine(["a"]), MockEngine(["b"])
    p1 = PulseAgent(name="p1", engine=e1, store=store, clock=clock, adapters=[MockAdapter([_msg("1")])])
    p2 = PulseAgent(name="p2", engine=e2, store=store, clock=clock, adapters=[MockAdapter([_msg("2")])])
    await asyncio.gather(p1.tick_once(), p2.tick_once())
    recs = _records(store)
    assert len(recs) == 2
    assert all(r.status == "completed" for r in recs)


async def test_contended_scheduled_record_runs_once() -> None:
    store = Store()
    clock = FakeClock()
    # Pre-seed one scheduled task; two agents tick concurrently against it.
    rec = PulseRecord(text="contended", status="scheduled", created_at=clock.now, run_at=clock.now)
    store.write(store_keys.task_key(rec.task_id), rec.model_dump(mode="json"))
    e1, e2 = MockEngine(["a"]), MockEngine(["b"])
    p1 = PulseAgent(name="p1", engine=e1, store=store, clock=clock)
    p2 = PulseAgent(name="p2", engine=e2, store=store, clock=clock)
    await asyncio.gather(p1.tick_once(), p2.tick_once())
    # Exactly one engine ran the task; CAS prevented the double-run.
    assert len(e1.calls) + len(e2.calls) == 1
    assert _records(store)[0].status == "completed"


async def test_distinct_task_ids_do_not_collide() -> None:
    store = Store()
    clock = FakeClock()
    e1, e2 = MockEngine(["a"]), MockEngine(["b"])
    p1 = PulseAgent(name="p1", engine=e1, store=store, clock=clock, adapters=[MockAdapter([_msg(f"a{i}") for i in range(3)])])
    p2 = PulseAgent(name="p2", engine=e2, store=store, clock=clock, adapters=[MockAdapter([_msg(f"b{i}") for i in range(3)])])
    await asyncio.gather(p1.tick_once(), p2.tick_once())
    recs = _records(store)
    assert len({r.task_id for r in recs}) == 6


async def test_review_request_visible_across_clients() -> None:
    # A review parked by one component is answerable by any other holding the
    # same Store — the basis for a remote reviewer.
    store = Store()
    ui = StoreReviewerUI(store, poll_interval=0.01, timeout=2.0)
    task = asyncio.create_task(ui.prompt("approve?", tools=[], output_type=str))
    await asyncio.sleep(0.05)
    reqs = pending_reviews(store)
    assert len(reqs) == 1
    respond(store, reqs[0]["review_id"], "approved")
    assert await task == "approved"
