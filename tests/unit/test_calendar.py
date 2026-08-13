"""Calendar scheduler: identity, misfire grace, overlap, day filters, After."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from lazybridge import Store

from lazypulse import ActionClass, After, BusinessDays, Calendar, Cron, PulseAgent
from lazypulse.testing import FakeClock, MockEngine

try:
    import croniter  # noqa: F401

    _HAS_CRONITER = True
except ImportError:
    _HAS_CRONITER = False

pytestmark = pytest.mark.skipif(not _HAS_CRONITER, reason="croniter not installed")

# Friday 2026-01-02, 08:00 UTC — a business day, so day-filtered tests start
# from a slot that is allowed unless the test says otherwise.
_START = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)


def _agent(clock: FakeClock, *, calendar: Calendar | None = None, store: Store | None = None) -> PulseAgent:
    return PulseAgent(
        name="cal",
        engine=MockEngine(["ok"]),
        store=store if store is not None else Store(),
        clock=clock,
        calendar=calendar,
        unsafe_allow_all=True,
    )


# --- Identity ----------------------------------------------------------- #


def test_calendar_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="Duplicate schedule name"):
        Calendar([Cron("dup", "a", "* * * * *"), Cron("dup", "b", "0 * * * *")])


def test_after_must_reference_a_declared_entry() -> None:
    with pytest.raises(ValueError, match="does not declare"):
        Calendar([After("dependent", "t", after="ghost", within=timedelta(hours=1))])


def test_schedule_name_cannot_contain_the_key_separator() -> None:
    with pytest.raises(ValueError, match="Invalid schedule name"):
        Cron("bad:name", "t", "* * * * *")


def test_re_registering_the_same_calendar_does_not_duplicate() -> None:
    """The bug the name-keyed record exists to prevent: one entry per restart."""
    clock = FakeClock(start=_START)
    store = Store()
    calendar = Calendar([Cron("daily", "work", "0 9 * * *")])

    for _ in range(3):  # three process restarts against one persistent Store
        agent = _agent(clock, calendar=calendar, store=store)

    assert [r.name for r in agent.list_schedules()] == ["daily"]


def test_sync_removes_managed_entries_the_calendar_dropped() -> None:
    clock = FakeClock(start=_START)
    store = Store()
    _agent(clock, calendar=Calendar([Cron("a", "t", "0 9 * * *"), Cron("b", "t", "0 9 * * *")]), store=store)
    agent = _agent(clock, calendar=Calendar([Cron("a", "t", "0 9 * * *")]), store=store)

    assert [r.name for r in agent.list_schedules()] == ["a"]


def test_sync_leaves_ad_hoc_entries_alone() -> None:
    """An ad-hoc entry was never the calendar's to delete."""
    clock = FakeClock(start=_START)
    store = Store()
    agent = _agent(clock, store=store)
    agent.schedule_cron("ad_hoc", "t", "0 9 * * *")

    agent = _agent(clock, calendar=Calendar([Cron("declared", "t", "0 9 * * *")]), store=store)

    assert sorted(r.name for r in agent.list_schedules()) == ["ad_hoc", "declared"]


# --- Firing ------------------------------------------------------------- #


def test_cron_entry_fires_and_records_its_task() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock, calendar=Calendar([Cron("hourly", "do the thing", "0 * * * *")]))

    clock.advance(3600)  # past 09:00
    report = agent.tick()

    assert report.fired == 1
    assert report.completed == 1
    record = agent.get_schedule("hourly")
    assert record is not None
    assert record.fire_count == 1
    assert record.last_task_id is not None
    assert record.consecutive_failures == 0


def test_fired_task_carries_the_declared_action_class() -> None:
    """A recurring job that sends Telegram must not read as a public read."""
    clock = FakeClock(start=_START)
    agent = _agent(
        clock,
        calendar=Calendar([Cron("digest", "send digest", "0 * * * *", action=ActionClass.EXTERNAL_SEND)]),
    )
    clock.advance(3600)
    agent.tick()

    record = agent.get_schedule("digest")
    assert record is not None
    raw = agent.store.read(f"pulse:task:{record.last_task_id}")
    assert raw["action_class"] == ActionClass.EXTERNAL_SEND


