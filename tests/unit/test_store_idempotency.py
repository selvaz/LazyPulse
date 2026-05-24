"""Idempotency guarantees across the intake → execute pipeline."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from lazybridge import Store

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.adapters.gmail.inbox import GmailInbox, GmailInboxConfig
from lazypulse.adapters.webhook import WebhookAdapter
from lazypulse.models import PulseRecord
from lazypulse.testing import FakeClock, MockAdapter, MockEngine


def _msg(mid: str) -> InboundMessage:
    return InboundMessage(source="mock", message_id=mid, received_at=datetime(2026, 1, 1, tzinfo=UTC), text="hi")


def _task_keys(store: Store) -> list[str]:
    return [k for k in list(store.keys()) if k.startswith(store_keys.TASK_PREFIX)]


async def test_event_marker_prevents_reprocessing_across_ticks() -> None:
    store = Store()
    engine = MockEngine(["done"])
    # An adapter that keeps re-emitting the same message id every drain.
    class RepeatAdapter:
        name = "repeat"

        async def drain(self, *, store, session=None):
            return [_msg("same")]

    pulse = PulseAgent(
        name="p", engine=engine, store=store, clock=FakeClock(), adapters=[RepeatAdapter()], unsafe_allow_all=True
    )
    await pulse.tick_once()
    await pulse.tick_once()
    await pulse.tick_once()
    assert len(_task_keys(store)) == 1
    assert len(engine.calls) == 1


async def test_duplicate_id_across_two_adapters_makes_one_task() -> None:
    store = Store()
    engine = MockEngine(["done"])
    pulse = PulseAgent(
        name="p",
        engine=engine,
        store=store,
        clock=FakeClock(),
        adapters=[MockAdapter([_msg("dup")], name="a"), MockAdapter([_msg("dup")], name="b")],
        unsafe_allow_all=True,
    )
    report = await pulse.tick_once()
    assert report.drained == 2
    assert report.duplicates == 1
    assert len(_task_keys(store)) == 1


async def test_cas_lets_only_one_claim_a_scheduled_record() -> None:
    store = Store()
    clock = FakeClock()
    rec = PulseRecord(text="contended", status="scheduled", created_at=clock.now, run_at=clock.now)
    key = store_keys.task_key(rec.task_id)
    store.write(key, rec.model_dump(mode="json"))
    expected = store.read(key)

    # First claim wins, second sees a moved value and fails.
    won_first = store.compare_and_swap(key, expected, {**expected, "status": "running"})
    won_second = store.compare_and_swap(key, expected, {**expected, "status": "running"})
    assert won_first is True
    assert won_second is False


async def test_webhook_nonce_dedupe_in_handler() -> None:
    adapter = WebhookAdapter()
    transport = httpx.ASGITransport(app=adapter.asgi_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        a = await client.post("/inbound", json={"message_id": "1", "text": "x", "nonce": "z"})
        b = await client.post("/inbound", json={"message_id": "2", "text": "y", "nonce": "z"})
    assert a.status_code == 202
    assert b.status_code == 409


async def test_gmail_at_least_once_until_event_marker_then_dedupes() -> None:
    # The adapter re-emits until the PulseAgent has recorded the message
    # (its central EVENT marker exists), so a crash before record-write can't
    # lose it. Once recorded, it's skipped.
    store = Store()
    svc = _FakeGmail({"a": _resource(), "b": _resource()})
    inbox = GmailInbox(svc, GmailInboxConfig(account="me@x"))

    first = await inbox.drain(store=store, session=None)
    assert len(first) == 2
    # No markers yet → re-drain re-emits (at-least-once).
    again = await inbox.drain(store=store, session=None)
    assert len(again) == 2

    # Simulate the PulseAgent recording both messages.
    for m in first:
        store.write(store_keys.event_key(m.message_id), {"task_id": "t"})
    assert await inbox.drain(store=store, session=None) == []


async def test_gmail_dedup_survives_restart_via_event_marker(tmp_path) -> None:
    db = str(tmp_path / "s.db")
    store = Store(db=db)
    inbox = GmailInbox(_FakeGmail({"a": _resource()}), GmailInboxConfig(account="me@x"))
    msgs = await inbox.drain(store=store, session=None)
    store.write(store_keys.event_key(msgs[0].message_id), {"task_id": "t"})  # PulseAgent records it
    store.close()

    store2 = Store(db=db)
    inbox2 = GmailInbox(_FakeGmail({"a": _resource()}), GmailInboxConfig(account="me@x"))
    second = await inbox2.drain(store=store2, session=None)
    assert second == []  # the persisted EVENT marker survives a restart
    store2.close()


def _resource() -> dict:
    return {"payload": {"headers": [{"name": "From", "value": "x@y.com"}]}, "snippet": "hi"}


class _FakeGmail:
    def __init__(self, messages: dict[str, dict]) -> None:
        self.messages = messages
        self.get_calls = 0

    def list_message_ids(self, *, query=None, max_results=25):
        return list(self.messages.keys())

    def get_message(self, message_id):
        self.get_calls += 1
        return self.messages[message_id]
