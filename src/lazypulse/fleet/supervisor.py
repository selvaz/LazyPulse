"""External process-level supervision for always-on agents.

An in-process agent cannot reliably report its own liveness after its event
loop, SDK subprocess, or permission stream wedges.  This supervisor therefore
runs outside the agent: process exit is one liveness signal, and durable Pulse
task progress is the other.  An always-on agent should never exit by itself,
so *every* exit -- including code 0 -- triggers a restart.

Exit-only supervision was found live to be insufficient: a dead-but-not-
exited provider subprocess can leave its Python parent alive while producing
nothing indefinitely.  :func:`is_stalled` closes that gap by detecting a
Pulse task stuck in ``running`` past a threshold.  A caller may inject the
pending-approval check because approvals often live in a different shared
Store; hard-coding the supervised process's own Store made legitimate
cross-store human waits invisible.

Backoff grows across repeated fast failures and resets after a sufficiently
long run, bounding crash-loop noise without persistent failure bookkeeping.

Usage::

    python -m lazypulse.fleet.supervisor --workspace-root <path> \
        --store-db <path> --agent-script <path> \
        --process-event-store-db <path> --supervised-name research
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lazybridge import Store

from lazypulse import store_keys
from lazypulse.fleet import events
from lazypulse.tasks import list_tasks

MIN_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0
#: An exit sooner than this counts as a fast failure.  This is long enough
#: that routine dependency imports and engine construction do not trip it.
FAST_FAILURE_SECONDS = 30.0

#: Long enough for real multi-file/provider work, but short enough that a
#: human need not discover and kill a wedged process manually.
DEFAULT_STALL_AFTER_SECONDS = 1800.0
#: How often to check durable liveness while the child process remains alive.
DEFAULT_STALL_CHECK_SECONDS = 60.0

#: ``list_tasks`` defaults to its 100 NEWEST records. A stall check must
#: see every running task, including ones older than that default window
#: -- the oldest are exactly the ones most likely to actually be stalled
#: -- or a burst past 100 concurrently-running tasks would silently hide
#: genuinely stuck ones from both `is_stalled` and `reclaim_stalled_tasks`.
#: Found by Codex review before this ever shipped.
_TASK_SCAN_LIMIT = 100_000


def is_stalled(
    store: Store,
    *,
    stall_after_seconds: float,
    has_pending_approval: Callable[[], bool] | None = None,
) -> bool:
    """Return whether an old running task has no legitimate wait explaining it.

    ``has_pending_approval`` is injected so it can inspect whichever Store or
    stores actually receive escalation tickets.  Omitting it means "no known
    pending approval"; this function never silently consults the wrong Store.

    Read failures fail open (``False``): a transient telemetry problem must
    not itself trigger a kill, and the next periodic check gets another try.
    """
    try:
        now = datetime.now(UTC)
        stalled = [
            task
            for task in list_tasks(store, status="running", limit=_TASK_SCAN_LIMIT)
            if (started_at := task.started_at) is not None and (now - started_at).total_seconds() >= stall_after_seconds
        ]
        if not stalled:
            return False
        return not has_pending_approval() if has_pending_approval is not None else True
    except Exception as exc:
        print(f"[fleet-supervisor] stall check failed (treating as not stalled): {exc}", flush=True)
        return False


def reclaim_stalled_tasks(store: Store, *, stall_after_seconds: float) -> list[str]:
    """Reset tasks running past the threshold back to ``scheduled``.

    This is the SAME reset ``PulseAgent._recover_stale`` eventually performs,
    done immediately after the supervisor has killed a process for precisely
    this reason instead of waiting for the agent's own threshold.

    Why this exists: ``_recover_stale``'s threshold is
    ``max(tick_seconds * 60, 3600.0)`` -- at least an HOUR -- while this
    supervisor normally kills a stalled task after 30 minutes. Found live
    2026-09-11: a task hard-killed mid-run left a ``running`` record behind;
    every subsequent restart was stall-killed within about 60 seconds because
    the record's age only grows and the process never survived long enough for
    PulseAgent's own threshold to fire. Resetting immediately closes that
    infinite-kill-loop gap.

    Best-effort: a Store failure here cannot crash the supervisor after the
    child has already been killed.
    """
    reclaimed: list[str] = []
    try:
        now = datetime.now(UTC)
        for task in list_tasks(store, status="running", limit=_TASK_SCAN_LIMIT):
            if task.started_at is None or (now - task.started_at).total_seconds() < stall_after_seconds:
                continue
            key = store_keys.TASK.format(task_id=task.task_id)
            raw = store.read(key)
            recovered = task.model_copy(
                update={"status": "scheduled", "started_at": None, "restart_count": task.restart_count + 1}
            )
            if isinstance(raw, dict) and store.compare_and_swap(key, raw, recovered.model_dump(mode="json")):
                reclaimed.append(task.task_id)
    except Exception as exc:
        print(f"[fleet-supervisor] reclaiming stalled tasks failed (leaving them as-is): {exc}", flush=True)
    return reclaimed


def next_backoff(current: float, ran_for: float) -> float:
    """Return the restart delay after one supervised run."""
    if ran_for < FAST_FAILURE_SECONDS:
        return min(current * 2, MAX_BACKOFF_SECONDS)
    return MIN_BACKOFF_SECONDS


def _agent_command(args: argparse.Namespace) -> list[str]:
    """Build the command for an agent script exposing the shared CLI shape."""
    return [
        sys.executable,
        str(args.agent_script),
        "--workspace-root",
        str(args.workspace_root),
        "--store-db",
        args.store_db,
        "--tick-cron",
        args.tick_cron,
        "--tz",
        args.tz,
    ]


def _terminate(proc: subprocess.Popen[Any]) -> None:
    """Best-effort graceful termination, escalating to kill after 10 seconds."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print("[fleet-supervisor] agent did not exit within 10s of terminate() -- killing it", flush=True)
        proc.kill()
        proc.wait(timeout=10)