def test_outage_coalesces_into_a_single_firing() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock, calendar=Calendar([Cron("hourly", "t", "0 * * * *")]))

    clock.advance(6 * 3600)  # six missed hourly slots
    report = agent.tick()

    assert report.fired == 1  # one occurrence, not a backlog of six


def test_paused_schedule_does_not_fire_and_resumes_cleanly() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock, calendar=Calendar([Cron("hourly", "t", "0 * * * *")]))

    assert agent.pause_schedule("hourly") is True
    assert agent.pause_schedule("hourly") is False  # already paused
    clock.advance(3600)
    assert agent.tick().fired == 0

    assert agent.resume_schedule("hourly") is True
    clock.advance(3600)
    assert agent.tick().fired == 1


def test_resuming_does_not_fire_the_slot_that_passed_while_paused() -> None:
    """A held schedule keeps its fire time moving, so resume starts fresh."""
    clock = FakeClock(start=_START)
    agent = _agent(clock, calendar=Calendar([Cron("hourly", "t", "0 * * * *")]))

    agent.pause_schedule("hourly")
    clock.advance(3 * 3600)  # three slots go by while held
    assert agent.tick().fired == 0
    held = agent.get_schedule("hourly")
    assert held is not None
    assert held.next_fire_at is not None
    assert held.next_fire_at > clock.now  # advanced past them, not left stale
    assert held.missed_count == 0  # pausing is deliberate, not a missed slot

    agent.resume_schedule("hourly")
    # The very next tick must not fire: there is no stale occurrence left over.
    assert agent.tick().fired == 0
    clock.advance(3600)
    assert agent.tick().fired == 1


def test_declaring_a_name_the_agent_created_takes_ownership() -> None:
    """A Calendar declaration claims the name; the agent may no longer rewrite it."""
    clock = FakeClock(start=_START)
    store = Store()
    agent = _agent(clock, store=store)
    agent.schedule_cron("shared_name", "creata dall'agente", "0 * * * *")
    store.write(  # mark it agent-owned, as CalendarTools does
        "pulse:schedule:shared_name",
        {**store.read("pulse:schedule:shared_name"), "created_by": "agent"},
    )

    agent = _agent(clock, calendar=Calendar([Cron("shared_name", "dichiarata", "0 * * * *")]), store=store)

    record = agent.get_schedule("shared_name")
    assert record is not None
    assert record.managed is True
    assert record.created_by is None  # ownership moved to code
    assert record.spec.text == "dichiarata"


def test_a_single_failed_run_counts_once_across_skipped_slots() -> None:
    """The failure counter must not re-observe the same failed task on skips."""
    clock = FakeClock(start=datetime(2026, 1, 2, 8, 0, tzinfo=UTC))  # Friday
    store = Store()
    agent = _agent(
        clock,
        calendar=Calendar([Cron("daily", "t", "0 9 * * *", on_days=BusinessDays())]),
        store=store,
    )

    clock.advance(3600)  # Fri 09:00 — fires
    agent.tick()
    record = agent.get_schedule("daily")
    assert record is not None
    task_key = f"pulse:task:{record.last_task_id}"
    store.write(task_key, {**store.read(task_key), "status": "failed"})

    clock.advance(24 * 3600)  # Sat — skipped
    agent.tick()
    clock.advance(24 * 3600)  # Sun — skipped
    agent.tick()
    after_skips = agent.get_schedule("daily")
    assert after_skips is not None
    assert after_skips.missed_count == 2
    assert after_skips.consecutive_failures == 0  # not folded in yet — no run happened

    clock.advance(24 * 3600)  # Mon — fires, and observes Friday's failure once
    agent.tick()
    monday = agent.get_schedule("daily")
    assert monday is not None
    assert monday.consecutive_failures == 1  # one failed run, counted once


