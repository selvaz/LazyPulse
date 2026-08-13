"""The agent managing its own calendar: tool surface, guardrails, ownership."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from lazybridge import Store

from lazypulse import Calendar, CalendarTools, Cron, PulseAgent
from lazypulse.testing import FakeClock, MockEngine

try:
    import croniter  # noqa: F401

    _HAS_CRONITER = True
except ImportError:
    _HAS_CRONITER = False

pytestmark = pytest.mark.skipif(not _HAS_CRONITER, reason="croniter not installed")

_START = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)


def _fixture(
    *, calendar: Calendar | None = None, store: Store | None = None, **tool_kwargs: object
) -> tuple[PulseAgent, CalendarTools, FakeClock, Store]:
    clock = FakeClock(start=_START)
    store = store if store is not None else Store()
    tools = CalendarTools(store, clock=clock, **tool_kwargs)  # type: ignore[arg-type]
    agent = PulseAgent(
        name="p",
        engine=MockEngine(["ok"]),
        store=store,
        clock=clock,
        calendar=calendar,
        tools=tools.tools(),
        unsafe_allow_all=True,
    )
    return agent, tools, clock, store


# --- Tool surface -------------------------------------------------------- #


def test_tools_are_wrapped_with_schemas_from_their_signatures() -> None:
    _, tools, _, _ = _fixture()
    wrapped = {t.name: t for t in tools.tools()}

    assert set(wrapped) == {
        "calendar_list",
        "calendar_add_cron",
        "calendar_add_after",
        "calendar_update",
        "calendar_pause",
        "calendar_resume",
        "calendar_remove",
    }
    params = wrapped["calendar_add_cron"].definition().parameters
    assert set(params["required"]) == {"name", "task", "cron"}
    assert "business_days_only" in params["properties"]


def test_read_only_mode_exposes_no_write_tools() -> None:
    _, tools, _, _ = _fixture(writable=False)
    assert [t.name for t in tools.tools()] == ["calendar_list"]


# --- The agent's own schedules ------------------------------------------ #


def test_agent_scheduled_work_actually_fires() -> None:
    """The whole point: what the agent schedules, the tick loop runs."""
    agent, tools, clock, _ = _fixture()
    tools.calendar_add_cron("hourly", "do the follow-up", "0 * * * *")

    clock.advance(3600)
    report = agent.tick()

    assert report.fired == 1
    assert report.completed == 1
    record = agent.get_schedule("hourly")
    assert record is not None
    assert record.created_by == "agent"


def test_agent_entries_survive_a_calendar_sync() -> None:
    """A restart re-syncs the declared calendar; self-scheduled work stays."""
    calendar = Calendar([Cron("declared", "t", "0 9 * * *")])
    _, tools, clock, store = _fixture(calendar=calendar)
    tools.calendar_add_cron("self_made", "my own follow-up", "0 10 * * *")

    agent = PulseAgent(  # a fresh process against the same Store
        name="p2",
        engine=MockEngine(["ok"]),
        store=store,
        clock=clock,
        calendar=calendar,
        unsafe_allow_all=True,
    )

    assert sorted(r.name for r in agent.list_schedules()) == ["declared", "self_made"]


def test_agent_can_update_and_remove_its_own_entries() -> None:
    agent, tools, _, _ = _fixture()
    tools.calendar_add_cron("mine", "old instruction", "0 * * * *")

    assert "Updated" in tools.calendar_update("mine", task="new instruction")
    record = agent.get_schedule("mine")
    assert record is not None
    assert record.spec.text == "new instruction"

    assert "Removed" in tools.calendar_remove("mine")
    assert agent.get_schedule("mine") is None


# --- Ownership boundary -------------------------------------------------- #


def test_declared_entries_cannot_be_rewritten_or_deleted() -> None:
    agent, tools, _, _ = _fixture(calendar=Calendar([Cron("declared", "keep me", "0 9 * * *")]))

    assert "declared in the application's calendar" in tools.calendar_update("declared", task="hijacked")
    assert "declared in the application's calendar" in tools.calendar_remove("declared")

    record = agent.get_schedule("declared")
    assert record is not None
    assert record.spec.text == "keep me"


def test_declared_entries_can_be_paused_and_resumed() -> None:
    agent, tools, clock, _ = _fixture(calendar=Calendar([Cron("declared", "t", "0 * * * *")]))

    assert "Paused" in tools.calendar_pause("declared")
    clock.advance(3600)
    assert agent.tick().fired == 0

    assert "Resumed" in tools.calendar_resume("declared")
    clock.advance(3600)
    assert agent.tick().fired == 1


def test_a_paused_declared_entry_stays_paused_across_a_sync() -> None:
    calendar = Calendar([Cron("declared", "t", "0 * * * *")])
    _, tools, clock, store = _fixture(calendar=calendar)
    tools.calendar_pause("declared")

    agent = PulseAgent(
        name="p2",
        engine=MockEngine(["ok"]),
        store=store,
        clock=clock,
        calendar=calendar,
        unsafe_allow_all=True,
    )

    record = agent.get_schedule("declared")
    assert record is not None
    assert record.paused is True


def test_a_name_the_agent_creates_can_be_reused_by_the_agent() -> None:
    _, tools, _, _ = _fixture()
    tools.calendar_add_cron("mine", "v1", "0 * * * *")
    assert "Updated" in tools.calendar_add_cron("mine", "v2", "0 * * * *")


# --- Guardrails ---------------------------------------------------------- #


def test_a_runaway_cadence_is_refused() -> None:
    agent, tools, _, _ = _fixture()
    out = tools.calendar_add_cron("spam", "loop", "* * * * *")

    assert out.startswith("Error:")
    assert "more often than" in out
    assert agent.get_schedule("spam") is None


def test_the_cadence_floor_is_configurable() -> None:
    _, tools, _, _ = _fixture(min_interval_seconds=30)
    assert "Scheduled" in tools.calendar_add_cron("fast", "t", "* * * * *")


def test_quota_caps_self_scheduling_but_allows_replacing() -> None:
    _, tools, _, _ = _fixture(max_agent_schedules=2)
    tools.calendar_add_cron("one", "t", "0 * * * *")
    tools.calendar_add_cron("two", "t", "0 * * * *")

    assert "maximum allowed" in tools.calendar_add_cron("three", "t", "0 * * * *")
    # Replacing an entry it already owns must still work at the cap.
    assert "Updated" in tools.calendar_add_cron("one", "revised", "0 * * * *")


def test_declared_entries_do_not_consume_the_agents_quota() -> None:
    calendar = Calendar([Cron("a", "t", "0 9 * * *"), Cron("b", "t", "0 9 * * *")])
    _, tools, _, _ = _fixture(calendar=calendar, max_agent_schedules=1)

    assert "Scheduled" in tools.calendar_add_cron("mine", "t", "0 * * * *")


# --- Errors come back as text ------------------------------------------- #


@pytest.mark.parametrize(
    ("call", "fragment"),
    [
        (lambda t: t.calendar_add_cron("bad name", "t", "0 * * * *"), "Invalid schedule name"),
        (lambda t: t.calendar_add_cron("ok", "t", "not a cron"), "Error:"),
        (lambda t: t.calendar_add_cron("ok", "t", "0 * * * *", tz="Not/AZone"), "Error:"),
        (lambda t: t.calendar_add_after("f", "t", after="ghost"), "no schedule named"),
        (lambda t: t.calendar_update("ghost", task="t"), "no schedule named"),
        (lambda t: t.calendar_pause("ghost"), "no schedule named"),
        (lambda t: t.calendar_resume("ghost"), "no schedule named"),
        (lambda t: t.calendar_remove("ghost"), "no schedule named"),
    ],
)
def test_bad_input_returns_a_message_instead_of_raising(call, fragment: str) -> None:  # type: ignore[no-untyped-def]
    _, tools, _, _ = _fixture()
    assert fragment in call(tools)


def test_cannot_depend_on_itself() -> None:
    _, tools, _, _ = _fixture()
    tools.calendar_add_cron("loop", "t", "0 * * * *")
    assert "cannot depend on itself" in tools.calendar_add_after("loop", "t", after="loop")


def test_cron_fields_are_refused_on_an_after_entry() -> None:
    _, tools, _, _ = _fixture()
    tools.calendar_add_cron("producer", "t", "0 * * * *")
    tools.calendar_add_after("consumer", "t", after="producer")

    assert "no cron expression" in tools.calendar_update("consumer", cron="0 9 * * *")


def test_update_with_nothing_to_change_says_so() -> None:
    _, tools, _, _ = _fixture()
    tools.calendar_add_cron("mine", "t", "0 * * * *")
    assert "Nothing to change" in tools.calendar_update("mine")


# --- Listing ------------------------------------------------------------- #


def test_list_reports_ownership_timing_and_history() -> None:
    agent, tools, clock, _ = _fixture(calendar=Calendar([Cron("declared", "t", "0 * * * *")]))
    tools.calendar_add_cron("mine", "t", "0 * * * *")
    clock.advance(3600)
    agent.tick()

    rows = {row["name"]: row for row in tools.calendar_list()}

    assert rows["declared"]["owner"] == "code"
    assert rows["mine"]["owner"] == "agent"
    assert rows["mine"]["runs"] == 1
    assert rows["mine"]["next_run_at"] is not None
    assert rows["mine"]["when"].startswith("cron 0 * * * *")