def _record_process_event(
    store: Store,
    *,
    agent_name: str,
    reason: str,
    exit_code: int | None,
    ran_for_seconds: float,
    restart_delay_seconds: float,
    prefix: str = events.DEFAULT_PROCESS_EVENT_PREFIX,
) -> None:
    """Best-effort append of one supervised-process exit event.

    Planned exits remain available for audit but are born delivered and
    suppressed so they never enter an alerting outbox.
    """
    try:
        event_id = uuid.uuid4().hex
        created_at = datetime.now(UTC)
        marker = events.planned_stop_covering(store, agent_name, created_at, now=created_at)
        event: dict[str, Any] = {
            "event_id": event_id,
            "agent_name": agent_name,
            "reason": reason,
            "exit_code": exit_code,
            "ran_for_seconds": ran_for_seconds,
            "restart_delay_seconds": restart_delay_seconds,
            "created_at": created_at.isoformat(),
            "delivered_at": None,
            "delivery_attempts": 0,
        }
        if marker is not None:
            event.update(
                delivered_at=created_at.isoformat(),
                delivery_kind="suppressed",
                suppressed=f"planned stop: {marker.get('reason', 'unspecified')}",
            )
        store.write(f"{prefix}{event_id}", event)
    except Exception as exc:
        print(f"[fleet-supervisor] process-event write failed (continuing restart loop): {exc}", flush=True)


