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

#: Gmail history cursor (reserved for future history-based polling). Message
#: idempotency uses the central ``EVENT`` marker, not a Gmail-specific key.
LAST_HISTORY = "pulse:gmail:last_history_id:{account}"

#: Webhook replay protection.
WEBHOOK_NONCE = "pulse:webhook:nonce:{nonce}"

#: Telegram getUpdates offset watermark (per bot). The adapter advances this
#: only past updates the PulseAgent has already recorded (at-least-once).
TG_OFFSET = "pulse:telegram:offset:{bot}"

#: Prefix used to enumerate every task record in the Store.
TASK_PREFIX = "pulse:task:"

#: Prefix used to enumerate pending review requests (derived from REVIEW_REQ).
REVIEW_REQ_PREFIX = "pulse:review:req:"


def task_key(task_id: str) -> str:
    return TASK.format(task_id=task_id)


def event_key(event_id: str) -> str:
    return EVENT.format(event_id=event_id)
