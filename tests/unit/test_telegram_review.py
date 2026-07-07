"""Human-in-the-loop review over Telegram (``TelegramReviewer``)."""

from __future__ import annotations

from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.adapters.telegram import TelegramReviewer
from lazypulse.models import Identity, PulseRecord, TrustLevel
from lazypulse.policy import PulsePolicy
from lazypulse.testing import FakeClock, MockAdapter, MockEngine

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OWNER = 42


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def _park(store: Store, text: str = "send the report", task_id: str | None = None) -> str:
    rec = PulseRecord(
        text=text,
        status="awaiting_review",
        created_at=NOW,
        run_at=NOW,
        **({"task_id": task_id} if task_id else {}),
    )
    store.write(store_keys.task_key(rec.task_id), rec.model_dump(mode="json"))
    return rec.task_id


def _cmd_msg(text: str, user_id: int, mid: str = "c1") -> InboundMessage:
    return InboundMessage(
        source="telegram",
        message_id=mid,
        received_at=NOW,
        sender_raw=str(user_id),
        text=text,
        metadata={"user_id": user_id},
    )


# --- notify_pending ---------------------------------------------------- #


async def test_notify_pending_messages_owner_once_per_task() -> None:
    store = Store()
    client = FakeClient()
    task_id = _park(store)
    reviewer = TelegramReviewer(client, store, owner_id=OWNER)

    assert await reviewer.notify_pending() == 1
    assert len(client.sent) == 1
    chat_id, text = client.sent[0]
    assert chat_id == OWNER
    assert task_id in text
    assert "/approve" in text and "/reject" in text
    # Idempotent: a second call re-sends nothing (marker set).
    assert await reviewer.notify_pending() == 0
    assert len(client.sent) == 1


async def test_notify_pending_retries_after_send_failure() -> None:
    class FlakyClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next = True

        def send_message(self, *, chat_id: int, text: str) -> None:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("boom")
            super().send_message(chat_id=chat_id, text=text)

    store = Store()
    client = FlakyClient()
    _park(store)
    reviewer = TelegramReviewer(client, store, owner_id=OWNER)

    assert await reviewer.notify_pending() == 0  # first send failed → not marked
    assert await reviewer.notify_pending() == 1  # retried and delivered


# --- handle_command ---------------------------------------------------- #


def test_handle_command_owner_approve() -> None:
    store = Store()
    client = FakeClient()
    task_id = _park(store)
    reviewer = TelegramReviewer(client, store, owner_id=OWNER)

    handled = reviewer.handle_command(_cmd_msg(f"/approve {task_id}", OWNER))

    assert handled is True
    assert PulseRecord.model_validate(store.read(store_keys.task_key(task_id))).status == "scheduled"
    assert any("Approved" in t for _, t in client.sent)  # ack sent to owner


def test_handle_command_owner_reject_with_reason() -> None:
    store = Store()
    task_id = _park(store)
    reviewer = TelegramReviewer(FakeClient(), store, owner_id=OWNER)

    handled = reviewer.handle_command(_cmd_msg(f"/reject {task_id} not now", OWNER))

    assert handled is True
    rec = PulseRecord.model_validate(store.read(store_keys.task_key(task_id)))
    assert rec.status == "rejected"
    assert rec.error == "not now"


def test_handle_command_from_non_owner_is_ignored() -> None:
    store = Store()
    task_id = _park(store)
    reviewer = TelegramReviewer(FakeClient(), store, owner_id=OWNER)

    handled = reviewer.handle_command(_cmd_msg(f"/approve {task_id}", 999))  # not the owner

    assert handled is False  # flows on to the policy, which rejects it
    assert PulseRecord.model_validate(store.read(store_keys.task_key(task_id))).status == "awaiting_review"


def test_handle_command_ignores_non_command() -> None:
    reviewer = TelegramReviewer(FakeClient(), Store(), owner_id=OWNER)
    assert reviewer.handle_command(_cmd_msg("what's the weather?", OWNER)) is False


def test_handle_command_ignores_bare_word_without_slash() -> None:
    # "approve the proposal" is an ordinary message, NOT a HITL command — it must
    # reach the worker, not be silently consumed. Only /approve · /reject count.
    store = Store()
    task_id = _park(store)
    reviewer = TelegramReviewer(FakeClient(), store, owner_id=OWNER)
    assert reviewer.handle_command(_cmd_msg("approve the proposal", OWNER)) is False
    assert reviewer.handle_command(_cmd_msg("reject this idea outright", OWNER)) is False
    # The parked task is untouched — the messages were not treated as commands.
    assert PulseRecord.model_validate(store.read(store_keys.task_key(task_id))).status == "awaiting_review"


def test_handle_command_strips_botname_suffix() -> None:
    store = Store()
    task_id = _park(store)
    reviewer = TelegramReviewer(FakeClient(), store, owner_id=OWNER)
    assert reviewer.handle_command(_cmd_msg(f"/approve@mybot {task_id}", OWNER)) is True


# --- end-to-end: reviewer wired as the agent's command_filter ---------- #


class _OwnerPolicy(PulsePolicy):
    def classify(self, inbound: InboundMessage) -> Identity:
        uid = (inbound.metadata or {}).get("user_id")
        trust = TrustLevel.OWNER_VERIFIED_EMAIL if uid == OWNER else TrustLevel.UNKNOWN
        return Identity(sender=inbound.sender_raw, trust=trust)


async def test_owner_approval_command_flows_through_agent() -> None:
    clock = FakeClock()
    store = Store()
    engine = MockEngine(["done"])
    client = FakeClient()
    task_id = _park(store)
    reviewer = TelegramReviewer(client, store, owner_id=OWNER)

    pulse = PulseAgent(
        name="p",
        engine=engine,
        store=store,
        clock=clock,
        policy=_OwnerPolicy(),
        command_filter=reviewer.handle_command,
        adapters=[MockAdapter([_cmd_msg(f"/approve {task_id}", OWNER)])],
    )
    report = await pulse.tick_once()

    # The /approve message was consumed as a command (not run as a task) and
    # re-scheduled the parked task, which then ran this same tick.
    assert PulseRecord.model_validate(store.read(store_keys.task_key(task_id))).status == "completed"
    assert engine.calls == ["send the report"]  # the parked task's text, not the command
    assert report.completed == 1
