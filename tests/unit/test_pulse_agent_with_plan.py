"""Plan-as-engine: a PulseAgent whose engine is a deterministic Plan.

Because PulseAgent dispatches through the ordinary ``Agent.run`` path, a
Plan engine works with no special handling — every step runs, and
``routes_by`` routing is honoured. Per-task isolation comes from each
inbound message getting its own PulseRecord (task_id); the Plan itself runs
fresh per task.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from lazybridge import Plan, Step, Store
from lazybridge.testing import MockAgent
from pydantic import BaseModel

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.models import PulseRecord
from lazypulse.testing import FakeClock, MockAdapter


def _msg(mid: str, text: str = "go") -> InboundMessage:
    return InboundMessage(source="mock", message_id=mid, received_at=datetime(2026, 1, 1, tzinfo=UTC), text=text)


def _records(store: Store) -> list[PulseRecord]:
    return [PulseRecord.model_validate(store.read(k)) for k in list(store.keys()) if k.startswith(store_keys.TASK_PREFIX)]


async def test_linear_plan_runs_all_steps() -> None:
    store = Store()
    a = MockAgent("A-out", name="a")
    b = MockAgent("B-out", name="b")
    pulse = PulseAgent(
        unsafe_allow_all=True,
        name="pipe",
        engine=Plan(Step("a"), Step("b")),
        tools=[a, b],
        store=store,
        clock=FakeClock(),
        adapters=[MockAdapter([_msg("1")])],
    )
    report = await pulse.tick_once()
    assert report.completed == 1
    assert len(a.calls) == 1 and len(b.calls) == 1
    rec = _records(store)[0]
    assert rec.status == "completed"
    assert rec.worker_text == "B-out"  # final step output


class Triage(BaseModel):
    category: Literal["research", "calendar"]
    confidence: float


async def test_plan_routes_by_field() -> None:
    store = Store()
    triager = MockAgent(Triage(category="calendar", confidence=0.95), name="triager", output=Triage)
    research = MockAgent("RESEARCH", name="research")
    calendar = MockAgent("CALENDAR", name="calendar")
    pulse = PulseAgent(
        unsafe_allow_all=True,
        name="router",
        engine=Plan(
            Step("triager", output=Triage, routes_by="category"),
            Step("research"),
            Step("calendar"),
        ),
        tools=[triager, research, calendar],
        store=store,
        clock=FakeClock(),
        adapters=[MockAdapter([_msg("1")])],
    )
    report = await pulse.tick_once()
    assert report.completed == 1
    rec = _records(store)[0]
    assert rec.worker_text == "CALENDAR"
    # Routed to calendar; research was skipped (detour semantics from the
    # router step, which is declared last).
    assert len(calendar.calls) == 1
    assert len(research.calls) == 0


async def test_each_message_gets_its_own_task_record() -> None:
    store = Store()
    a = MockAgent("A", name="a")
    b = MockAgent("B", name="b")
    pulse = PulseAgent(
        unsafe_allow_all=True,
        name="pipe",
        engine=Plan(Step("a"), Step("b")),
        tools=[a, b],
        store=store,
        clock=FakeClock(),
        adapters=[MockAdapter([_msg("1"), _msg("2"), _msg("3")])],
    )
    report = await pulse.tick_once()
    assert report.completed == 3
    recs = _records(store)
    assert len({r.task_id for r in recs}) == 3
    assert len(a.calls) == 3 and len(b.calls) == 3
