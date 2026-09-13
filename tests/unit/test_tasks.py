"""Read-only task lookup: ``get_task`` and ``list_tasks``."""

from __future__ import annotations

import pytest
from lazybridge import Store

from lazypulse import PulseAgent, get_task, list_tasks
from lazypulse.testing import FakeClock, MockEngine


def test_get_task_returns_none_for_unknown_id() -> None:
    store = Store()
    assert get_task(store, "does-not-exist") is None


async def test_get_task_returns_the_record() -> None:
    store = Store()
    pulse = PulseAgent(name="p", engine=MockEngine(["done"]), store=store, clock=FakeClock())
    task_id = pulse.schedule("do the thing")
    rec = get_task(store, task_id)
    assert rec is not None
    assert rec.status == "scheduled"
    await pulse.tick_once()
    assert get_task(store, task_id).status == "completed"


async def test_list_tasks_filters_by_status() -> None:
    store = Store()
    clock = FakeClock()
    pulse = PulseAgent(name="p", engine=MockEngine(["a", "b"]), store=store, clock=clock)
    done_id = pulse.schedule("first")
    await pulse.tick_once()
    pending_id = pulse.schedule_after("second", 3600)

    completed = list_tasks(store, status="completed")
    assert [r.task_id for r in completed] == [done_id]
    scheduled = list_tasks(store, status="scheduled")
    assert [r.task_id for r in scheduled] == [pending_id]
    assert len(list_tasks(store)) == 2


async def test_list_tasks_newest_first_and_bounded() -> None:
    store = Store()
    clock = FakeClock()
    pulse = PulseAgent(name="p", engine=MockEngine(["a", "b", "c"]), store=store, clock=clock)
    first = pulse.schedule("first")
    clock.advance(1)
    second = pulse.schedule("second")
    clock.advance(1)
    third = pulse.schedule("third")

    ordered = list_tasks(store)
    assert [r.task_id for r in ordered] == [third, second, first]
    assert [r.task_id for r in list_tasks(store, limit=2)] == [third, second]


def test_list_tasks_rejects_non_positive_limit() -> None:
    store = Store()
    with pytest.raises(ValueError, match="limit must be positive"):
        list_tasks(store, limit=0)
