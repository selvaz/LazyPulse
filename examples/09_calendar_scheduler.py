"""A calendar of recurring work, plus an agent that can manage it itself.

Declares three schedules — a weekday market job, a follow-up that waits for it
to *complete* rather than guessing a clock offset, and a weekly review — then
shows the parts a bare cron trigger leaves out: a stale slot is skipped instead
of firing at the wrong time of day, and a holiday is passed over.

The second half hands the agent ``CalendarTools`` so it can list, add, pause and
remove schedules during a run, and shows where its autonomy stops: entries
declared in code can be paused but not rewritten.

Runs offline on a fake clock (no API key needed), fully synchronous.

    python examples/09_calendar_scheduler.py
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from lazybridge import Store

from lazypulse import (
    ActionClass,
    After,
    BusinessDays,
    Calendar,
    CalendarTools,
    Cron,
    PulseAgent,
)
from lazypulse.testing import FakeClock, MockEngine


def main() -> None:
    # Thursday 2026-12-24, 08:00 UTC — the day before a holiday.
    clock = FakeClock(start=datetime(2026, 12, 24, 8, 0, tzinfo=UTC))
    store = Store()

    calendar = Calendar(
        [
            # 15:45 Rome on weekdays, skipping holidays. If the process was
            # down and the slot is more than 45 minutes stale, skip it — a
            # market digest at 23:00 is worse than no digest.
            Cron(
                "etf_daily_stats",
                "Run the daily ETF stats and send the digest",
                "45 15 * * MON-FRI",
                tz="Europe/Rome",
                action=ActionClass.EXTERNAL_SEND,
                on_days=BusinessDays(holidays=[date(2026, 12, 25), date(2027, 1, 1)]),
                misfire_grace=timedelta(minutes=45),
            ),
            # Not "30 minutes later" — when the stats job has actually
            # finished, so this never analyses yesterday's row.
            After(
                "anomaly_check",
                "Investigate today's anomalies",
                after="etf_daily_stats",
                within=timedelta(hours=2),
            ),
            Cron("weekly_review", "Verify the week's explanations", "0 10 * * SAT", tz="Europe/Rome"),
        ]
    )

    tools = CalendarTools(store, clock=clock, min_interval_seconds=300, max_agent_schedules=5)
    pulse = PulseAgent(
        name="scheduler",
        engine=MockEngine(["done"]),
        store=store,
        clock=clock,
        calendar=calendar,
        tools=tools.tools(),
        unsafe_allow_all=True,  # dev only — pass policy=... in production
    )

    print("Declared calendar")
    for record in pulse.list_schedules():
        print(f"  {record.name:<18} next: {record.next_fire_at}")

    # --- The daily job fires, and its dependent follows it ---------------- #
    clock.advance(7 * 3600)  # Thu 15:00 UTC = 16:00 Rome, past the 15:45 slot
    report = pulse.tick()
    print(f"\nThu 16:00 Rome  -> fired={report.fired} missed={report.missed}")

    report = pulse.tick()  # the follow-up sees a completed predecessor
    print(f"same tick loop  -> fired={report.fired} (anomaly_check followed it)")

    # --- Christmas: the slot is passed over ------------------------------- #
    clock.advance(24 * 3600)  # Fri 2026-12-25, a listed holiday
    report = pulse.tick()
    stats = pulse.get_schedule("etf_daily_stats")
    assert stats is not None
    print(f"\nFri (holiday)   -> fired={report.fired} missed={report.missed} (skipped, not run late)")
    print(f"                  runs={stats.fire_count} skipped={stats.missed_count}")

    # --- The agent manages its own calendar ------------------------------- #
    print("\nAgent-side calendar management")
    print("  add   :", tools.calendar_add_cron("month_end", "Close the books", "0 9 28-31 * *"))
    print("  after :", tools.calendar_add_after("month_end_check", "Verify the close", after="month_end"))
    print("  loop  :", tools.calendar_add_cron("runaway", "spin", "* * * * *"))
    print("  own   :", tools.calendar_update("month_end", task="Close the books and file"))

    # Entries declared in code stay owned by code — but can be held.
    print("  code  :", tools.calendar_update("etf_daily_stats", task="hijacked"))
    print("  pause :", tools.calendar_pause("etf_daily_stats"))

    print("\nFinal calendar")
    for row in tools.calendar_list():
        flag = " (paused)" if row["paused"] else ""
        print(f"  {row['name']:<18} [{row['owner']:<5}] {row['when']}{flag}")


if __name__ == "__main__":
    main()
