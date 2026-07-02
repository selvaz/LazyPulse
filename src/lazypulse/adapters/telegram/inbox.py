"""Telegram polling adapter.

Polls the Bot API for new updates via ``getUpdates`` and emits one
:class:`~lazypulse.models.InboundMessage` per text message. A media message
with a caption counts: the caption is the task text, so "analyse this" under
a photo is not silently dropped. Updates with neither (stickers, callbacks,
membership changes, …) are skipped and confirmed past.

**At-least-once.** Telegram discards updates once a higher ``offset`` confirms
them, so losing the offset-versus-recorded ordering would lose mail. The
adapter therefore advances the persisted offset watermark **only across a
contiguous prefix of updates the PulseAgent has already recorded** (the central
``store_keys.EVENT`` marker exists). An unrecorded message stops the watermark,
so a crash between drain and record-write simply re-fetches it next poll.
Central dedupe means a message still becomes at most one task.

Depends only on the duck-typed
:class:`~lazytools.connectors.telegram.client.TelegramService`, so it imports
without the ``telegram`` extra and is testable with a fake client.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lazytools.connectors.telegram.client import TelegramService, split_message

from lazypulse import store_keys
from lazypulse.models import ActionClass, InboundMessage

if TYPE_CHECKING:
    from collections.abc import Callable

    from lazybridge import Session, Store

    from lazypulse.models import PulseRecord


@dataclass
class TelegramInboxConfig:
    """Configuration for :class:`TelegramInbox`."""

    #: Stable, unique label for this bot used to key its offset watermark in
    #: the Store (e.g. the bot username). Two PulseAgents on one Store must use
    #: distinct ``bot_id``s if they poll different bots.
    bot_id: str
    #: Updates fetched per poll. The Bot API caps ``getUpdates`` at 100.
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

    def __post_init__(self) -> None:
        if not 1 <= self.max_results <= 100:
            raise ValueError(f"max_results must be in 1..100 (the Bot API getUpdates limit), got {self.max_results}")


class TelegramInbox:
    """An :class:`~lazypulse.Adapter` that polls a Telegram bot for updates."""

    def __init__(
        self,
        client: TelegramService,
        config: TelegramInboxConfig,
        *,
        name: str = "telegram",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = name
        self._client = client
        self._config = config
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    async def drain(self, *, store: Store, session: Session | None = None) -> list[InboundMessage]:
        offset_key = store_keys.TG_OFFSET.format(bot=self._config.bot_id)
        stored = store.read(offset_key)
        offset = 0
        if isinstance(stored, dict):
            try:
                offset = int(stored["offset"])
            except (KeyError, TypeError, ValueError):
                offset = 0  # corrupt watermark: refetch from scratch; central EVENT dedupe absorbs the replay

        # timeout=0 → short poll: return immediately with whatever is pending.
        # Long polling would block the tick loop; ``tick_seconds`` paces us.
        # The client is synchronous (httpx.Client) and the workers' async I/O
        # runs on this same event loop, so the network call is offloaded to a
        # thread — a slow Bot API round-trip must never stall in-flight tasks.
        updates = await asyncio.to_thread(
            self._client.get_updates, offset=offset, timeout=0, limit=self._config.max_results
        )

        # Updates without a usable update_id can be neither confirmed nor
        # deduped (they would all collide on one event id) — skip them.
        keyed: list[tuple[int, dict[str, Any]]] = []
        for upd in updates:
            try:
                keyed.append((int(upd["update_id"]), upd))
            except (KeyError, TypeError, ValueError):
                continue
        keyed.sort(key=lambda pair: pair[0])

        out: list[InboundMessage] = []
        confirmed = offset
        confirming = True  # advance the watermark only across a recorded/skippable prefix
        for update_id, upd in keyed:
            event_id = f"telegram:{self._config.bot_id}:{update_id}"
            msg = upd.get("message")
            if not isinstance(msg, dict):
                msg = {}
            text = _message_text(msg)
            if text is None or store.read(store_keys.event_key(event_id)) is not None:
                # Not a task (no text/caption) or already recorded in a prior
                # tick: confirm past it while the prefix is still intact.
                if confirming:
                    confirmed = update_id + 1
                continue
            # Unrecorded text message: emit it and stop advancing the watermark
            # so a crash before the PulseAgent records it re-fetches it.
            confirming = False
            out.append(self._to_inbound(event_id, msg, text))

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
        claimed, previous = self._claim_reply_window(chat_id, store)
        if not claimed:
            return  # within the per-chat throttle window — break the loop
        # One logical reply, one throttle claim — but chunked to the Bot API's
        # 4096-char sendMessage limit (a long worker answer would otherwise
        # fail outright and the user would hear nothing). Each send is
        # offloaded to a thread so it never stalls the tick loop (see the
        # matching note in ``drain``).
        sent_any = False
        try:
            for chunk in split_message(text):
                await asyncio.to_thread(self._client.send_message, chat_id=chat_id, text=chunk)
                sent_any = True
        except BaseException:
            # Nothing reached the chat → give the window back so the failed
            # attempt doesn't silently swallow the chat's next legitimate
            # reply. If at least one chunk landed, the claim stands: a reply
            # DID go into the chat, and re-opening the window would defeat
            # the anti-amplification purpose of the throttle.
            if not sent_any:
                self._restore_reply_window(chat_id, store, previous)
            raise

    def _claim_reply_window(self, chat_id: Any, store: Store) -> tuple[bool, Any]:
        """Claim the per-chat auto-reply throttle window.

        Returns ``(claimed, previous_value)``. ``False`` when a reply would
        fire within ``reply_min_interval_seconds`` of the previous one to this
        chat — or when a concurrent reply claimed the window first: the write
        is a compare-and-swap against the value we read, so two tasks
        completing at once for the same chat can never both pass."""
        interval = self._config.reply_min_interval_seconds
        if interval <= 0:
            return True, None
        key = store_keys.tg_reply_throttle_key(self._config.bot_id, str(chat_id))
        now = self._clock()
        last = store.read(key)
        if isinstance(last, dict) and isinstance(last.get("at"), str):
            try:
                last_at = datetime.fromisoformat(last["at"])
            except ValueError:
                last_at = None
            if last_at is not None and (now - last_at).total_seconds() < interval:
                return False, last
        if not store.compare_and_swap(key, last, {"at": now.isoformat()}):
            return False, last  # lost the race to a concurrent reply
        return True, last

    def _restore_reply_window(self, chat_id: Any, store: Store, previous: Any) -> None:
        """Best-effort rollback of a claimed throttle window after a send that
        delivered nothing. Between our claim and this rollback no other reply
        can claim (they see our fresh watermark), so a plain write is safe."""
        if self._config.reply_min_interval_seconds <= 0:
            return
        key = store_keys.tg_reply_throttle_key(self._config.bot_id, str(chat_id))
        if previous is None:
            store.delete(key)
        else:
            store.write(key, previous)

    def _to_inbound(self, event_id: str, msg: dict[str, Any], text: str) -> InboundMessage:
        frm = msg.get("from") or {}
        chat = msg.get("chat") or {}
        user_id = frm.get("id")
        date = msg.get("date")
        received = datetime.fromtimestamp(int(date), tz=UTC) if isinstance(date, int | float) else self._clock()
        return InboundMessage(
            source=self.name,
            message_id=event_id,
            received_at=received,
            sender_raw=str(user_id) if user_id is not None else None,
            text=text,
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


def _message_text(msg: dict[str, Any]) -> str | None:
    """The task text of an update's message: its ``text``, or the ``caption``
    of a media message — "analyse this" under a photo must not vanish
    silently. ``None`` for anything else (stickers, callbacks, membership
    changes, …), which the drain confirms past without emitting."""
    for source_field in ("text", "caption"):
        value = msg.get(source_field)
        if isinstance(value, str) and value:
            return value
    return None
