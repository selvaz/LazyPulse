"""GmailPushInbox: event-driven intake, watch lifecycle, at-least-once cursor.

Uses ``FakeGmailService`` (lazytools.testing) for the Gmail surface and
httpx's ASGI transport for the Pub/Sub push endpoint — no network, no
Google. The central claims under test:

* idle steady-state makes **zero** Gmail API calls (the whole point);
* a push notification triggers exactly one ``history.list`` sync;
* the history cursor advances only after the PulseAgent has recorded the
  batch (at-least-once across a crash between drain and record);
* endpoint auth (403 on bad token), poison-payload acking, cross-account
  notification filtering;
* watch arming and renewal; expired-cursor resync.
"""

from __future__ import annotations

import base64
import json

import httpx
from lazybridge import Store
from lazytools.testing import FakeGmailService

from lazypulse import store_keys
from lazypulse.adapters.gmail import GmailPushConfig, GmailPushInbox
from lazypulse.testing import FakeClock

ACCOUNT = "me@example.com"


def _adapter(
    fake: FakeGmailService,
    clock: FakeClock,
    **cfg_overrides,
) -> GmailPushInbox:
    cfg = GmailPushConfig(account=ACCOUNT, shared_token="s3cret", **cfg_overrides)
    return GmailPushInbox(fake, cfg, clock=clock)


def _push_body(account: str = ACCOUNT, history_id: str = "1002") -> dict:
    data = base64.b64encode(json.dumps({"emailAddress": account, "historyId": history_id}).encode())
    return {"message": {"data": data.decode(), "messageId": "pubsub-1"}, "subscription": "sub"}


