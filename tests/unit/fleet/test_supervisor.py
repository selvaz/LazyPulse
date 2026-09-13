from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from lazybridge import Store

from lazypulse.fleet.events import read_process_events, write_planned_stop_marker
from lazypulse.fleet.supervisor import (
    FAST_FAILURE_SECONDS,
    MAX_BACKOFF_SECONDS,
    MIN_BACKOFF_SECONDS,
    _record_process_event,
    _terminate,
    is_stalled,
    main,
    next_backoff,
    reclaim_stalled_tasks,
)
from lazypulse.models import Identity, PolicyDecision, PulseRecord, TrustLevel
from lazypulse.store_keys import TASK
from lazypulse.tasks import get_task


def _write_running_task(store: Store, *, started_at: datetime) -> str:
    record = PulseRecord(
        text="do something",
        status="running",
        created_at=started_at,
        run_at=started_at,
        started_at=started_at,
        identity=Identity(trust=TrustLevel.SYSTEM),
        decision=PolicyDecision.ALLOW,
    )
    store.write(TASK.format(task_id=record.task_id), record.model_dump(mode="json"))
    return record.task_id


@pytest.mark.parametrize(
    ("current", "ran_for", "expected"),
    [
        (MIN_BACKOFF_SECONDS, 0.0, 10.0),
        (10.0, FAST_FAILURE_SECONDS - 0.01, 20.0),
        (MAX_BACKOFF_SECONDS, 0.0, MAX_BACKOFF_SECONDS),
        (80.0, FAST_FAILURE_SECONDS, MIN_BACKOFF_SECONDS),
        (80.0, FAST_FAILURE_SECONDS + 100, MIN_BACKOFF_SECONDS),
    ],
)
def test_next_backoff_fast_failure_doubles_and_slow_run_resets(current: float, ran_for: float, expected: float) -> None:
    assert next_backoff(current, ran_for) == expected


def test_is_stalled_without_approval_callback_reports_old_task() -> None:
    store = Store()
    _write_running_task(store, started_at=datetime.now(UTC) - timedelta(hours=1))

    assert is_stalled(store, stall_after_seconds=1800.0) is True


def test_is_stalled_uses_injected_cross_store_approval_check() -> None:
    """The supervised Store intentionally contains no approval information.

    Without the injected callback this exact scenario reports True/stalled,
    reproducing the old cross-store blindness that killed an agent while its
    escalation waited legitimately in a different shared Store.
    """
    supervised_store = Store()
    approval_store = Store()
    _write_running_task(supervised_store, started_at=datetime.now(UTC) - timedelta(hours=1))
    approval_store.write("approval:pending", {"status": "pending"})
    callback_calls = 0

    def has_pending_approval() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        raw = approval_store.read("approval:pending")
        return isinstance(raw, dict) and raw.get("status") == "pending"

    assert (
        is_stalled(
            supervised_store,
            stall_after_seconds=1800.0,
            has_pending_approval=has_pending_approval,
        )
        is False
    )
    assert callback_calls == 1


def test_is_stalled_ignores_callback_until_a_task_is_old_enough() -> None:
    store = Store()
    _write_running_task(store, started_at=datetime.now(UTC) - timedelta(seconds=5))

    def should_not_run() -> bool:
        pytest.fail("approval lookup is unnecessary without a stalled task")

    assert is_stalled(store, stall_after_seconds=1800.0, has_pending_approval=should_not_run) is False


def test_is_stalled_fails_open_when_approval_lookup_fails() -> None:
    store = Store()
    _write_running_task(store, started_at=datetime.now(UTC) - timedelta(hours=1))

    def broken() -> bool:
        raise RuntimeError("approval Store unavailable")

    assert is_stalled(store, stall_after_seconds=1800.0, has_pending_approval=broken) is False