def test_after_still_fires_when_retention_is_shorter_than_its_window() -> None:
    """Schedules are evaluated before the prune pass deletes their trigger."""
    clock = FakeClock(start=_START)
    agent = PulseAgent(
        name="cal",
        engine=MockEngine(["ok"]),
        store=Store(),
        clock=clock,
        terminal_retention=1.0,  # retention far shorter than the 2h window
        calendar=Calendar(
            [
                Cron("producer", "produce", "0 * * * *"),
                After("consumer", "consume", after="producer", within=timedelta(hours=2)),
            ]
        ),
        unsafe_allow_all=True,
    )

    clock.advance(3600)
    assert agent.tick().fired == 1  # producer runs and completes
    clock.advance(120)  # older than retention, still well inside `within`

    assert agent.tick().fired == 1  # the dependent still saw its trigger
    consumer = agent.get_schedule("consumer")
    assert consumer is not None
    assert consumer.fire_count == 1


def test_remove_schedule() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock)
    agent.schedule_cron("gone", "t", "0 * * * *")

    assert agent.remove_schedule("gone") is True
    assert agent.remove_schedule("gone") is False
    assert agent.list_schedules() == []


# --- Misfire grace ------------------------------------------------------ #


def test_stale_slot_is_skipped_when_grace_is_exceeded() -> None:
    """The 15:45 market job must not fire at 23:00 after an outage."""
    clock = FakeClock(start=_START)
    agent = _agent(
        clock,
        calendar=Calendar([Cron("hourly", "t", "0 * * * *", misfire_grace=timedelta(minutes=30))]),
    )

    clock.advance(5 * 3600)  # the 09:00 slot is now four hours stale
    report = agent.tick()

    assert report.fired == 0
    assert report.missed == 1
    record = agent.get_schedule("hourly")
    assert record is not None
    assert record.missed_count == 1
    assert record.fire_count == 0
    # The occurrence is passed over once, not retried forever.
    assert record.next_fire_at is not None
    assert record.next_fire_at > clock.now


def test_slot_inside_the_grace_window_still_fires() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(
        clock,
        calendar=Calendar([Cron("hourly", "t", "0 * * * *", misfire_grace=timedelta(minutes=30))]),
    )

    clock.advance(3600 + 600)  # 09:00 slot, ten minutes late
    report = agent.tick()

    assert report.fired == 1


def test_no_grace_means_fire_no_matter_how_late() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock, calendar=Calendar([Cron("hourly", "t", "0 * * * *")]))

    clock.advance(48 * 3600)
    assert agent.tick().fired == 1


# --- Day filter --------------------------------------------------------- #


def test_holiday_slot_is_skipped() -> None:
    clock = FakeClock(start=datetime(2025, 12, 31, 23, 0, tzinfo=UTC))
    agent = _agent(
        clock,
        calendar=Calendar([Cron("daily", "t", "0 9 * * *", on_days=BusinessDays(holidays=[date(2026, 1, 1)]))]),
    )

    clock.advance(11 * 3600)  # 2026-01-01 10:00 — past the 09:00 slot, a holiday
    report = agent.tick()

    assert report.fired == 0
    assert report.missed == 1


def test_weekend_slot_is_skipped_but_the_next_weekday_fires() -> None:
    # Saturday 2026-01-03, 08:00 UTC.
    clock = FakeClock(start=datetime(2026, 1, 3, 8, 0, tzinfo=UTC))
    agent = _agent(clock, calendar=Calendar([Cron("daily", "t", "0 9 * * *", on_days=BusinessDays())]))

    clock.advance(3600)  # Sat 09:00
    assert agent.tick().missed == 1
    clock.advance(24 * 3600)  # Sun 09:00
    assert agent.tick().missed == 1
    clock.advance(24 * 3600)  # Mon 09:00
    assert agent.tick().fired == 1


def test_day_filter_uses_the_entrys_timezone() -> None:
    """A 00:30 Tokyo slot is still Monday there while it is Sunday in UTC."""
    entry = Cron("tokyo", "t", "30 0 * * *", tz="Asia/Tokyo", on_days=BusinessDays())
    # 2026-01-05 00:30 Tokyo == 2026-01-04 15:30 UTC (a Sunday).
    slot_utc = datetime(2026, 1, 4, 15, 30, tzinfo=UTC)
    assert slot_utc.date().weekday() == 6  # Sunday in UTC
    assert entry.local_date(slot_utc) == date(2026, 1, 5)
    assert entry.on_days is not None
    assert entry.on_days.allows(entry.local_date(slot_utc)) is True