def run_forever(
    args: argparse.Namespace,
    *,
    has_pending_approval: Callable[[], bool] | None = None,
) -> None:
    """Supervise until Ctrl+C; deliberately never give up after N restarts."""
    backoff = MIN_BACKOFF_SECONDS
    cmd = _agent_command(args)
    task_store = Store(db=args.store_db)
    event_store = Store(db=args.process_event_store_db) if args.process_event_store_db is not None else None
    while True:
        print(f"[fleet-supervisor] starting: {' '.join(cmd)}", flush=True)
        started_at = time.monotonic()
        try:
            proc = subprocess.Popen(cmd)
        except OSError as exc:
            # Popen itself can fail (missing interpreter, resource
            # exhaustion, ...) -- if that propagates uncaught, it kills
            # the SUPERVISOR, not just the supervised agent, directly
            # contradicting "deliberately never give up after N
            # restarts". Treated as an instant failed run instead, going
            # through the same backoff/retry/event-recording path as a
            # real child exit. Found by Codex review before this ever
            # shipped.
            ran_for = time.monotonic() - started_at
            backoff = next_backoff(backoff, ran_for)
            print(f"[fleet-supervisor] failed to start the agent process: {exc}", flush=True)
            if event_store is not None:
                _record_process_event(
                    event_store,
                    agent_name=args.supervised_name,
                    reason="launch_failed",
                    exit_code=None,
                    ran_for_seconds=ran_for,
                    restart_delay_seconds=backoff,
                )
            print(f"[fleet-supervisor] retrying in {backoff:.0f}s", flush=True)
            try:
                time.sleep(backoff)
            except KeyboardInterrupt:
                print("[fleet-supervisor] interrupted during backoff -- stopping", flush=True)
                raise
            continue
        exit_code: int | None = None
        exit_reason = "unexpected_exit"
        try:
            while exit_code is None:
                try:
                    exit_code = proc.wait(timeout=args.stall_check_seconds)
                except subprocess.TimeoutExpired:
                    if is_stalled(
                        task_store,
                        stall_after_seconds=args.stall_after_seconds,
                        has_pending_approval=has_pending_approval,
                    ):
                        print(
                            f"[fleet-supervisor] a task has been running past "
                            f"{args.stall_after_seconds:.0f}s with no pending approval to explain it -- "
                            "treating as stalled, killing",
                            flush=True,
                        )
                        _terminate(proc)
                        exit_code = -1
                        exit_reason = "stall_kill"
                        reclaimed = reclaim_stalled_tasks(task_store, stall_after_seconds=args.stall_after_seconds)
                        if reclaimed:
                            print(
                                f"[fleet-supervisor] reclaimed {len(reclaimed)} stalled task(s): {reclaimed}",
                                flush=True,
                            )
        except KeyboardInterrupt:
            print("[fleet-supervisor] interrupted -- stopping the agent and exiting (not restarting)", flush=True)
            _terminate(proc)
            raise
        ran_for = time.monotonic() - started_at
        backoff = next_backoff(backoff, ran_for)
        print(f"[fleet-supervisor] agent exited (code={exit_code}) after {ran_for:.1f}s", flush=True)
        if event_store is not None:
            _record_process_event(
                event_store,
                agent_name=args.supervised_name,
                reason=exit_reason,
                exit_code=exit_code,
                ran_for_seconds=ran_for,
                restart_delay_seconds=backoff,
            )
        print(f"[fleet-supervisor] restarting in {backoff:.0f}s", flush=True)
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            print("[fleet-supervisor] interrupted during backoff -- stopping", flush=True)
            raise


def main() -> None:
    """Run the generic supervisor CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--store-db", required=True)
    parser.add_argument(
        "--agent-script",
        required=True,
        type=Path,
        help="Agent script exposing --workspace-root/--store-db/--tick-cron/--tz.",
    )
    parser.add_argument("--tick-cron", default="0 * * * *")
    parser.add_argument("--tz", default="UTC")
    parser.add_argument("--stall-after-seconds", type=float, default=DEFAULT_STALL_AFTER_SECONDS)
    parser.add_argument("--stall-check-seconds", type=float, default=DEFAULT_STALL_CHECK_SECONDS)
    parser.add_argument(
        "--process-event-store-db",
        default=None,
        help="Write durable child-exit events to this Store; omitted disables recording.",
    )
    parser.add_argument(
        "--supervised-name",
        default=None,
        help="Stable agent name for process events (required with --process-event-store-db).",
    )
    args = parser.parse_args()
    if args.process_event_store_db is not None and not args.supervised_name:
        parser.error("--supervised-name is required when --process-event-store-db is given")
    # A zero --stall-check-seconds drives a continuous CPU-busy polling
    # loop (proc.wait(timeout=0) never blocks); a nonpositive
    # --stall-after-seconds classifies every running task as already
    # stalled, killing a healthy child almost immediately after launch.
    # Found by Codex review before this ever shipped.
    if args.stall_after_seconds <= 0:
        parser.error("--stall-after-seconds must be positive")
    if args.stall_check_seconds <= 0:
        parser.error("--stall-check-seconds must be positive")
    with contextlib.suppress(KeyboardInterrupt):
        run_forever(args)


__all__ = [
    "DEFAULT_STALL_AFTER_SECONDS",
    "DEFAULT_STALL_CHECK_SECONDS",
    "FAST_FAILURE_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "MIN_BACKOFF_SECONDS",
    "is_stalled",
    "main",
    "next_backoff",
    "reclaim_stalled_tasks",
    "run_forever",
]


if __name__ == "__main__":
    main()