async def _notify(adapter: GmailPushInbox, body: dict | bytes, token: str = "s3cret") -> httpx.Response:
    transport = httpx.ASGITransport(app=adapter.asgi_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        kwargs = {"content": body} if isinstance(body, bytes) else {"json": body}
        return await client.post(f"/gmail/push?token={token}", **kwargs)


def _history_calls(fake: FakeGmailService) -> int:
    return fake.calls.count("list_history")


# --------------------------------------------------------------------------- #
# Core flow
# --------------------------------------------------------------------------- #


async def test_first_drain_anchors_cursor_and_emits_nothing() -> None:
    fake = FakeGmailService()
    store = Store()
    adapter = _adapter(fake, FakeClock())

    assert await adapter.drain(store=store, session=None) == []

    state = store.read(store_keys.LAST_HISTORY.format(account=ACCOUNT))
    assert state["history_id"] == fake.get_history_id()


async def test_idle_makes_zero_gmail_calls() -> None:
    fake = FakeGmailService()
    store = Store()
    clock = FakeClock()
    adapter = _adapter(fake, clock)
    await adapter.drain(store=store, session=None)  # anchor + initial sync
    baseline = len(fake.calls)

    # No notification, clock barely moves: ten drains, zero API traffic.
    for _ in range(10):
        clock.advance(1)
        assert await adapter.drain(store=store, session=None) == []
    assert len(fake.calls) == baseline


async def test_notification_triggers_single_sync_and_emits_mail() -> None:
    fake = FakeGmailService()
    store = Store()
    adapter = _adapter(fake, FakeClock())
    await adapter.drain(store=store, session=None)
    before = _history_calls(fake)

    fake.add_message("m-new")
    resp = await _notify(adapter, _push_body())
    assert resp.status_code == 204

    msgs = await adapter.drain(store=store, session=None)
    assert [m.message_id for m in msgs] == ["m-new"]
    assert msgs[0].source == "gmail"
    assert _history_calls(fake) == before + 1


async def test_cursor_advances_only_after_event_recorded() -> None:
    """At-least-once: a crash between drain and record re-emits the mail;
    once the EVENT marker exists the cursor advances and re-emission stops."""
    fake = FakeGmailService()
    store = Store()
    clock = FakeClock()
    adapter = _adapter(fake, clock)
    await adapter.drain(store=store, session=None)

    fake.add_message("m1")
    await _notify(adapter, _push_body())
    first = await adapter.drain(store=store, session=None)
    assert [m.message_id for m in first] == ["m1"]

    # Simulate the crash window: no EVENT marker yet → re-emitted, cursor held.
    again = await adapter.drain(store=store, session=None)
    assert [m.message_id for m in again] == ["m1"]

    # PulseAgent records it → next drain settles the batch and goes quiet.
    store.write(store_keys.event_key("m1"), {"task_id": "t1"})
    assert await adapter.drain(store=store, session=None) == []
    state = store.read(store_keys.LAST_HISTORY.format(account=ACCOUNT))
    assert state["history_id"] == fake.get_history_id()
    assert not state.get("pending_history_id")


async def test_idle_resync_safety_net() -> None:
    fake = FakeGmailService()
    store = Store()
    clock = FakeClock()
    adapter = _adapter(fake, clock, idle_resync_seconds=900.0)
    await adapter.drain(store=store, session=None)
    before = _history_calls(fake)

    # Mail arrives but the push notification is lost. Before the idle window
    # elapses nothing happens; after it, the safety resync finds the mail.
    fake.add_message("m-lost-push")
    clock.advance(899)
    assert await adapter.drain(store=store, session=None) == []
    clock.advance(2)
    msgs = await adapter.drain(store=store, session=None)
    assert [m.message_id for m in msgs] == ["m-lost-push"]
    assert _history_calls(fake) == before + 1


async def test_expired_cursor_resyncs_with_warning(recwarn) -> None:
    fake = FakeGmailService()
    store = Store()
    adapter = _adapter(fake, FakeClock())
    await adapter.drain(store=store, session=None)

    fake.history_expired = True
    fake.add_message("m-in-gap")
    await _notify(adapter, _push_body())
    assert await adapter.drain(store=store, session=None) == []
    assert any("history cursor expired" in str(w.message) for w in recwarn.list)

    # Cursor was reset forward; once history works again, only post-reset
    # mail is event-driven (the gap is documented as unrecoverable).
    fake.history_expired = False
    fake.add_message("m-after-reset")
    await _notify(adapter, _push_body())
    msgs = await adapter.drain(store=store, session=None)
    assert [m.message_id for m in msgs] == ["m-after-reset"]


# --------------------------------------------------------------------------- #
# Endpoint security
# --------------------------------------------------------------------------- #


async def test_wrong_token_is_rejected_and_does_not_notify() -> None:
    fake = FakeGmailService()
    store = Store()
    adapter = _adapter(fake, FakeClock())
    await adapter.drain(store=store, session=None)
    before = _history_calls(fake)

    fake.add_message("m1")
    resp = await _notify(adapter, _push_body(), token="wrong")
    assert resp.status_code == 403
    assert await adapter.drain(store=store, session=None) == []
    assert _history_calls(fake) == before  # rejected push must not trigger a sync


async def test_malformed_body_is_acked_but_ignored() -> None:
    fake = FakeGmailService()
    store = Store()
    adapter = _adapter(fake, FakeClock())
    await adapter.drain(store=store, session=None)
    before = _history_calls(fake)

    resp = await _notify(adapter, b"not json at all")
    assert resp.status_code == 204  # ack — Pub/Sub must not redeliver poison forever
    assert await adapter.drain(store=store, session=None) == []
    assert _history_calls(fake) == before


async def test_notification_for_other_account_is_ignored() -> None:
    fake = FakeGmailService()
    store = Store()
    adapter = _adapter(fake, FakeClock())
    await adapter.drain(store=store, session=None)
    before = _history_calls(fake)

    resp = await _notify(adapter, _push_body(account="someone-else@example.com"))
    assert resp.status_code == 204
    assert await adapter.drain(store=store, session=None) == []
    assert _history_calls(fake) == before


# --------------------------------------------------------------------------- #
# Watch lifecycle
# --------------------------------------------------------------------------- #


async def test_watch_armed_once_and_renewed_near_expiry() -> None:
    fake = FakeGmailService()
    store = Store()
    clock = FakeClock()
    adapter = _adapter(fake, clock, topic_name="projects/p/topics/gmail")

    await adapter.drain(store=store, session=None)
    assert len(fake.watches) == 1
    assert fake.watches[0]["topic_name"] == "projects/p/topics/gmail"

    # Far-future expiration → no re-arm on subsequent drains.
    await adapter.drain(store=store, session=None)
    assert len(fake.watches) == 1

    # Within the renewal margin → re-armed.
    key = store_keys.LAST_HISTORY.format(account=ACCOUNT)
    state = store.read(key)
    state["watch_expiration_ms"] = int((clock.now.timestamp() + 60) * 1000)  # expires in 1 min
    store.write(key, state)
    await adapter.drain(store=store, session=None)
    assert len(fake.watches) == 2


# --------------------------------------------------------------------------- #
# Burst larger than one history batch (Codex P1: must not drop mail)
# --------------------------------------------------------------------------- #


async def test_burst_larger_than_batch_drains_fully_without_dropping() -> None:
    """120 messages arrive with a single push notification. The first drain
    is capped at the history batch size; the cursor must stop at the last
    returned message and the adapter must keep draining on later ticks until
    the backlog is empty — no idle-resync wait, no skipped mail."""
    fake = FakeGmailService()
    store = Store()
    adapter = _adapter(fake, FakeClock())
    await adapter.drain(store=store, session=None)

    for i in range(120):
        fake.add_message(f"b{i}")
    await _notify(adapter, _push_body())

    collected: list[str] = []
    for _ in range(5):  # a few ticks; PulseAgent records each batch between drains
        msgs = await adapter.drain(store=store, session=None)
        for m in msgs:
            collected.append(m.message_id)
            store.write(store_keys.event_key(m.message_id), {"task_id": m.message_id})
        if not msgs and len(collected) == 120:
            break

    assert collected == [f"b{i}" for i in range(120)]  # all of it, in order

    # Backlog settled: cursor is at "now" and the adapter goes quiet again.
    assert await adapter.drain(store=store, session=None) == []
    state = store.read(store_keys.LAST_HISTORY.format(account=ACCOUNT))
    assert state["history_id"] == fake.get_history_id()
    assert not state.get("pending_history_id")