# --- Overlap ------------------------------------------------------------ #


def test_overlap_skip_does_not_stack_a_second_run() -> None:
    clock = FakeClock(start=_START)
    store = Store()
    agent = _agent(clock, calendar=Calendar([Cron("hourly", "t", "0 * * * *")]), store=store)

    clock.advance(3600)
    agent.tick()
    record = agent.get_schedule("hourly")
    assert record is not None
    # Park the produced task as if the worker were still busy.
    task_key = f"pulse:task:{record.last_task_id}"
    store.write(task_key, {**store.read(task_key), "status": "running"})

    clock.advance(3600)
    report = agent.tick()

    assert report.fired == 0
    assert report.missed == 1


def test_overlap_allow_starts_the_next_run_anyway() -> None:
    clock = FakeClock(start=_START)
    store = Store()
    agent = _agent(clock, calendar=Calendar([Cron("hourly", "t", "0 * * * *", overlap="allow")]), store=store)

    clock.advance(3600)
    agent.tick()
    record = agent.get_schedule("hourly")
    assert record is not None
    task_key = f"pulse:task:{record.last_task_id}"
    store.write(task_key, {**store.read(task_key), "status": "running"})

    clock.advance(3600)
    assert agent.tick().fired == 1


# --- After (dependency) ------------------------------------------------- #


def _after_calendar() -> Calendar:
    return Calendar(
        [
            Cron("producer", "produce", "0 * * * *"),
            After("consumer", "consume", after="producer", within=timedelta(hours=2)),
        ]
    )


def test_after_fires_once_the_predecessor_completes() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock, calendar=_after_calendar())

    clock.advance(3600)
    first = agent.tick()  # producer fires and completes within the same tick
    assert first.fired == 1

    second = agent.tick()  # consumer sees the completed predecessor
    assert second.fired == 1
    consumer = agent.get_schedule("consumer")
    assert consumer is not None
    assert consumer.fire_count == 1
    assert consumer.last_trigger_task_id == agent.get_schedule("producer").last_task_id  # type: ignore[union-attr]


def test_after_fires_only_once_per_predecessor_run() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock, calendar=_after_calendar())

    clock.advance(3600)
    agent.tick()
    agent.tick()
    for _ in range(3):  # more ticks, same predecessor run
        assert agent.tick().fired == 0

    consumer = agent.get_schedule("consumer")
    assert consumer is not None
    assert consumer.fire_count == 1


def test_after_does_not_fire_while_the_predecessor_is_still_running() -> None:
    clock = FakeClock(start=_START)
    store = Store()
    agent = _agent(clock, calendar=_after_calendar(), store=store)

    clock.advance(3600)
    agent.tick()
    producer = agent.get_schedule("producer")
    assert producer is not None
    task_key = f"pulse:task:{producer.last_task_id}"
    store.write(task_key, {**store.read(task_key), "status": "running"})

    assert agent.tick().fired == 0
    consumer = agent.get_schedule("consumer")
    assert consumer is not None
    assert consumer.fire_count == 0


def test_after_skips_when_the_window_has_elapsed() -> None:
    """Better nothing than analysing a predecessor's stale output."""
    clock = FakeClock(start=_START)
    # A *daily* producer, so the elapsed window is not masked by the producer
    # firing again and handing the consumer a fresh run to follow.
    agent = _agent(
        clock,
        calendar=Calendar(
            [
                Cron("producer", "produce", "0 9 * * *"),
                After("consumer", "consume", after="producer", within=timedelta(hours=1)),
            ]
        ),
    )

    clock.advance(3600)
    agent.tick()  # producer completes here
    clock.advance(2 * 3600)  # past the one-hour window

    report = agent.tick()

    consumer = agent.get_schedule("consumer")
    assert consumer is not None
    assert consumer.fire_count == 0
    assert consumer.missed_count == 1
    assert report.missed >= 1
