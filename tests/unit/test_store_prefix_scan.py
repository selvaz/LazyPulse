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


class _NoPrefixStore:
    """A duck-typed store whose ``items()`` predates the ``prefix=`` keyword.

    Stands in for an older lazybridge in the supported ``>=0.7.9`` range (or any
    duck-typed test store): ``items`` exists, so a naive ``hasattr`` guard would
    call ``items(prefix=...)`` and raise ``TypeError``. The scanner must catch
    that and degrade to the ``keys()`` walk instead of failing outright.
    """

    def __init__(self) -> None:
        self._d: dict[str, dict[str, object]] = {}

    def write(self, key: str, value: dict[str, object]) -> None:
        self._d[key] = value

    def read(self, key: str) -> dict[str, object] | None:
        return self._d.get(key)

    def keys(self) -> list[str]:
        return list(self._d.keys())

    def items(self) -> list[tuple[str, dict[str, object]]]:  # no prefix= kwarg
        return list(self._d.items())

    def delete(self, key: str) -> None:
        self._d.pop(key, None)

    def compare_and_swap(self, key: str, expected: object, new: dict[str, object]) -> bool:
        if self._d.get(key) == expected:
            self._d[key] = new
            return True
        return False


def test_iter_task_records_falls_back_when_items_lacks_prefix() -> None:
    """A store with items() but no prefix= must degrade to the keys() walk."""
    from datetime import timedelta

    from lazypulse import pending_tasks, purge_terminal_tasks
    from lazypulse.models import PulseRecord
    from lazypulse.tasks import _iter_task_records

    base = datetime(2026, 1, 1, tzinfo=UTC)
    waiting = PulseRecord(text="needs review", status="awaiting_review", created_at=base, run_at=base)
    done = PulseRecord(text="finished", status="completed", created_at=base, run_at=base, completed_at=base)
    store = _NoPrefixStore()
    store.write(f"pulse:task:{waiting.task_id}", waiting.model_dump(mode="json"))
    store.write(f"pulse:task:{done.task_id}", done.model_dump(mode="json"))
    store.write("pulse:event:e1", {"task_id": "x"})  # non-task noise

    records = _iter_task_records(store)  # type: ignore[arg-type]
    assert {k for k, _ in records} == {f"pulse:task:{waiting.task_id}", f"pulse:task:{done.task_id}"}

    # The public helpers ride the same fallback rather than raising TypeError.
    assert [r.task_id for r in pending_tasks(store)] == [waiting.task_id]  # type: ignore[arg-type]
    deleted = purge_terminal_tasks(store, older_than=timedelta(0))  # type: ignore[arg-type]
    assert deleted == 1  # the completed record is pruned via the fallback path


def test_scan_records_falls_back_when_items_lacks_prefix() -> None:
    """PulseAgent._scan_records delegates to the same hardened scanner."""
    store = _NoPrefixStore()
    store.write("pulse:task:t1", {"status": "scheduled"})
    store.write("other:noise", {"status": "scheduled"})
    agent = PulseAgent(
        name="noprefix",
        engine=MockEngine(["ok"]),
        store=store,  # type: ignore[arg-type]
        unsafe_allow_all=True,
    )
    records = agent._scan_records()
    assert [k for k, _ in records] == ["pulse:task:t1"]


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