def test_is_stalled_and_reclaim_scan_beyond_list_tasks_default_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``list_tasks`` defaults to its 100 NEWEST records -- a stall check
    must still see running tasks older than that default window, or a
    burst past 100 concurrently-running tasks would silently hide
    genuinely stalled ones from both is_stalled and reclaim_stalled_tasks.
    Found by Codex review before this ever shipped."""
    from lazypulse.fleet import supervisor

    captured_limits: list[int] = []
    real_list_tasks = supervisor.list_tasks

    def spy_list_tasks(store: Store, *, status: str | None = None, limit: int = 100):
        captured_limits.append(limit)
        return real_list_tasks(store, status=status, limit=limit)

    monkeypatch.setattr(supervisor, "list_tasks", spy_list_tasks)
    store = Store()

    supervisor.is_stalled(store, stall_after_seconds=1800.0)
    supervisor.reclaim_stalled_tasks(store, stall_after_seconds=1800.0)

    assert captured_limits == [supervisor._TASK_SCAN_LIMIT, supervisor._TASK_SCAN_LIMIT]
    assert all(limit > 100 for limit in captured_limits)


def test_reclaim_stalled_tasks_resets_old_running_task_only() -> None:
    store = Store()
    old_id = _write_running_task(store, started_at=datetime.now(UTC) - timedelta(hours=1))
    fresh_id = _write_running_task(store, started_at=datetime.now(UTC) - timedelta(seconds=5))

    assert reclaim_stalled_tasks(store, stall_after_seconds=1800.0) == [old_id]
    old = get_task(store, old_id)
    assert old is not None
    assert old.status == "scheduled"
    assert old.started_at is None
    assert old.restart_count == 1
    assert get_task(store, fresh_id).status == "running"


class _Proc:
    def __init__(self, *, times_out: bool = False) -> None:
        self.running = True
        self.times_out = times_out
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True
        if not self.times_out:
            self.running = False

    def wait(self, timeout: float | None = None) -> int:
        if self.running and self.times_out:
            self.times_out = False
            raise subprocess.TimeoutExpired("agent", timeout)
        self.running = False
        return 0

    def kill(self) -> None:
        self.killed = True
        self.running = False


def test_terminate_escalates_only_after_grace_timeout() -> None:
    graceful = _Proc()
    _terminate(graceful)  # type: ignore[arg-type]
    assert graceful.terminated is True and graceful.killed is False

    hanging = _Proc(times_out=True)
    _terminate(hanging)  # type: ignore[arg-type]
    assert hanging.terminated is True and hanging.killed is True


def test_record_process_event_writes_plain_and_suppressed_events() -> None:
    store = Store()
    _record_process_event(
        store,
        agent_name="plain",
        reason="unexpected_exit",
        exit_code=7,
        ran_for_seconds=2.0,
        restart_delay_seconds=10.0,
    )
    write_planned_stop_marker(store, "planned", "code reload")
    _record_process_event(
        store,
        agent_name="planned",
        reason="unexpected_exit",
        exit_code=1,
        ran_for_seconds=2.0,
        restart_delay_seconds=10.0,
    )

    by_name = {event["agent_name"]: event for event in read_process_events(store)}
    assert by_name["plain"]["delivered_at"] is None
    assert by_name["planned"]["delivery_kind"] == "suppressed"
    assert "code reload" in by_name["planned"]["suppressed"]


def test_cli_requires_agent_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["fleet-supervisor", "--workspace-root", ".", "--store-db", "agent.sqlite"],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


@pytest.mark.parametrize("flag", ["--stall-after-seconds", "--stall-check-seconds"])
@pytest.mark.parametrize("value", ["0", "-5"])
def test_cli_rejects_nonpositive_stall_intervals(monkeypatch: pytest.MonkeyPatch, flag: str, value: str) -> None:
    """A zero --stall-check-seconds drives a continuous CPU-busy polling
    loop; a nonpositive --stall-after-seconds classifies every running
    task as already stalled, killing a healthy child almost immediately.
    Found by Codex review before this ever shipped."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "fleet-supervisor",
            "--workspace-root",
            ".",
            "--store-db",
            "agent.sqlite",
            "--agent-script",
            "agent.py",
            flag,
            value,
        ],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_run_forever_retries_when_popen_itself_fails(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Popen failing to start the agent (not the child exiting) must not
    kill the SUPERVISOR itself -- that directly contradicts "deliberately
    never give up after N restarts". Found by Codex review before this
    ever shipped."""
    import argparse

    from lazypulse.fleet import supervisor

    sleep_calls: list[float] = []
    monkeypatch.setattr(supervisor.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    class _StopTest(Exception):
        pass

    popen_calls = {"count": 0}

    def fake_popen(cmd: list[str]) -> None:
        popen_calls["count"] += 1
        if popen_calls["count"] == 1:
            raise OSError("no such file or directory")
        raise _StopTest

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)

    args = argparse.Namespace(
        workspace_root=str(tmp_path),
        store_db=str(tmp_path / "agent.sqlite"),
        agent_script="agent.py",
        tick_cron="0 * * * *",
        tz="UTC",
        process_event_store_db=None,
        supervised_name=None,
    )

    with pytest.raises(_StopTest):
        supervisor.run_forever(args)

    assert popen_calls["count"] == 2
    assert sleep_calls == [pytest.approx(MIN_BACKOFF_SECONDS * 2)]
