# Scheduling: the calendar

A `Calendar` is the timetable of recurring work you hand a `PulseAgent`. You
declare it in Python, the agent reconciles it against the Store at startup, and
the tick loop fires it.

```python
from datetime import date, timedelta

from lazybridge import Store
from lazypulse import (
    ActionClass, After, BusinessDays, Calendar, Cron, PulseAgent,
)

calendar = Calendar([
    Cron("etf_daily_stats", "Run the daily ETF stats and send the digest",
         "45 15 * * MON-FRI",
         tz="Europe/Rome",
         action=ActionClass.EXTERNAL_SEND,
         on_days=BusinessDays(holidays=[date(2026, 1, 1), date(2026, 12, 25)]),
         misfire_grace=timedelta(minutes=45)),

    After("anomaly_check", "Investigate today's anomalies",
          after="etf_daily_stats", within=timedelta(hours=2)),

    Cron("weekly_review", "Verify the week's explanations", "0 10 * * SAT",
         tz="Europe/Rome"),
])

pulse = PulseAgent(name="pulse", engine=..., store=Store(db="pulse.db"),
                   calendar=calendar)
pulse.serve()
```

Requires the `cron` extra: `pip install 'lazypulse[cron]'`.

## The name is the identity

Each entry is keyed by its `name`, not by a generated id. Registering the same
calendar again — every process restart does — updates the entries in place. A
uuid-keyed record would instead leave you with one extra copy of the daily job
per restart, and three digests a day by Wednesday.

That also makes the calendar reconcilable. On `sync`:

- names not in the Store are **created**;
- known names keep their runtime state (next fire time, counters, paused flag)
  and adopt the declared spec;
- the fire time is recomputed **only** when `expr` or `tz` actually changed, so
  a restart never shifts an unchanged schedule;
- entries the calendar no longer declares are **removed** — but only the ones it
  created. An ad-hoc `schedule_cron` is never pruned by a sync.

## Execution semantics

The parts a bare cron trigger leaves out, and why each one is there.

### Misfire grace

`misfire_grace` is how late a slot may still fire. Past it the occurrence is
skipped, counted in `missed_count`, and reported as `pulse.schedule_missed`.

Without it, an agent that was down from Monday to Friday wakes up and
immediately runs the 15:45 market digest — at 23:00, on stale data. `None` (the
default) keeps the old behaviour: fire no matter how late.

### Coalescing

Missed slots never queue up. The next fire time is always computed forward from
*now*, so a six-hour outage of an hourly job produces one occurrence, not six.
This is not configurable: replaying a backlog of LLM runs is expensive and
almost never what a recurring job wants.

### Overlap

`overlap="skip"` (default) will not start a run while the previous one is still
`scheduled`, `running`, or `awaiting_review`. `overlap="allow"` starts it
anyway. A 40-minute job on a 30-minute schedule stacks up without this.

### Day filters

`on_days=BusinessDays(holidays=[...])` skips slots falling on a weekend or a
listed holiday. The date is evaluated **in the entry's own timezone**, so a
00:30 Asia/Tokyo slot is tested against the Tokyo date, not the UTC one.

Holidays are plain `date` objects, which keeps the filter free of any market-data
dependency — feed it from whatever exchange calendar you already trust.

## `After`: depend on completion, not on the clock

A follow-up job scheduled 30 minutes after its predecessor is a guess. When the
predecessor runs long, the follow-up reads yesterday's output and reports on it
with total confidence.

`After` waits for the predecessor's task to actually reach `completed`:

```python
After("anomaly_check", "Investigate today's anomalies",
      after="etf_daily_stats", within=timedelta(hours=2))
```

It fires at most once per predecessor run. If the predecessor completed more
than `within` ago, the occurrence is recorded as missed instead of acting on
stale work. If the predecessor failed, nothing fires — a retry may still
complete it.

## Operating a calendar

```python
pulse.list_schedules()          # every entry with its live state
pulse.get_schedule("weekly_review")
pulse.pause_schedule("etf_daily_stats")   # keeps the record and its history
pulse.resume_schedule("etf_daily_stats")
pulse.remove_schedule("ad_hoc_job")       # declared entries return on next sync
```

Each record carries `fire_count`, `missed_count`, `consecutive_failures`,
`last_fire_at` and `last_task_id`, so a schedule that has been quietly failing
for a week is visible rather than inferred from missing Telegram messages.

Session events: `pulse.schedule_fired`, `pulse.schedule_missed` (with a `reason`
of `misfire_grace_exceeded`, `non_business_day`, `overlap` or
`within_window_elapsed`), and `pulse.schedule_error`. `TickReport` gains `fired`
and `missed`.

For one-off ad-hoc entries there is `schedule_cron(name, text, expr, ...)` and
the generic `add_schedule(entry)`. Prefer a `Calendar` for anything that should
survive a restart — it keeps the timetable in code, where it can be reviewed.

## Letting the agent manage its own calendar

`CalendarTools` turns the calendar into a tool set, so the agent can read and
change its own timetable during an ordinary run.

```python
from lazypulse import CalendarTools

store = Store(db="pulse.db")
tools = CalendarTools(store, min_interval_seconds=300, max_agent_schedules=20)

pulse = PulseAgent(name="pulse", engine=..., store=store,
                   calendar=calendar, tools=tools.tools())
```

The agent gets `calendar_list`, `calendar_add_cron`, `calendar_add_after`,
`calendar_update`, `calendar_pause`, `calendar_resume` and `calendar_remove`.
Pass `writable=False` to expose only `calendar_list` — enough for the agent to
explain its schedule, not to change it.

### The autonomy boundary

An agent that can rewrite its own timetable can also schedule itself into a
spend loop, so the tools draw a line:

| | declared in a `Calendar` | created by the agent |
|---|---|---|
| list | yes | yes |
| pause / resume | yes | yes |
| update / remove | **no** | yes |
| pruned by `sync` | yes | never |
| counts against quota | no | yes |

Declared entries stay owned by code. Letting the agent rewrite them would be
worse than refusing, because the next `sync` would silently restore them and the
agent's change would vanish without explanation. Pause and resume are allowed —
that is the operationally useful half ("the upstream feed is down, hold the
digest until I say otherwise").

Two more guards apply to what the agent creates: a cron whose consecutive fire
times are closer together than `min_interval_seconds` (default 300) is refused,
and it may hold at most `max_agent_schedules` entries of its own.

Bad arguments come back as `"Error: ..."` strings rather than raising, so a model
that gets a cron expression wrong reads the reason and corrects itself instead
of failing the run.
