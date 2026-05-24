"""TelegramInbox polling + offset/idempotency, TelegramPolicy, TelegramTools."""

from __future__ import annotations

from typing import Any

import pytest
from lazybridge import Store

from lazypulse import store_keys
from lazypulse.adapters.telegram.inbox import TelegramInbox, TelegramInboxConfig
from lazypulse.adapters.telegram.policy import TelegramPolicy
from lazypulse.adapters.telegram.tools import TelegramSendBlocked, TelegramTools
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


# --- Tools ------------------------------------------------------------- #


def test_send_blocked_without_confirmation() -> None:
    svc = FakeService([])
    with pytest.raises(TelegramSendBlocked, match="no outstanding confirmation"):
        TelegramTools(svc)._send_message(chat_id=42, text="hi")
    assert svc.sent == []


def test_confirm_once_authorizes_exactly_one_send() -> None:
    svc = FakeService([])
    tools = TelegramTools(svc)
    tools.confirm_once()
    tools._send_message(chat_id=42, text="hi")
    assert len(svc.sent) == 1
    with pytest.raises(TelegramSendBlocked):
        tools._send_message(chat_id=42, text="again")


def test_confirm_send_bound_to_chat() -> None:
    svc = FakeService([])
    tools = TelegramTools(svc)
    tools.confirm_send(chat_id=42)
    with pytest.raises(TelegramSendBlocked):
        tools._send_message(chat_id=99, text="wrong chat")
    tools._send_message(chat_id=42, text="ok")
    assert len(svc.sent) == 1


def test_allow_list_enforced() -> None:
    svc = FakeService([])
    tools = TelegramTools(svc, allowed_chat_ids=[42])
    tools.confirm_once()
    with pytest.raises(TelegramSendBlocked, match="allow-list"):
        tools._send_message(chat_id=99, text="blocked")
    assert svc.sent == []


def test_require_confirmation_false_allows_reply() -> None:
    # The chat-bot setup: reply freely to an allow-listed chat.
    svc = FakeService([])
    tools = TelegramTools(svc, allowed_chat_ids=[42], require_confirmation=False)
    tools._send_message(chat_id=42, text="hi")
    assert len(svc.sent) == 1
