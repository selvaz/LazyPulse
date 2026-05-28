"""Test that _scan_records uses items(prefix=) and ignores non-task keys."""

from __future__ import annotations

from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import PulseAgent
from lazypulse.testing import FakeClock, MockEngine


def test_scan_records_ignores_non_task_keys() -> None:
    store = Store()
    clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    agent = PulseAgent(
        name="scan-test",
        engine=MockEngine(["ok"]),
        store=store,
        clock=clock,
        unsafe_allow_all=True,
    )
    agent.schedule("real task")
    # Write non-task noise that _scan_records must not return
    store.write("pulse:event:e1", {"task_id": "irrelevant"})
    store.write("pulse:rate:alice:0", {"count": 5})
    store.write("other:stuff", {"data": True})

    records = agent._scan_records()
    keys = [k for k, _ in records]
    assert all(k.startswith("pulse:task:") for k in keys), f"unexpected keys: {keys}"
    assert len(keys) == 1


def test_scan_records_uses_items_prefix() -> None:
    """Confirm items(prefix=) is used rather than full store.keys() scan."""
    store = Store()
    clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    agent = PulseAgent(
        name="scan-items-test",
        engine=MockEngine(["ok"]),
        store=store,
        clock=clock,
        unsafe_allow_all=True,
    )
    # Write 3 tasks + noise
    for i in range(3):
        agent.schedule(f"task {i}")
    store.write("pulse:cron:abc", {"expr": "* * * * *"})
    store.write("pulse:event:xyz", {"task_id": "t1"})

    records = agent._scan_records()
    assert len(records) == 3
    assert all(k.startswith("pulse:task:") for k, _ in records)
