# The packaged launcher

`lazypulse serve` runs an always-on Telegram agent configured entirely from
environment variables — no code to write, no file to copy.

```bash
pip install "lazypulse[telegram,cron] @ git+https://github.com/selvaz/LazyPulse.git"

export BOT_TOKEN=123:AA...        # from @BotFather
export OWNER_ID=123456789         # your Telegram user id
export DEEPSEEK_API_KEY=sk-...    # whatever your MODEL needs
lazypulse serve
```

That gives you the Telegram inbox with an owner-only trust policy, the
human-in-the-loop reviewer (`/approve` · `/reject` in the chat), crash recovery,
Store retention, and a `notify_owner` tool.

Point it at a calendar and it also runs recurring work:

```bash
export CALENDAR_FILE=/data/calendar.toml
lazypulse serve
```

## The calendar file

Each table under `[schedules]` is one entry, keyed by name. `cron` makes a
[`Cron`](scheduling.md); `after` makes an [`After`](scheduling.md) — exactly one
must be present.

```toml
[schedules.etf_daily_stats]
task = "Run the daily ETF stats and send the digest with notify_owner"
cron = "45 15 * * MON-FRI"
tz = "Europe/Rome"
action = "external_send"
misfire_grace_minutes = 45
business_days = true
holidays = ["2026-12-25", 2027-01-01]

[schedules.anomaly_check]
task = "Investigate today's anomalies and report them"
after = "etf_daily_stats"
within_minutes = 120
```

| key | applies to | meaning |
|---|---|---|
| `task` | both | *required* — the instruction to run each time |
| `cron` | cron | five-field expression |
| `tz` | cron | IANA timezone the expression is read in (default `UTC`) |
| `misfire_grace_minutes` | cron | skip the slot if it is later than this. `0` tolerates no lateness at all — and since a slot is only ever observed one tick *after* it passes, that skips essentially every occurrence. Omit the key to fire however late |
| `business_days` | cron | skip weekends |
| `holidays` | cron | ISO dates (or native TOML dates) to skip |
| `after` | after | name of the schedule to follow |
| `within_minutes` | after | skip if the predecessor finished longer ago (default 120) |
| `action` | both | ActionClass recorded on the task (default `read_public`) |
| `overlap` | both | `skip` (default) or `allow` |
| `enabled` | both | `false` to declare it without running it |

Booleans must be real TOML booleans: `enabled = "false"` is rejected rather than
coerced, because Python reads that string as *true* and would run a schedule you
wrote down as off.

An unknown key is an **error**, not a silent no-op: a typo'd `buisness_days`
that quietly did nothing would let a market job run on Christmas. Validate
before deploying:

```bash
lazypulse check-calendar /data/calendar.toml
```

It prints what the file declares and exits non-zero if the file is malformed,
naming the offending schedule. Cron expressions and timezones are compiled
during the check, not on first fire, so a typo'd `45 15 * * MONFRI` or an
unknown zone fails here rather than inside a running `serve`. A BOM (which
Notepad and PowerShell add) is handled — it does not need stripping.

## Delivering scheduled output

A scheduled task has no conversation to reply into: the adapter's reply path
answers *inbound* messages, keyed on the chat they came from. So the launcher
gives the agent a **`notify_owner`** tool, and a scheduled task that should
reach you must say so in its `task` text. Without it the work runs, produces
its answer, and delivers it nowhere.

Set `NOTIFY_TOOL=0` to withhold the tool, and `OWNER_CHAT_ID` to send somewhere
other than the owner's own chat — it redirects the approval requests too, so
every unprompted message the launcher sends lands in the same place.

## Letting the agent manage its own timetable

`CALENDAR_TOOLS=1` adds [`CalendarTools`](scheduling.md#letting-the-agent-manage-its-own-calendar),
so you can add and pause schedules by messaging the bot. The autonomy boundary
still applies: entries from `CALENDAR_FILE` can be paused and resumed but not
rewritten, and `CALENDAR_MIN_INTERVAL` (default 300s) caps how often the agent
can schedule itself.

## Environment

| variable | default | |
|---|---|---|
| `BOT_TOKEN` | — | **required**, from @BotFather |
| `OWNER_ID` | — | **required**, your numeric Telegram user id |
| `MODEL` | `deepseek-v4-flash` | any LazyBridge model id; its provider key must be set |
| `SYSTEM_PROMPT` | a concise-assistant prompt | system instructions |
| `STORE_DB` | `pulse.db` | put it on a mounted volume in a container |
| `BOT_ID` | `lazypulse` | Telegram watermark identity — see the warning below |
| `AGENT_NAME` | `lazypulse` | agent name, used in logs |
| `ADAPTER_NAME` | `telegram` | adapter name — see the warning below |
| `TICK_SECONDS` | `3` | loop interval |
| `MAX_CONCURRENT` | `4` | cap on tasks running at once — your spend ceiling |
| `REPLY_MIN_INTERVAL` | `2` | per-chat auto-reply throttle |
| `RETENTION_SECONDS` | `604800` | prune terminal records older than this |
| `STALE_AFTER` | `600` | recover `running` records older than this |
| `REVIEW_KEYWORDS` | a risky-verb list | empty string disables the HITL heuristic |
| `OWNER_CHAT_ID` | `OWNER_ID` | chat for unprompted messages |
| `NOTIFY_TOOL` | `1` | `0` withholds `notify_owner` |
| `CALENDAR_FILE` | — | TOML calendar to run |
| `CALENDAR_TOOLS` | `0` | `1` lets the agent manage its own schedules |
| `CALENDAR_MIN_INTERVAL` | `300` | floor on agent-created cadences, seconds |
| `CALENDAR_MAX_SCHEDULES` | `20` | cap on agent-created schedules |

!!! warning "`BOT_ID` and `ADAPTER_NAME` are stored identities, not labels"
    `BOT_ID` keys the Telegram update watermark (`pulse:telegram:offset:{bot}`):
    change it on an existing deployment and the offset resets, so the bot
    re-reads updates it had already handled.

    `ADAPTER_NAME` is written onto every task as its `source`, and reply routing
    looks it up by exact name. Change it and any task already in the Store
    completes with nowhere to send its answer — silently, because a missing
    responder is not an error. Both must keep whatever value the Store already
    holds; `ADAPTER_NAME` defaults to `telegram` for exactly that reason.

## Extending it

For extra tools or a different engine, import `serve` rather than copying the
module — you keep every later fix to the launcher:

```python
from lazypulse.launcher import serve

serve(tools=my_tools())          # engine=... to override the model too
```

`deploy/tg-bot/bot.py` in the repository is exactly this: a thin wrapper that
builds LazyCrawler and registry tools and hands them to `serve`, plus a
Dockerfile for Railway / Render / Coolify.
