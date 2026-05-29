"""Templated Store key conventions.

Every LazyPulse key lives under the ``pulse:`` namespace so a PulseAgent can
share a Store with arbitrary lazybridge state (``from_agent`` outputs, Plan
checkpoints, user data) without collisions.

Use ``.format(...)`` to fill the templated segment::

    store.read(TASK.format(task_id=record.task_id))
"""

from __future__ import annotations

#: One PulseRecord per task, keyed by ``task_id``.
TASK = "pulse:task:{task_id}"

#: Idempotency marker: maps an inbound ``message_id`` to the task it created.
EVENT = "pulse:event:{event_id}"

#: Human review request/response channel (see ``review.StoreReviewerUI``).
REVIEW_REQ = "pulse:review:req:{review_id}"
REVIEW_RESP = "pulse:review:resp:{review_id}"

#: Gmail history cursor — RESERVED for future history-based polling and not yet
#: read or written anywhere in the codebase. The current GmailInbox polls with a
#: query and relies on the central ``EVENT`` idempotency marker (not a
#: Gmail-specific key), so this template is intentionally inert for now.
LAST_HISTORY = "pulse:gmail:last_history_id:{account}"

#: Webhook replay protection.
WEBHOOK_NONCE = "pulse:webhook:nonce:{nonce}"

#: Telegram getUpdates offset watermark (per bot). The adapter advances this
#: only past updates the PulseAgent has already recorded (at-least-once).
TG_OFFSET = "pulse:telegram:offset:{bot}"

#: Telegram auto-reply throttle watermark (per bot + chat). Holds the
#: timestamp of the last auto-reply so the Responder path can rate-limit
#: replies into a single chat and break runaway reply loops.
TG_REPLY_THROTTLE = "pulse:telegram:reply_throttle:{bot}:{chat}"

#: Prefix used to enumerate every task record in the Store.
TASK_PREFIX = "pulse:task:"

#: Prefix used to enumerate pending review requests (derived from REVIEW_REQ).
REVIEW_REQ_PREFIX = "pulse:review:req:"

#: Recurring cron job records.
CRON = "pulse:cron:{cron_id}"
CRON_PREFIX = "pulse:cron:"

#: Per-sender rate-limit counter (window-bucketed).
RATE_KEY = "pulse:rate:{sender}:{window_bucket}"
RATE_PREFIX = "pulse:rate:"


def task_key(task_id: str) -> str:
    return TASK.format(task_id=task_id)


def event_key(event_id: str) -> str:
    return EVENT.format(event_id=event_id)


def tg_reply_throttle_key(bot: str, chat: str) -> str:
    return TG_REPLY_THROTTLE.format(bot=bot, chat=chat)
