"""Human-in-the-loop review over Telegram.

When the policy parks a task as ``awaiting_review`` (a
``QUEUE_FOR_REVIEW`` / ``REQUIRE_OWNER_CONFIRMATION`` decision), someone has to
approve or reject it. :class:`TelegramReviewer` closes that loop **through the
same bot the owner already talks to**:

* :meth:`notify_pending` — pushes a message to the owner for every parked task
  it has not announced yet ("Approve? ``/approve <id>`` or ``/reject <id>``"),
  marking each so a per-tick call never re-sends the same request.
* :meth:`handle_command` — recognises the owner's ``/approve <id>`` /
  ``/reject <id> [reason]`` replies and applies them to the Store. Wire it as
  the ``PulseAgent(command_filter=...)`` hook so those replies are consumed as
  operator commands instead of being run as ordinary worker tasks.

Only the **owner** (a Telegram numeric user id) may approve — commands from
anyone else are ignored (left to flow through the normal policy, which will
reject them). Identity is Telegram's server-verified ``from.id``; there is no
header to forge, so the approval channel is as trustworthy as the bot itself.

Depends only on a duck-typed client exposing ``send_message(chat_id, text)``,
so it imports without the ``telegram`` extra and is testable with a fake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lazypulse import store_keys
from lazypulse.tasks import approve_task, pending_tasks, reject_task

if TYPE_CHECKING:
    from lazybridge import Store

    from lazypulse.models import InboundMessage


class TelegramReviewer:
    """Bridge the ``awaiting_review`` queue to a Telegram owner conversation.

    Parameters
    ----------
    client:
        A Telegram client with a synchronous ``send_message(chat_id, text)``.
    store:
        The same Store the :class:`~lazypulse.PulseAgent` writes task lifecycle
        into (approvals are Store compare-and-swaps against parked tasks).
    owner_id:
        The owner's Telegram numeric user id. Approval commands are accepted
        only from this id; notifications are pushed to it (in a private chat the
        chat id equals the user id). Pass ``owner_chat_id`` when the review
        conversation is not the owner's DM.
    """

    def __init__(
        self,
        client: object,
        store: Store,
        *,
        owner_id: int,
        owner_chat_id: int | None = None,
        bot_id: str = "telegram",
    ) -> None:
        self._client = client
        self._store = store
        self._owner_id = owner_id
        self._owner_chat_id = owner_chat_id if owner_chat_id is not None else owner_id
        self._bot_id = bot_id

    # ------------------------------------------------------------------ #
    # Notify — announce parked tasks to the owner
    # ------------------------------------------------------------------ #
    async def notify_pending(self) -> int:
        """Send an approval request for every parked task not yet announced.

        Returns the number of new notifications sent. Idempotent per task: a
        ``REVIEW_NOTIFIED`` marker is written after a successful send so calling
        this every tick does not re-announce the same task. Best-effort — a send
        failure simply leaves the task un-notified for the next call to retry.
        """
        sent = 0
        for task in pending_tasks(self._store):
            notified_key = store_keys.REVIEW_NOTIFIED.format(task_id=task.task_id)
            if self._store.read(notified_key) is not None:
                continue
            text = self._format_request(task.task_id, task.text)
            try:
                await _send(self._client, self._owner_chat_id, text)
            except Exception:
                continue  # leave un-notified; next call retries
            self._store.write(notified_key, {"notified": True})
            sent += 1
        return sent

    # ------------------------------------------------------------------ #
    # Handle — apply the owner's approve/reject replies
    # ------------------------------------------------------------------ #
    def handle_command(self, inbound: InboundMessage) -> bool:
        """Consume an owner ``/approve``/``/reject`` reply; return whether it was one.

        Wire as ``PulseAgent(command_filter=reviewer.handle_command)``. Returns
        ``True`` when the message was an owner review command (already applied to
        the Store) so the agent drops it from the task pipeline; ``False`` for
        anything else, which flows on to the normal policy. Non-owner senders can
        never approve: a review command from a non-owner returns ``False`` and is
        left for the policy to reject.
        """
        parsed = _parse_command(inbound.text)
        if parsed is None:
            return False
        # Authenticate: only the owner's server-verified id may approve.
        meta = inbound.metadata or {}
        if meta.get("user_id") != self._owner_id:
            return False
        verb, task_id, reason = parsed
        if verb == "approve":
            ok = approve_task(self._store, task_id)
            result = f"✅ Approved {task_id}" if ok else f"⚠️ {task_id} is not awaiting review"
        else:
            ok = reject_task(self._store, task_id, reason or "rejected by owner")
            result = f"🚫 Rejected {task_id}" if ok else f"⚠️ {task_id} is not awaiting review"
        # The task has left awaiting_review — drop the notified marker so a
        # future task reusing nothing lingers; harmless if already absent.
        self._store.delete(store_keys.REVIEW_NOTIFIED.format(task_id=task_id))
        self._ack(result)
        return True

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _format_request(self, task_id: str, text: str) -> str:
        preview = text if len(text) <= 500 else text[:497] + "…"
        return (
            "🔔 A task needs your approval:\n\n"
            f"{preview}\n\n"
            f"Reply  /approve {task_id}\n"
            f"or     /reject {task_id} [reason]"
        )

    def _ack(self, text: str) -> None:
        """Best-effort confirmation back to the owner. Runs on the tick thread
        (``handle_command`` is synchronous), so it is a single quick Bot API
        call wrapped to never propagate — the approval already landed in the
        Store regardless of whether the ack is delivered."""
        send = getattr(self._client, "send_message", None)
        if send is None:
            return
        try:
            send(chat_id=self._owner_chat_id, text=text)
        except Exception:
            pass


def _parse_command(text: str | None) -> tuple[str, str, str | None] | None:
    """Parse ``/approve <id>`` or ``/reject <id> [reason]``.

    Returns ``(verb, task_id, reason)`` or ``None`` if the text is not a review
    command. Tolerates a leading ``@botname`` suffix on the command word."""
    if not text:
        return None
    parts = text.strip().split(maxsplit=2)
    if not parts:
        return None
    word = parts[0].lower().lstrip("/")
    word = word.split("@", 1)[0]  # strip /approve@mybot → approve
    if word not in ("approve", "reject"):
        return None
    if len(parts) < 2 or not parts[1]:
        return None
    task_id = parts[1]
    reason = parts[2] if len(parts) > 2 else None
    return word, task_id, reason


async def _send(client: object, chat_id: int, text: str) -> None:
    """Send via the client's synchronous ``send_message``, offloaded to a thread
    so a slow Bot API round-trip never stalls the caller's event loop (matching
    the pattern in ``TelegramInbox``)."""
    import asyncio

    send = getattr(client, "send_message", None)
    if send is None:
        raise RuntimeError("client has no send_message(chat_id, text)")
    await asyncio.to_thread(send, chat_id=chat_id, text=text)
