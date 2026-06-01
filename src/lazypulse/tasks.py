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

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from lazypulse import store_keys
from lazypulse.models import PulseRecord

if TYPE_CHECKING:
    from lazybridge import Store

#: Statuses a task can no longer leave — safe to delete once old enough.
_TERMINAL_STATUSES = frozenset({"completed", "rejected", "failed"})


def _iter_task_records(store: Store) -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(key, raw)`` for every task record in the Store.

    Uses the indexed ``Store.items(prefix=)`` B-tree range scan when available
    (lazybridge >= 0.9.1), giving O(M) in the number of task keys rather than
    O(N) over the whole keyspace. Mirrors ``PulseAgent._scan_records`` so the
    review helpers and the tick loop share one scan strategy; falls back to a
    full ``keys()`` walk on older stores without ``items(prefix=)``.
    """
    if hasattr(store, "items"):
        return [(k, v) for k, v in store.items(prefix=store_keys.TASK_PREFIX) if isinstance(v, dict)]
    out: list[tuple[str, dict[str, Any]]] = []
    for key in list(store.keys()):
        if not key.startswith(store_keys.TASK_PREFIX):
            continue
        raw = store.read(key)
        if isinstance(raw, dict):
            out.append((key, raw))
    return out


def pending_tasks(store: Store) -> list[PulseRecord]:
    """Every task currently waiting for a human decision."""
    return [
        PulseRecord.model_validate(raw)
        for _key, raw in _iter_task_records(store)
        if raw.get("status") == "awaiting_review"
    ]


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


def purge_terminal_tasks(
    store: Store,
    *,
    older_than: timedelta,
    now: datetime | None = None,
) -> int:
    """Delete terminal task records older than ``older_than``; return the count.

    A task in a terminal status (``completed`` / ``rejected`` / ``failed``)
    never changes again, so once its ``completed_at`` is older than the
    retention window it is safe to drop. This keeps an always-on agent's Store
    from growing without bound — and keeps the per-tick record scan from
    getting slower over time.

    Idempotency markers (``pulse:event:*``) are **left in place**: they are tiny
    and deleting them would let an adapter re-ingest a long-finished message as
    a brand-new task. Run this from a cron, or let ``PulseAgent`` call it
    automatically by passing ``terminal_retention=``.
    """
    cutoff = (now or datetime.now(UTC)) - older_than
    deleted = 0
    for key, raw in _iter_task_records(store):
        if raw.get("status") not in _TERMINAL_STATUSES:
            continue
        completed_at = raw.get("completed_at")
        if not isinstance(completed_at, str):
            continue
        try:
            ts = datetime.fromisoformat(completed_at)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts <= cutoff:
            store.delete(key)
            deleted += 1
    return deleted
