"""TelegramInbox polling + offset/idempotency, TelegramPolicy, TelegramTools."""

from __future__ import annotations

from typing import Any

from lazybridge import Store

from lazypulse import store_keys
from lazypulse.adapters.telegram.inbox import TelegramInbox, TelegramInboxConfig
from lazypulse.adapters.telegram.policy import TelegramPolicy
from lazypulse.models import ActionClass, TrustLevel

BOT = "testbot"


def _update(
    update_id: int,
    *,
    text: str | None = "hello",
    user_id: int = 111,
    username: str = "user",
    is_bot: bool = False,
    chat_id: int = 111,
    chat_type: str = "private",
    date: int = 1700000000,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "message_id": update_id * 10,
        "date": date,
        "from": {"id": user_id, "username": username, "is_bot": is_bot},
        "chat": {"id": chat_id, "type": chat_type},
    }
    if text is not None:
        msg["text"] = text
    return {"update_id": update_id, "message": msg}


class FakeService:
    """Mimics getUpdates: returns updates with update_id >= offset."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self._updates = updates
        self.sent: list[dict[str, Any]] = []
        self.offsets: list[int] = []

    def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        return [u for u in self._updates if u["update_id"] >= offset][:limit]

    def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": 999}


def _inbox(svc: FakeService, **cfg: Any) -> TelegramInbox:
    return TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT, **cfg))


def _event_id(update_id: int) -> str:
    return f"telegram:{BOT}:{update_id}"


def _record(store: Store, update_id: int) -> None:
    store.write(store_keys.event_key(_event_id(update_id)), {"task_id": "t"})


# --- Inbox / drain ----------------------------------------------------- #


async def test_drain_emits_text_message() -> None:
    svc = FakeService([_update(5, text="ping", user_id=42, username="alice", chat_id=42)])
    msgs = await _inbox(svc).drain(store=Store(), session=None)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.source == "telegram"
    assert m.message_id == _event_id(5)
    assert m.text == "ping"
    assert m.sender_raw == "42"
    assert m.metadata["user_id"] == 42
    assert m.metadata["username"] == "alice"
    assert m.metadata["chat_id"] == 42
    assert m.metadata["chat_type"] == "private"


async def test_drain_skips_non_text_updates() -> None:
    # A photo (no text) is not turned into a task, but the offset advances past it.
    store = Store()
    svc = FakeService([{"update_id": 7, "message": {"date": 1, "from": {"id": 1}, "chat": {"id": 1}}}])
    assert await _inbox(svc).drain(store=store, session=None) == []
    assert store.read(store_keys.TG_OFFSET.format(bot=BOT)) == {"offset": 8}


async def test_at_least_once_offset_not_advanced_until_recorded() -> None:
    # An unrecorded message must NOT advance the offset, so a re-drain re-emits it.
    store = Store()
    svc = FakeService([_update(3)])
    first = await _inbox(svc).drain(store=store, session=None)
    assert len(first) == 1
    # Offset watermark unchanged (still 0) → nothing persisted.
    assert store.read(store_keys.TG_OFFSET.format(bot=BOT)) is None
    second = await _inbox(svc).drain(store=store, session=None)
    assert len(second) == 1  # re-emitted


async def test_drain_dedupes_and_advances_once_recorded() -> None:
    store = Store()
    svc = FakeService([_update(3)])
    await _inbox(svc).drain(store=store, session=None)
    _record(store, 3)  # PulseAgent records it
    third = await _inbox(svc).drain(store=store, session=None)
    assert third == []
    assert store.read(store_keys.TG_OFFSET.format(bot=BOT)) == {"offset": 4}


async def test_offset_advances_past_recorded_prefix_only() -> None:
    # [4 recorded, 5 unrecorded] → offset advances to 5, message 5 emitted.
    store = Store()
    _record(store, 4)
    svc = FakeService([_update(4), _update(5, text="new")])
    out = await _inbox(svc).drain(store=store, session=None)
    assert [m.message_id for m in out] == [_event_id(5)]
    assert store.read(store_keys.TG_OFFSET.format(bot=BOT)) == {"offset": 5}
    # The next poll asks Telegram for updates from 5 onward.
    assert svc.offsets[-1] == 0  # this drain used the stored (absent→0) offset


async def test_get_updates_uses_stored_offset() -> None:
    store = Store()
    store.write(store_keys.TG_OFFSET.format(bot=BOT), {"offset": 100})
    svc = FakeService([_update(100, text="x")])
    await _inbox(svc).drain(store=store, session=None)
    assert svc.offsets == [100]


async def test_drain_emits_media_caption_as_text() -> None:
    # A photo with a caption is a task ("analyse this" must not vanish).
    upd = _update(9, text=None)
    upd["message"]["caption"] = "analyse this"
    svc = FakeService([upd])
    msgs = await _inbox(svc).drain(store=Store(), session=None)
    assert len(msgs) == 1
    assert msgs[0].text == "analyse this"


async def test_drain_recovers_from_corrupt_offset() -> None:
    # A corrupt watermark must not wedge the adapter: refetch from 0 and let
    # the central EVENT dedupe absorb the replay.
    store = Store()
    store.write(store_keys.TG_OFFSET.format(bot=BOT), {"offset": "garbage"})
    svc = FakeService([_update(3)])
    msgs = await _inbox(svc).drain(store=store, session=None)
    assert len(msgs) == 1
    assert svc.offsets == [0]


async def test_drain_skips_updates_without_update_id() -> None:
    # No usable update_id → can be neither confirmed nor deduped; skipped
    # without crashing, and without colliding on a shared event id.
    class RawService(FakeService):
        def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 100) -> list[dict[str, Any]]:
            return [{"message": {"text": "orphan"}, "update_id": None}, {"message": {"text": "orphan2"}}]

    svc = RawService([])
    assert await _inbox(svc).drain(store=Store(), session=None) == []


def test_config_rejects_out_of_range_max_results() -> None:
    import pytest

    with pytest.raises(ValueError, match="max_results"):
        TelegramInboxConfig(bot_id=BOT, max_results=101)
    with pytest.raises(ValueError, match="max_results"):
        TelegramInboxConfig(bot_id=BOT, max_results=0)


async def test_default_action_propagates() -> None:
    svc = FakeService([_update(1)])
    msgs = await _inbox(svc, default_action=ActionClass.WRITE_LOCAL).drain(store=Store(), session=None)
    assert msgs[0].requested_action == ActionClass.WRITE_LOCAL


# --- Policy ------------------------------------------------------------ #


def _msg(svc_update: dict[str, Any]) -> Any:
    # Build an InboundMessage via the inbox path so metadata matches production.
    import asyncio

    svc = FakeService([svc_update])
    return asyncio.run(_inbox(svc).drain(store=Store(), session=None))[0]


def test_policy_owner_verified() -> None:
    msg = _msg(_update(1, user_id=42))
    assert TelegramPolicy(owner_ids=[42]).classify(msg).trust == TrustLevel.OWNER_VERIFIED_EMAIL


def test_policy_allowed_external() -> None:
    msg = _msg(_update(1, user_id=7))
    pol = TelegramPolicy(owner_ids=[42], allowed_user_ids=[7])
    assert pol.classify(msg).trust == TrustLevel.EXTERNAL_VERIFIED


def test_policy_unknown_sender_rejected() -> None:
    msg = _msg(_update(1, user_id=999))
    assert TelegramPolicy(owner_ids=[42]).classify(msg).trust == TrustLevel.UNKNOWN


def test_policy_bot_sender_never_trusted() -> None:
    # Even if a bot's id is in owner_ids, a bot sender is rejected.
    msg = _msg(_update(1, user_id=42, is_bot=True))
    assert TelegramPolicy(owner_ids=[42]).classify(msg).trust == TrustLevel.UNKNOWN


# NOTE: TelegramTools unit tests (allow-list + one-shot confirmation + scope
# binding) moved to lazytools/tests/test_telegram_tools.py with the tool itself
# (lazytoolkit, 0.8). The inbox/policy/auto-reply tests below stay here.


# --- Blocking client must not stall the event loop --------------------- #


async def test_drain_offloads_blocking_client() -> None:
    # The production client is synchronous httpx: a slow getUpdates must not
    # freeze the tick loop's other coroutines (in-flight workers, recovery).
    # A sentinel coroutine on the same loop must complete WHILE the blocking
    # drain is in flight — it can only do so if drain offloads to a thread.
    import asyncio
    import time

    class SlowService(FakeService):
        def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 100) -> list[dict[str, Any]]:
            time.sleep(0.2)  # blocking, like a slow network round-trip
            return super().get_updates(offset=offset, timeout=timeout, limit=limit)

    svc = SlowService([_update(1)])
    loop_alive = asyncio.Event()

    async def sentinel() -> None:
        await asyncio.sleep(0.05)  # fires mid-drain only if the loop is free
        loop_alive.set()

    drain = asyncio.ensure_future(_inbox(svc).drain(store=Store(), session=None))
    asyncio.ensure_future(sentinel())
    await asyncio.wait_for(loop_alive.wait(), timeout=0.15)
    assert len(await drain) == 1


async def test_reply_offloads_blocking_client() -> None:
    import asyncio
    import time

    class SlowService(FakeService):
        def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]:
            time.sleep(0.2)
            return super().send_message(chat_id=chat_id, text=text)

    svc = SlowService([])
    loop_alive = asyncio.Event()

    async def sentinel() -> None:
        await asyncio.sleep(0.05)
        loop_alive.set()

    reply = asyncio.ensure_future(_inbox(svc).reply(_reply_record(), "hi", store=Store(), session=None))
    asyncio.ensure_future(sentinel())
    await asyncio.wait_for(loop_alive.wait(), timeout=0.15)
    await reply
    assert len(svc.sent) == 1


# --- Conversational auto-reply (Responder) ----------------------------- #


async def test_completed_task_auto_replies_to_origin_chat() -> None:
    from lazypulse import PulseAgent
    from lazypulse.testing import FakeClock, MockEngine

    store = Store()
    svc = FakeService([_update(1, user_id=42, chat_id=42, text="ping")])
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT))
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["pong"]),
        store=store,
        clock=FakeClock(),
        policy=TelegramPolicy(owner_ids=[42]),
        adapters=[inbox],
    )
    # One beat: drain → record → run → complete → reply.
    await pulse.tick_once()
    assert svc.sent == [{"chat_id": 42, "text": "pong"}]


async def test_failing_reply_never_uncompletes_task_or_breaks_tick() -> None:
    # The reply is best-effort: a Responder whose send raises must leave the
    # task completed and the tick intact (pulse.reply_error is emitted).
    from lazypulse import PulseAgent
    from lazypulse.testing import FakeClock, MockEngine

    class BrokenSendService(FakeService):
        def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]:
            raise RuntimeError("Telegram API down")

    store = Store()
    svc = BrokenSendService([_update(1, user_id=42, chat_id=42, text="ping")])
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT))
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["pong"]),
        store=store,
        clock=FakeClock(),
        policy=TelegramPolicy(owner_ids=[42]),
        adapters=[inbox],
    )
    report = await pulse.tick_once()  # must not raise
    assert report.completed == 1
    from lazypulse.tasks import _iter_task_records

    statuses = [raw["status"] for _key, raw in _iter_task_records(store)]
    assert statuses == ["completed"]


async def test_auto_reply_can_be_disabled() -> None:
    from lazypulse import PulseAgent
    from lazypulse.testing import FakeClock, MockEngine

    store = Store()
    svc = FakeService([_update(1, user_id=42, chat_id=42, text="ping")])
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT, reply_with_output=False))
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["pong"]),
        store=store,
        clock=FakeClock(),
        policy=TelegramPolicy(owner_ids=[42]),
        adapters=[inbox],
    )
    await pulse.tick_once()
    assert svc.sent == []


async def test_rejected_message_never_replies() -> None:
    from lazypulse import PulseAgent
    from lazypulse.testing import FakeClock, MockEngine

    store = Store()
    # Stranger → rejected by policy → worker never runs → no reply.
    svc = FakeService([_update(1, user_id=999, chat_id=999, text="hi")])
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT))
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["should-not-send"]),
        store=store,
        clock=FakeClock(),
        policy=TelegramPolicy(owner_ids=[42]),
        adapters=[inbox],
    )
    await pulse.tick_once()
    assert svc.sent == []


def _reply_record(chat_id: int = 42, *, is_bot: bool = False) -> Any:
    from datetime import UTC, datetime

    from lazypulse.models import PulseRecord

    now = datetime.now(UTC)
    return PulseRecord(
        text="x",
        created_at=now,
        run_at=now,
        source="telegram",
        inbound_metadata={"chat_id": chat_id, "is_bot": is_bot},
    )


async def test_auto_reply_skips_bot_origin_by_default() -> None:
    # Defence in depth against bot↔bot loops: the Responder path itself
    # refuses to reply into a bot conversation, independent of the policy.
    svc = FakeService([])
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT))
    await inbox.reply(_reply_record(is_bot=True), "hi", store=Store(), session=None)
    assert svc.sent == []
    # Opt back in explicitly.
    inbox_optin = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT, reply_to_bots=True))
    await inbox_optin.reply(_reply_record(is_bot=True), "hi", store=Store(), session=None)
    assert svc.sent == [{"chat_id": 42, "text": "hi"}]


async def test_auto_reply_rate_limit_breaks_loop() -> None:
    from lazypulse.testing import FakeClock

    store = Store()
    svc = FakeService([])
    clock = FakeClock()
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT, reply_min_interval_seconds=60.0), clock=clock)
    rec = _reply_record(chat_id=42)
    await inbox.reply(rec, "first", store=store, session=None)
    # A second reply within the window is dropped — the circuit breaker.
    await inbox.reply(rec, "second", store=store, session=None)
    assert svc.sent == [{"chat_id": 42, "text": "first"}]
    # Once the window has elapsed the next reply is allowed again.
    clock.advance(61.0)
    await inbox.reply(rec, "third", store=store, session=None)
    assert svc.sent == [{"chat_id": 42, "text": "first"}, {"chat_id": 42, "text": "third"}]


async def test_throttle_claim_is_cas_guarded_against_concurrent_reply() -> None:
    # Two tasks for the same chat completing at once must not both pass the
    # throttle. Simulate the race deterministically: a competing reply lands
    # its watermark between our read and our write — the CAS must lose and
    # the reply must be dropped.
    from datetime import UTC, datetime

    class RacyStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.raced = False

        def compare_and_swap(self, key: str, expected: Any, new: Any) -> bool:
            if not self.raced and key.startswith("pulse:telegram:reply_throttle:"):
                self.raced = True
                super().write(key, {"at": datetime.now(UTC).isoformat()})
            return super().compare_and_swap(key, expected, new)

    store = RacyStore()
    svc = FakeService([])
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT, reply_min_interval_seconds=60.0))
    await inbox.reply(_reply_record(chat_id=42), "hi", store=store, session=None)
    assert svc.sent == []  # lost the claim race → no send


async def test_failed_send_does_not_burn_throttle_window() -> None:
    # A send that delivers nothing must give the window back: the next
    # legitimate reply within the interval still goes out.
    import pytest

    class FailOnceService(FakeService):
        def __init__(self) -> None:
            super().__init__([])
            self.fail_next = True

        def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("Telegram API down")
            return super().send_message(chat_id=chat_id, text=text)

    store = Store()
    svc = FailOnceService()
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT, reply_min_interval_seconds=60.0))
    rec = _reply_record(chat_id=42)
    with pytest.raises(RuntimeError):
        await inbox.reply(rec, "lost", store=store, session=None)
    assert svc.sent == []
    # The failed attempt did not consume the window.
    await inbox.reply(rec, "retry", store=store, session=None)
    assert svc.sent == [{"chat_id": 42, "text": "retry"}]


async def test_reply_chunks_long_output() -> None:
    # A worker answer over the Bot API's 4096-char limit is split into
    # multiple sends instead of failing outright.
    svc = FakeService([])
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT))
    await inbox.reply(_reply_record(chat_id=42), "a" * 5000, store=Store(), session=None)
    assert [len(s["text"]) for s in svc.sent] == [4096, 904]
    assert all(s["chat_id"] == 42 for s in svc.sent)


async def test_reply_chunks_count_as_one_throttled_reply() -> None:
    # The per-chat throttle gates the logical reply, not each chunk: a long
    # chunked answer goes out in full, and only the NEXT reply is throttled.
    store = Store()
    svc = FakeService([])
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT, reply_min_interval_seconds=60.0))
    await inbox.reply(_reply_record(chat_id=42), "a" * 5000, store=store, session=None)
    assert len(svc.sent) == 2  # both chunks sent
    await inbox.reply(_reply_record(chat_id=42), "next", store=store, session=None)
    assert len(svc.sent) == 2  # follow-up reply throttled


async def test_rate_limit_disabled_by_default() -> None:
    # With the default interval (0.0), consecutive replies are not throttled.
    store = Store()
    svc = FakeService([])
    inbox = TelegramInbox(svc, TelegramInboxConfig(bot_id=BOT))
    rec = _reply_record(chat_id=42)
    await inbox.reply(rec, "a", store=store, session=None)
    await inbox.reply(rec, "b", store=store, session=None)
    assert [s["text"] for s in svc.sent] == ["a", "b"]
