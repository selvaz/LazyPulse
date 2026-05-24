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

#: Gmail idempotency + cursor.
GMAIL_PROCESSED = "pulse:gmail:processed:{message_id}"
LAST_HISTORY = "pulse:gmail:last_history_id:{account}"

#: Webhook replay protection.
WEBHOOK_NONCE = "pulse:webhook:nonce:{nonce}"

#: Prefix used to enumerate every task record in the Store.
TASK_PREFIX = "pulse:task:"


def task_key(task_id: str) -> str:
    return TASK.format(task_id=task_id)


def event_key(event_id: str) -> str:
    return EVENT.format(event_id=event_id)
