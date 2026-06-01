"""Terminal-record retention: keep an always-on agent's Store bounded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lazybridge import Store

from lazypulse import PulseAgent, purge_terminal_tasks, store_keys
from lazypulse.models import PulseRecord
from lazypulse.testing import FakeClock, MockEngine


def _put(store: Store, *, status: str, completed_at: datetime | None, text: str = "t") -> str:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rec = PulseRecord(
        text=text,
        status=status,  # type: ignore[arg-type]
        created_at=base,
        run_at=base,
        completed_at=completed_at,
    )
    store.write(store_keys.task_key(rec.task_id), rec.model_dump(mode="json"))
    return rec.task_id


def test_purge_deletes_only_old_terminal_records() -> None:
    store = Store()
    now = datetime(2026, 1, 2, tzinfo=UTC)
    old_done = _put(store, status="completed", completed_at=now - timedelta(hours=2))
    old_failed = _put(store, status="failed", completed_at=now - timedelta(hours=3))
    recent_done = _put(store, status="completed", completed_at=now - timedelta(minutes=1))
    scheduled = _put(store, status="scheduled", completed_at=None)

    deleted = purge_terminal_tasks(store, older_than=timedelta(hours=1), now=now)

    assert deleted == 2
    keys = list(store.keys())
    assert store_keys.task_key(old_done) not in keys
    assert store_keys.task_key(old_failed) not in keys
    assert store_keys.task_key(recent_done) in keys  # within the window — kept
    assert store_keys.task_key(scheduled) in keys  # non-terminal — never pruned


def test_purge_leaves_event_markers_intact() -> None:
    # Dedupe markers must survive pruning, or a long-finished message could be
    # re-ingested as a brand-new task.
    store = Store()
    now = datetime(2026, 1, 2, tzinfo=UTC)
    base = now - timedelta(hours=2)
    rec = PulseRecord(
        text="t",
        status="completed",
        created_at=base,
        run_at=base,
        completed_at=base,
        source_event_id="gmail:abc",
    )
    store.write(store_keys.task_key(rec.task_id), rec.model_dump(mode="json"))
    store.write(store_keys.event_key("gmail:abc"), {"task_id": rec.task_id})

    purge_terminal_tasks(store, older_than=timedelta(hours=1), now=now)

    assert store.read(store_keys.task_key(rec.task_id)) is None
    assert store.read(store_keys.event_key("gmail:abc")) is not None


def test_purge_ignores_non_task_keys() -> None:
    # The prune path now scans via Store.items(prefix="pulse:task:"); confirm it
    # only ever touches task records and leaves unrelated namespaces alone, even
    # when they would match the terminal/old-enough conditions on shape.
    store = Store()
    now = datetime(2026, 1, 2, tzinfo=UTC)
    old_done = _put(store, status="completed", completed_at=now - timedelta(hours=2))
    store.write(store_keys.event_key("m1"), {"task_id": "irrelevant"})
    store.write("pulse:rate:alice:0", {"count": 5})
    # A non-task key that happens to look like a terminal record must be ignored.
    store.write("other:thing", {"status": "completed", "completed_at": (now - timedelta(hours=5)).isoformat()})

    deleted = purge_terminal_tasks(store, older_than=timedelta(hours=1), now=now)

    assert deleted == 1
    keys = list(store.keys())
    assert store_keys.task_key(old_done) not in keys
    assert store_keys.event_key("m1") in keys
    assert "pulse:rate:alice:0" in keys
    assert "other:thing" in keys


def test_pending_tasks_scans_only_task_records() -> None:
    # pending_tasks shares the indexed scan; non-task noise must never surface
    # as a (mis-validated) pending record.
    from lazypulse import pending_tasks

    store = Store()
    waiting = _put(store, status="awaiting_review", completed_at=None)
    _put(store, status="completed", completed_at=datetime(2026, 1, 1, tzinfo=UTC))
    store.write(store_keys.event_key("m1"), {"task_id": "x"})
    store.write("other:noise", {"status": "awaiting_review"})

    pending = pending_tasks(store)

    assert [r.task_id for r in pending] == [waiting]


async def test_pulse_agent_prunes_terminal_records_when_retention_set() -> None:
    clock = FakeClock()
    store = Store()
    old = PulseRecord(
        text="old",
        status="completed",
        created_at=clock.now - timedelta(hours=2),
        run_at=clock.now - timedelta(hours=2),
        completed_at=clock.now - timedelta(hours=2),
    )
    store.write(store_keys.task_key(old.task_id), old.model_dump(mode="json"))
    pulse = PulseAgent(name="p", engine=MockEngine(["x"]), store=store, clock=clock, terminal_retention=3600)

    report = await pulse.tick_once()

    assert report.pruned == 1
    assert store.read(store_keys.task_key(old.task_id)) is None


async def test_policy_rejected_intake_records_get_purged() -> None:
    # A policy REJECT lands as terminal on first write — the record must carry
    # completed_at so terminal_retention can age it out. Regression: previously
    # _intake left completed_at=None on the reject path, so spammy adapters
    # grew the ledger unbounded despite retention being enabled.
    from lazypulse.models import InboundMessage, PolicyDecision
    from lazypulse.policy import PulsePolicy
    from lazypulse.testing import FakeClock

    class _RejectAll(PulsePolicy):
        def classify(self, msg):  # type: ignore[override]
            from lazypulse.models import Identity, TrustLevel
            return Identity(sender=msg.sender_raw, trust=TrustLevel.UNKNOWN)

        def authorize(self, identity, action):  # type: ignore[override]
            return PolicyDecision.REJECT

    clock = FakeClock()
    store = Store()
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["x"]),
        store=store,
        clock=clock,
        policy=_RejectAll(),
        terminal_retention=3600,
    )
    from lazypulse.models import TickReport
    pulse._intake(
        InboundMessage(
            source="test",
            message_id="m1",
            received_at=clock.now,
            text="spam",
            sender_raw="x",
        ),
        clock.now,
        report=TickReport(at=clock.now),
    )
    clock.advance(7200)
    report = await pulse.tick_once()

    assert report.pruned == 1
    assert list(store.keys()) == [
        # only the EVENT dedupe marker survives (intentional: prevents replay)
        store_keys.event_key("m1"),
    ]


async def test_pulse_agent_keeps_records_without_retention() -> None:
    clock = FakeClock()
    store = Store()
    old = PulseRecord(
        text="old",
        status="completed",
        created_at=clock.now - timedelta(hours=2),
        run_at=clock.now - timedelta(hours=2),
        completed_at=clock.now - timedelta(hours=2),
    )
    store.write(store_keys.task_key(old.task_id), old.model_dump(mode="json"))
    pulse = PulseAgent(name="p", engine=MockEngine(["x"]), store=store, clock=clock)  # retention off (default)

    report = await pulse.tick_once()

    assert report.pruned == 0
    assert store.read(store_keys.task_key(old.task_id)) is not None
