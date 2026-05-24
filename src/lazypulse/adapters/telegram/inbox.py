"""Telegram polling adapter.

Polls the Bot API for new updates via ``getUpdates`` and emits one
:class:`~lazypulse.models.InboundMessage` per text message.

**At-least-once.** Telegram discards updates once a higher ``offset`` confirms
them, so losing the offset-versus-recorded ordering would lose mail. The
adapter therefore advances the persisted offset watermark **only across a
contiguous prefix of updates the PulseAgent has already recorded** (the central
``store_keys.EVENT`` marker exists). An unrecorded message stops the watermark,
so a crash between drain and record-write simply re-fetches it next poll.
Central dedupe means a message still becomes at most one task.

Depends only on the duck-typed
:class:`~lazypulse.adapters.telegram.client.TelegramService`, so it imports
without the ``telegram`` extra and is testable with a fake client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lazypulse import store_keys
from lazypulse.adapters.telegram.client import TelegramService
from lazypulse.models import ActionClass, InboundMessage

if TYPE_CHECKING:
    from lazybridge import Session, Store

    from lazypulse.models import PulseRecord


@dataclass
class TelegramInboxConfig:
    """Configuration for :class:`TelegramInbox`."""

    #: Stable, unique label for this bot used to key its offset watermark in
    #: the Store (e.g. the bot username). Two PulseAgents on one Store must use
    #: distinct ``bot_id``s if they poll different bots.
    bot_id: str
    max_results: int = 100
    default_action: ActionClass = ActionClass.READ_PUBLIC
    #: When True (default), a completed task's worker output is sent back to
    #: the originating chat automatically — making the bot conversational with
    #: no extra wiring. Set False for a fire-and-forget / triage bot that
    #: should not reply.
    reply_with_output: bool = True
    #: When False (default), the auto-reply path never sends into a chat whose
    #: originating message came from a bot account. ``TelegramPolicy`` already
    #: rejects bot senders at intake, so this is defence-in-depth that keeps
    #: the Responder path self-protecting regardless of the policy in use —
    #: the main guard against bot↔bot reply loops.
    reply_to_bots: bool = False
    #: Minimum seconds between consecutive auto-replies into the *same* chat.
    #: ``0.0`` (default) disables throttling. When set, a reply that would fire
    #: within the window of the previous one is dropped — a circuit breaker
    #: against runaway reply amplification. The watermark is kept in the Store
    #: (per bot + chat) so it holds across ticks, restarts, and processes.
    reply_min_interval_seconds: float = 0.0


class TelegramInbox:
    """An :class:`~lazypulse.Adapter` that polls a Telegram bot for updates."""

    def __init__(self, client: TelegramService, config: TelegramInboxConfig, *, name: str = "telegram") -> None:
        self.name = name
        self._client = client
        self._config = config

    async def drain(self, *, store: Store, session: Session | None = None) -> list[InboundMessage]:
        offset_key = store_keys.TG_OFFSET.format(bot=self._config.bot_id)
        stored = store.read(offset_key)
        offset = int(stored["offset"]) if isinstance(stored, dict) and "offset" in stored else 0

        # timeout=0 → short poll: return immediately with whatever is pending.
        # Long polling would block the tick loop; ``tick_seconds`` paces us.
        updates = self._client.get_updates(offset=offset, timeout=0, limit=self._config.max_results)

        out: list[InboundMessage] = []
        confirmed = offset
        confirming = True  # advance the watermark only across a recorded/skippable prefix
        for upd in sorted(updates, key=lambda u: int(u.get("update_id", 0))):
            update_id = int(upd.get("update_id", 0))
            event_id = f"telegram:{self._config.bot_id}:{update_id}"
            msg = upd.get("message")

            if not isinstance(msg, dict) or not isinstance(msg.get("text"), str):
                # Non-text update (media, callback, membership change, …): not a
                # task. Confirm past it only while the prefix is still intact.
                if confirming:
                    confirmed = update_id + 1
                continue

            if store.read(store_keys.event_key(event_id)) is not None:
                # Already recorded in a prior tick → safe to confirm past it.
                if confirming:
                    confirmed = update_id + 1
                continue

            # Unrecorded text message: emit it and stop advancing the watermark
            # so a crash before the PulseAgent records it re-fetches it.
            confirming = False
            out.append(self._to_inbound(event_id, msg))

        if confirmed != offset:
            store.write(offset_key, {"offset": confirmed})
        return out

    async def reply(
        self,
        record: PulseRecord,
        text: str,
        *,
        store: Store,
        session: Session | None = None,
    ) -> None:
        """Send the worker's output back to the chat the message came from.

        This implements the :class:`~lazypulse.adapters.base.Responder`
        protocol, so a ``PulseAgent`` calls it automatically when a Telegram-
        sourced task completes. Replying to the original (already-authorized)
        chat needs no confirmation — unlike :class:`TelegramTools`, which can
        target arbitrary chats and stays gated.

        Two circuit breakers guard against reply loops / amplification:
        ``reply_to_bots`` (skip replying into a bot conversation) and
        ``reply_min_interval_seconds`` (per-chat rate limit)."""
        meta = record.inbound_metadata or {}
        if not self._config.reply_with_output:
            return
        if not self._config.reply_to_bots and meta.get("is_bot"):
            return  # defence in depth: never auto-reply into a bot conversation
        chat_id = meta.get("chat_id")
        if chat_id is None or not text:
            return
        if not self._reply_allowed_now(chat_id, store):
            return  # within the per-chat throttle window — break the loop
        self._client.send_message(chat_id=chat_id, text=text)

    def _reply_allowed_now(self, chat_id: Any, store: Store) -> bool:
        """Enforce the per-chat auto-reply rate limit. Returns ``False`` (and
        records nothing) when a reply would fire within
        ``reply_min_interval_seconds`` of the previous one to this chat."""
        interval = self._config.reply_min_interval_seconds
        if interval <= 0:
            return True
        key = store_keys.tg_reply_throttle_key(self._config.bot_id, str(chat_id))
        now = datetime.now(UTC)
        last = store.read(key)
        if isinstance(last, dict) and isinstance(last.get("at"), str):
            try:
                last_at = datetime.fromisoformat(last["at"])
            except ValueError:
                last_at = None
            if last_at is not None and (now - last_at).total_seconds() < interval:
                return False
        store.write(key, {"at": now.isoformat()})
        return True

    def _to_inbound(self, event_id: str, msg: dict[str, Any]) -> InboundMessage:
        frm = msg.get("from") or {}
        chat = msg.get("chat") or {}
        user_id = frm.get("id")
        date = msg.get("date")
        received = datetime.fromtimestamp(int(date), tz=UTC) if isinstance(date, int | float) else datetime.now(UTC)
        return InboundMessage(
            source=self.name,
            message_id=event_id,
            received_at=received,
            sender_raw=str(user_id) if user_id is not None else None,
            text=msg.get("text", ""),
            requested_action=self._config.default_action,
            metadata={
                "user_id": user_id,
                "username": frm.get("username"),
                "is_bot": bool(frm.get("is_bot", False)),
                "chat_id": chat.get("id"),
                "chat_type": chat.get("type"),
                "telegram_message_id": msg.get("message_id"),
            },
        )
