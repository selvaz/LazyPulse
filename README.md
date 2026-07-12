# LazyPulse

**Give an LLM agent a heartbeat.** LazyPulse turns a one-shot agent into an
always-on one: it watches a **Telegram** chat / inbox / webhook, decides *who is
allowed to ask it for what*, runs the work in the background, and pauses for
your approval — right in the chat — before anything risky actually happens.

**Start with Telegram.** It is the simplest and safest channel to run: the
sender id is authenticated by Telegram's servers and *cannot be spoofed*, so the
trust policy keys on `owner_ids=[...]` directly — no DKIM/DMARC to parse, no
mailbox to hand over, and the bot is **two-way out of the box** (message it, get
the agent's answer back). Gmail, Outlook, and generic webhooks are there when
you need them.

It's built on [lazybridge](https://github.com/selvaz/LazyBridge): a
`PulseAgent` is a normal `lazybridge.Agent` with three additions — a **tick
loop**, a **trust policy**, and **inbound adapters**.

```
   inbound message            PulsePolicy                 your Agent
  ┌──────────────┐   drain   ┌────────────┐   allow?   ┌────────────┐
  │ Telegram     │ ────────> │ who sent    │ ────────> │ engine +   │
  │ Gmail        │           │ this? what  │  review?  │ tools +    │
  │ Webhook      │           │ may they    │  reject?  │ verify     │
  │ your adapter │           │ ask for?    │           └────────────┘
  └──────────────┘           └────────────┘            lifecycle in Store
        every tick_seconds     approve in Telegram ↩
```

---

> [!IMPORTANT]
> **Compliance & liability — your responsibility.** LazyPulse connects to external
> services (Gmail, Telegram, webhooks). You are solely responsible for ensuring
> your use complies with each provider's Terms of Service — in particular
> [Google's Terms of Service](https://policies.google.com/terms) and the
> [API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)
> for Gmail — and with applicable laws. Polling and scheduled sending can get an
> account rate-limited or suspended. Provided "as is", without warranty; the
> authors accept no liability for how it is used (see [LICENSE](LICENSE)).

## Install

LazyPulse is distributed from GitHub at a release tag (only LazyBridge is on
PyPI). Add an extra to the direct reference:

```bash
G="git+https://github.com/selvaz/LazyPulse.git@v0.3.1"
pip install "lazypulse @ $G"                # core tick loop + policy
pip install "lazypulse[telegram] @ $G"      # + Telegram inbox & send  ← the recommended start
pip install "lazypulse[gmail,webhook] @ $G" # + Gmail intake (push notifications — the default; webhook pulls the HTTP pieces)
pip install "lazypulse[webhook] @ $G"       # + HTTP intake
pip install "lazypulse[outlook] @ $G"       # + local Outlook desktop inbox (Windows, via LazyTools)
pip install "lazypulse[cron] @ $G"          # + cron-expression scheduling
pip install "lazypulse[dev] @ $G"           # test + lint toolchain
```

A bare `lazypulse` core does **not** pull the Google libraries or
starlette — those come only with the extras. The `[gmail]` / `[telegram]`
extras pull `lazytoolkit[...]`: the inbound adapters (inbox + trust policy)
live in LazyPulse, while the matching clients and guarded send tools
(`GmailClient`, `GmailTools`, …) live in `lazytools.connectors.*`.

You'll also need an API key for whatever model your agent uses (e.g.
`ANTHROPIC_API_KEY`), exactly as with lazybridge.

---

## 30-second example

This runs as-is — no API key, no network — because it uses the bundled mocks.
Swap `MockEngine` for `LLMEngine("claude-opus-4-8")` and `MockAdapter` for a
real one to go live.

```python
import time
from datetime import datetime, timezone

from lazybridge import Store
from lazypulse import PulseAgent, InboundMessage, PulseRecord, store_keys
from lazypulse.testing import MockEngine, MockAdapter

store = Store()                       # task lifecycle lives here
pulse = PulseAgent(
    name="assistant",
    engine=MockEngine(["Summary: 3 unread, all low priority."]),
    store=store,
    adapters=[MockAdapter([
        InboundMessage(source="demo", message_id="1",
                       received_at=datetime.now(timezone.utc),
                       text="summarise my unread mail"),
    ])],
    unsafe_allow_all=True,            # dev only: run inbound without a policy
    tick_seconds=0.05,
)

with pulse.running():                 # background loop; stops on block exit
    time.sleep(0.3)

for key in list(store.keys()):
    if key.startswith(store_keys.TASK_PREFIX):
        rec = PulseRecord.model_validate(store.read(key))
        print(rec.status, "→", rec.worker_text)
# completed → Summary: 3 unread, all low priority.
```

The loop drained the message, ran your agent, and recorded the result in the
Store. `unsafe_allow_all=True` is the local-dev shortcut — in production you
pass a `policy=` instead, and a `PulseAgent` with adapters but neither will
**refuse to start** (so untrusted mail can't run with full trust by accident).

---

## A real agent: a Telegram assistant that asks before acting

This is the recommended way to run LazyPulse — a personal, always-on Telegram
bot that only *you* can drive, and that pauses for your approval, **in the
chat**, before doing anything you've flagged as risky. There is a ready-to-ship
version (DeepSeek + optional web tools, one-command Railway/Render deploy) under
[`deploy/tg-bot/`](deploy/tg-bot/); the core wiring is:

```python
from lazybridge import LLMEngine, Session, Store
from lazypulse import PulseAgent
from lazypulse.adapters.telegram import (
    TelegramInbox, TelegramInboxConfig, TelegramPolicy, TelegramReviewer,
)
from lazypulse.models import ActionClass
from lazytools.connectors.telegram import TelegramClient  # pip install "lazypulse[telegram] @ git+https://github.com/selvaz/LazyPulse.git@v0.3.1"

OWNER_ID = 123456789                       # your Telegram user id (message.from.id)
client = TelegramClient.from_token("123:AA...")
store = Store(db="pulse.db")               # persistent: survives restarts

# Human-in-the-loop over the same bot: a parked task pings you in Telegram; you
# reply /approve <id> or /reject <id>. handle_command intercepts those replies.
reviewer = TelegramReviewer(client, store, owner_id=OWNER_ID)

# Decide what needs approval. Here: any message that asks to send/delete/pay is
# re-labelled EXTERNAL_SEND, so it parks for confirmation instead of auto-running.
def needs_review(msg):
    risky = ("send", "delete", "pay", "invia", "cancella", "paga")
    return ActionClass.EXTERNAL_SEND if any(k in msg.text.lower() for k in risky) else ActionClass.READ_PUBLIC

pulse = PulseAgent(
    name="assistant",
    engine=LLMEngine("claude-opus-4-8", system="You are a concise personal assistant."),
    store=store,
    session=Session(),                              # observability
    policy=TelegramPolicy(owner_ids=[OWNER_ID]),    # only the verified owner acts
    adapters=[TelegramInbox(client, TelegramInboxConfig(bot_id="assistant"))],
    action_classifier=needs_review,                 # risky intent → review
    command_filter=reviewer.handle_command,         # owner /approve · /reject
    terminal_retention=7 * 24 * 3600,               # keep the Store bounded (always-on)
    tick_seconds=3.0,
)

with pulse.running():                    # background loop
    import asyncio
    async def notify_loop():
        while pulse.is_running():
            await reviewer.notify_pending()   # ping the owner about parked tasks
            await asyncio.sleep(3.0)
    asyncio.run(notify_loop())
```

What happens each tick:

1. `TelegramInbox` polls for new messages and emits one per message, carrying
   the **server-verified** sender id (`message.from.id`) — nothing to forge.
2. `TelegramPolicy` classifies: the `OWNER_ID` is `OWNER_VERIFIED`; a bot or any
   other user is `UNKNOWN` → **rejected** before the model sees the text.
3. `action_classifier` labels intent. A benign question runs and the answer is
   sent straight back to your chat (two-way, no tool wiring). A risky one parks
   in `awaiting_review`.
4. `reviewer.notify_pending()` messages you: *"🔔 approve? `/approve <id>` /
   `/reject <id>`"*. Your reply is caught by `command_filter` (consumed as a
   command, not run as a task) and applied via compare-and-swap. Approve → it
   runs on the next tick; reject → it's dropped.

> Because the sender id can't be spoofed and only the owner is trusted, the
> attack surface is small by construction. Add `allowed_user_ids=[...]` to let
> named others in (as `EXTERNAL_VERIFIED`) — and gate any tool that sends,
> pays, deletes, or runs code, since the action label expresses *intent*, not a
> tool sandbox (see [what the policy does and doesn't gate](#a-note-on-what-the-policy-does-and-doesnt-gate)).

**Load & stress test it offline** (no token, no network) with
[`deploy/tg-bot/loadtest.py`](deploy/tg-bot/loadtest.py): it drives the real
inbox + agent + reviewer through a fake client and checks throughput, dedup, the
HITL approve/reject flow, crash-recovery (no double-execution), and retention.

### Also: watching Gmail (push or polling)

The same shape works for email. **The default way to watch Gmail is push
notifications, not polling** (`users.watch` + Cloud Pub/Sub → **zero** Gmail API
calls while quiet, one cheap `history.list` per arrival) — see
[`examples/04_gmail_push.py`](examples/04_gmail_push.py) for the ~10-min Pub/Sub
setup. The polling `GmailInbox` is the zero-setup quick start:

```python
from lazypulse.adapters.gmail import GmailInbox, GmailInboxConfig, GmailPolicy
from lazytools.connectors.gmail import GmailClient, GmailTools  # pip install "lazypulse[gmail] @ git+https://github.com/selvaz/LazyPulse.git@v0.3.1"

OWNER = "you@example.com"
client = GmailClient.from_credentials(
    credentials_path="credentials.json", token_path="token.json",
    scopes=["https://www.googleapis.com/auth/gmail.metadata"],
)
pulse = PulseAgent(
    name="inbox-assistant",
    engine=LLMEngine("claude-opus-4-8", system="You triage and draft email replies."),
    tools=[GmailTools(client, allowed_recipients=[OWNER])],   # draft freely; send is gated
    store=Store(db="pulse.db"),
    policy=GmailPolicy(owner_emails=[OWNER]),                 # only verified owner mail acts
    adapters=[GmailInbox(client, GmailInboxConfig(account=OWNER, query="is:unread"))],
    tick_seconds=15.0,
)
pulse.serve()
```

Here `GmailPolicy` classifies the sender from its DKIM/SPF/DMARC result: owner
mail that passes DKIM + DMARC is `OWNER_VERIFIED_EMAIL`, a spoof or stranger is
not. `GmailTools.gmail_create_draft` works freely; `gmail_send` stays blocked
until a **one-shot**, recipient-bound, task-bound confirmation
(`tools.confirm_send(to=addr, task_id=rec.task_id)`) — typically granted right
after you `approve_task(...)`. Unlike Telegram, email identity rests on a
conservative MVP parser of Gmail's own header, so it takes more care to run
safely; that is the other reason Telegram is the recommended starting point.

---

## Core concepts

### PulseAgent

A subclass of `lazybridge.Agent`, so it takes **every** Agent argument
(`engine`, `tools`, `guard`, `verify`, `memory`, `output`, `fallback`, …)
plus these:

| Argument | Default | What it does |
|---|---|---|
| `store=` | **required** | Where task lifecycle is kept. `Store()` (memory) or `Store(db="…")` (persistent). |
| `adapters=` | `[]` | List of inbound sources (`Adapter`s). |
| `policy=` | `None` | Trust + authorization. **Required when `adapters=` is set** (or pass `unsafe_allow_all=True`). |
| `unsafe_allow_all=` | `False` | Opt out of requiring a policy — runs all inbound with full trust. Local dev only. |
| `tick_seconds=` | `1.0` | How often the loop wakes up. |
| `max_concurrent_inbound=` | `4` | Cap on tasks running at once. |
| `retry_policy=` | `None` | `RetryPolicy(...)` — auto-retry failed tasks with exponential backoff. Configurable `max_attempts`, `backoff_base`, `backoff_max`, and a `retry_on` exception filter (see **Retries and cron** below). |
| `stale_after=` | `max(tick*60, 3600)` | A `running` task older than this is treated as crashed and retried. Default is 1 h so slow LLM workers and tasks parked in human review (default review timeout 3600 s) are never falsely recovered. Tune down for fast pure-LLM agents. |
| `terminal_retention=` | `None` | When set (seconds), terminal task records (`completed`/`rejected`/`failed`) older than this are pruned during ticks so an always-on agent's Store does not grow without bound. `None` keeps the full ledger forever. **Set this in production** (see below). |
| `clock=` | UTC now | Inject a clock for deterministic tests. |

Lifecycle control — all synchronous; the event loop is hidden in a background
thread, so your code stays plain like a lazybridge `agent("task")` call:

```python
pulse.start()            # launch the loop in a background thread (non-blocking)
pulse.is_running()       # -> bool
pulse.stop()             # stop & join the thread (safe to call twice)

with pulse.running():    # start + guaranteed stop
    ...

pulse.serve()            # start and block until Ctrl-C — one-liner for a daemon
report = pulse.tick()    # run exactly one beat synchronously (cron / scripts)
```

For embedding inside an event loop you already run (FastAPI, etc.), the async
primitive `await pulse.tick_once()` is still there.

### Scheduling — the timer side

Besides reacting to adapters, you can enqueue trusted work yourself. These
bypass the policy (your own code is trusted) and run on the next tick where
`run_at <= now`:

```python
pulse.schedule("post the daily standup summary")            # now
pulse.schedule_after("retry the export", seconds=300)       # in 5 min
pulse.schedule_at("send the weekly report", when=monday_9am)
pulse.schedule_cron("send the weekly report", "0 9 * * 1")  # every Mon 09:00
```

`schedule_cron(text, cron, tz="UTC")` registers a **recurring** task from a
5-field cron expression and returns a `cron_id`; the tick loop fires it on
schedule and advances the next fire time atomically. It needs the `cron` extra
(`pip install "lazypulse[cron] @ git+https://github.com/selvaz/LazyPulse.git@v0.3.1"`). The other three `schedule_*` calls are
one-shots.

A `schedule`-only agent (no adapters) needs no policy. Combine with a small
`tick_seconds` for reactive work, or a large one for cron-like jobs.

### Retries and cron — the resilient side

Tasks fail — an LLM call times out, a tool 500s. Pass a `RetryPolicy` to retry
them automatically with exponential backoff instead of marking them `failed` on
the first error:

```python
from lazypulse import PulseAgent, RetryPolicy

pulse = PulseAgent(
    store=Store(db="pulse.db"),
    retry_policy=RetryPolicy(
        max_attempts=4,        # total attempts before giving up (default 1 = no retry)
        backoff_base=2.0,      # delay = min(base ** attempt, backoff_max) seconds
        backoff_max=300.0,
        retry_on=(Exception,), # only retry these exception types
    ),
    # engine=, adapters=, policy=, … — the usual PulseAgent args
)
```

A retried task is re-scheduled with its `next_retry_at` set; the tick loop picks
it up when due, and `attempt` is tracked on the `PulseRecord`.

### Bounding Store growth in production — `terminal_retention`

Every task leaves a `PulseRecord` in the Store, and terminal records
(`completed`/`rejected`/`failed`) are kept forever by default. For an always-on
agent that means the ledger grows without bound, and because the per-tick scans
(`_collect_due`, `_recover_stale`, recovery, pruning) walk every task record,
that growth eventually shows up as per-tick cost.

Set `terminal_retention=` to an age (in seconds) so finished records are pruned
during ticks once they age out:

```python
PulseAgent(
    store=Store(db="pulse.db"),
    terminal_retention=7 * 24 * 3600,   # keep a week of history, then prune
    ...
)
```

**Recommendation:** always set `terminal_retention` in production. Choose a
window long enough for whatever auditing/observability you need (e.g. a few days
to a week), but short enough to keep the ledger bounded. `None` (the default)
preserves the full historical ledger and is fine for tests and short-lived runs.

For a one-off trim outside the tick loop (a migration, or a manual cleanup), call
`purge_terminal_tasks(store, older_than=timedelta(days=7))` directly — it deletes
terminal records older than the given age and returns how many it removed.

> Note: per-tick task lookups use an indexed `Store.items(prefix=)` range scan
> (lazybridge ≥ 0.9.1), so they are O(M) in the number of task records — not the
> whole keyspace. They still walk *all* task records regardless of status, so
> `terminal_retention` (which bounds M) remains the primary lever for keeping an
> always-on agent healthy; a status-indexed key scheme to make scans
> proportional to *due* work is a documented follow-up.

### PulsePolicy — who may ask for what

Authorization happens **before** your agent runs and is based on the
*sender*, never the message text (so prompt injection can't talk its way in).
Two steps:

```python
policy.classify(inbound) -> Identity          # resolve sender → TrustLevel
policy.authorize(identity, action) -> PolicyDecision   # allow / review / reject
```

The default trust → allowed-actions matrix:

| TrustLevel | May do |
|---|---|
| `UNKNOWN`, `OWNER_CLAIM_UNVERIFIED` | nothing → **rejected** |
| `EXTERNAL_VERIFIED` | `READ_PUBLIC` (more → **queued for review**) |
| `OWNER_VERIFIED_EMAIL` | read + `WRITE_LOCAL` (send/destructive → **needs confirmation**) |
| `APPROVED_SESSION` | the above + `EXTERNAL_SEND` |
| `SYSTEM` | everything |

Write your own by subclassing and overriding `classify`:

```python
from lazypulse import PulsePolicy, Identity, TrustLevel

class SlackPolicy(PulsePolicy):
    def classify(self, inbound):
        if inbound.sender_raw in self.owner_emails:
            return Identity(sender=inbound.sender_raw, trust=TrustLevel.OWNER_VERIFIED_EMAIL)
        return Identity(sender=inbound.sender_raw, trust=TrustLevel.UNKNOWN)
```

Tighten the matrix per deployment with `action_rules=`. See
[`docs/security.md`](docs/security.md) for the full threat model.

**Per-sender rate limiting.** Pass a `RateLimit` to the policy to cap how many
messages a single sender may submit per time window — a coarse abuse guard
applied at intake, before the agent runs:

```python
from lazypulse import PulsePolicy, RateLimit

policy = PulsePolicy(
    rate_limit=RateLimit(
        max_per_sender=10,     # messages allowed per window, per sender
        window_seconds=3600,   # fixed (tumbling) window
        on_exceeded="reject",  # "reject" drops the message; "queue" routes it to human review
    ),
)
```

The window is fixed, not sliding, so a burst straddling a boundary can admit up
to `2 * max_per_sender` across the two adjacent windows — fine for coarse abuse
limiting. Over-limit messages set `rate_limited` on the `PulseRecord`.

### Adapters — where work comes from

An adapter is any object with a `name` and an async `drain()` that returns new
`InboundMessage`s:

```python
from lazypulse import InboundMessage

class QueueAdapter:
    name = "myqueue"
    async def drain(self, *, store, session):
        rows = my_queue.pop_all()
        return [InboundMessage(source=self.name, message_id=r.id,
                               received_at=r.ts, text=r.body) for r in rows]
```

An adapter is **at-least-once**: dedupe is central, on `message_id`, so it's
fine (preferable, even) to re-emit a message until LazyPulse has durably
recorded it — that's what makes a crash between drain and record-write safe.
A message still becomes at most one task. Built-in adapters: `WebhookAdapter`,
`GmailPushInbox` (the default for Gmail), `GmailInbox` (polling fallback),
`TelegramInbox`, and `OutlookInbox` (a local Outlook desktop inbox on Windows,
with `OutlookInboxConfig` / `OutlookPolicy`; needs the `[outlook]` extra).

Chat platforms make the policy *simpler and stronger* than email:
`TelegramInbox` carries the platform-authenticated sender id, which can't be
spoofed, so `TelegramPolicy` keys on `owner_ids=[...]` directly — no
DKIM/DMARC parsing. A bot or a stranger is rejected before the worker runs.

### Talking back — conversational adapters

An adapter can also implement `reply()` (the `Responder` protocol). When a
task completes, the PulseAgent sends the worker's output straight back to the
conversation it came from — so `TelegramInbox` is a **two-way** channel out of
the box: message the bot, get the agent's answer back, no tool wiring. Because
the reply goes to the *already-authorized* sender, it needs no confirmation;
sending to a *new* recipient still goes through a gated tool (`TelegramTools`,
`GmailTools`). Turn auto-reply off with `TelegramInboxConfig(reply_with_output=False)`.

### PulseRecord — the task ledger

Every message becomes one `PulseRecord` in the Store, moving through
`scheduled → running → completed | failed | rejected | awaiting_review`.
Read them back to build a dashboard, retry failures, or drive a UI:

```python
for key in store.keys():
    if key.startswith(store_keys.TASK_PREFIX):
        rec = PulseRecord.model_validate(store.read(key))
        print(rec.task_id, rec.status, rec.action_class, rec.cost_usd, rec.error)
```

### Human review from anywhere

**Over Telegram (recommended).** `TelegramReviewer` closes the loop through the
bot you already talk to: it messages the owner about each parked task and turns
the owner's `/approve <id>` / `/reject <id>` reply into the decision — wire
`command_filter=reviewer.handle_command` and call `reviewer.notify_pending()`
each tick (see the assistant example above). Only the owner's server-verified id
may approve. It uses the pre-run review queue (the task is `awaiting_review`, not
`running`), so it is never touched by the crash-recovery clock.

For tasks that need a human mid-run (a worker's `verify` step), `StoreReviewerUI`
parks the question in the Store instead of a terminal — so a phone, a CLI, or a
Slack bot on the same Store can answer it:

```python
from lazybridge import Agent
from lazybridge.ext.hil import HumanEngine
from lazypulse import StoreReviewerUI, pending_reviews, respond

# Worker side: route approval through the Store.
reviewer = Agent(name="approver", engine=HumanEngine(ui=StoreReviewerUI(store)))

# Reviewer side (another process / the included example CLI):
for req in pending_reviews(store):
    print(req["task"])
    respond(store, req["review_id"], "approved")
```

Separately, tasks the **policy** parked (`awaiting_review` — e.g. an owner's
external send, or a stranger queued for review) are closed out with the task
queue API. Nothing runs them until you approve:

```python
from lazypulse import pending_tasks, approve_task, reject_task

for rec in pending_tasks(store):
    print(rec.task_id, rec.action_class, rec.text)
    approve_task(store, rec.task_id)            # → scheduled, runs next tick
    # or: reject_task(store, rec.task_id, "not appropriate")
```

Both are compare-and-swap, so two reviewers on one Store can't double-act.

### A note on what the policy does and doesn't gate

The policy authorizes **inbound execution** — whether a message reaches the
worker at all. It does **not** sandbox the tools your agent then calls. If the
worker has a tool that sends, pays, deletes, or runs code, guard that tool
itself (as `GmailTools` gates `gmail_send`) or wrap it — the policy won't stop a
tool call mid-run. The `action_class`/`action_classifier` express *intent* (and
route risky intent to review); they do not automatically constrain tool calls.

**Telegram** gives the strongest identity signal: `message.from.id` is verified
by Telegram's servers and cannot be spoofed, so `TelegramPolicy` needs no
parsing and a stranger is rejected outright. **Gmail** is weaker: `GmailPolicy`'s
DKIM/SPF/DMARC handling is a conservative MVP parser of Gmail's own
`Authentication-Results` header — sound for "is this really my owner address",
but if you need rigorous multi-hop verification, validate upstream. This is why
Telegram is the recommended channel to start with.

---

## Observability

Pass a `Session` and LazyPulse emits events you can export
(`pulse.tick`, `pulse.tick_error`, `pulse.adapter_error`, `pulse.write_conflict`).
Quiet ticks don't emit, so a long-running agent's event log stays signal.

---

## Examples

Runnable files in [`examples/`](examples/) (01, 05, 06 need no credentials):

| | |
|---|---|
| `01_minimal_pulse.py` | the 30-second example above |
| `02_webhook_intake.py` | HTTP intake + `curl` recipes |
| `03_gmail_polling.py` | full Gmail setup |
| `04_store_review_thin_client.py` | a reviewer CLI |
| `05_plan_routing_deterministico.py` | route by category with a `Plan` engine |
| `06_multi_pulse_shared_store.py` | two agents, one Store, no double-runs |
| `07_telegram_polling.py` | watch a Telegram bot, reply only to the owner |
| `08_outlook_local.py` | watch a local Outlook desktop inbox (Windows) |

## Docs

- [`docs/security.md`](docs/security.md) — threat model & the trust matrix
- [`docs/plan_engine.md`](docs/plan_engine.md) — deterministic routing with `Plan`
- [`docs/architecture.md`](docs/architecture.md) — how it maps onto lazybridge (for contributors)

## License

Apache-2.0

---

## How This Was Built

LazyBridge is designed by **selvaz** with **Claude Code** and
**ChatGPT Codex** as primary implementation partners.
I focus on architecture, mental model, and trade-offs —
they handle the building under my direction.
