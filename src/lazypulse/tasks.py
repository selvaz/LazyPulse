"""Operating on tasks parked for review.

When the policy returns ``QUEUE_FOR_REVIEW`` or ``REQUIRE_OWNER_CONFIRMATION``,
the task is written with status ``awaiting_review`` and the loop will not run
it. These helpers let a human (via a CLI, a web UI, a phone) close the loop:
list what's pending, then approve it (→ ``scheduled``, picked up on the next
tick) or reject it (→ ``rejected``).

All transitions are compare-and-swap, so they are safe when several reviewers
share one Store: a second approval of the same task simply returns ``False``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lazypulse import store_keys
from lazypulse.models import PulseRecord

if TYPE_CHECKING:
    from lazybridge import Store


def pending_tasks(store: Store) -> list[PulseRecord]:
    """Every task currently waiting for a human decision."""
    out: list[PulseRecord] = []
    for key in list(store.keys()):
        if not key.startswith(store_keys.TASK_PREFIX):
            continue
        raw = store.read(key)
        if isinstance(raw, dict) and raw.get("status") == "awaiting_review":
            out.append(PulseRecord.model_validate(raw))
    return out


def approve_task(store: Store, task_id: str, *, run_at: datetime | None = None) -> bool:
    """Approve a parked task: ``awaiting_review`` → ``scheduled``.

    The loop runs it on the next tick where ``run_at <= now`` (defaults to
    the task's original time, i.e. immediately). Returns ``False`` if the task
    is gone or no longer awaiting review (e.g. another reviewer acted first)."""
    key = store_keys.task_key(task_id)
    raw = store.read(key)
    if not isinstance(raw, dict) or raw.get("status") != "awaiting_review":
        return False
    rec = PulseRecord.model_validate(raw)
    updated = rec.model_copy(update={"status": "scheduled", "run_at": run_at or rec.run_at})
    return store.compare_and_swap(key, raw, updated.model_dump(mode="json"))


def reject_task(store: Store, task_id: str, reason: str) -> bool:
    """Reject a parked task: ``awaiting_review`` → ``rejected``.

    Returns ``False`` if the task is gone or no longer awaiting review."""
    key = store_keys.task_key(task_id)
    raw = store.read(key)
    if not isinstance(raw, dict) or raw.get("status") != "awaiting_review":
        return False
    rec = PulseRecord.model_validate(raw)
    updated = rec.model_copy(
        update={"status": "rejected", "completed_at": datetime.now(UTC), "error": reason}
    )
    return store.compare_and_swap(key, raw, updated.model_dump(mode="json"))
