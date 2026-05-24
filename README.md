# LazyPulse

**Give an LLM agent a heartbeat.** LazyPulse turns a one-shot agent into an
always-on one: it watches an inbox / webhook / queue, decides *who is allowed
to ask it for what*, runs the work in the background, and pauses for your
approval before anything risky — like sending an email — actually happens.

It's built on [lazybridge](https://github.com/selvaz/LazyBridge): a
`PulseAgent` is a normal `lazybridge.Agent` with three additions — a **tick
loop**, a **trust policy**, and **inbound adapters**.

```
   inbound message            PulsePolicy                 your Agent
  ┌──────────────┐   drain   ┌────────────┐   allow?   ┌────────────┐
  │ Gmail        │ ────────> │ who sent    │ ────────> │ engine +   │
  │ Webhook      │           │ this? what  │  review?  │ tools +    │
  │ your adapter │           │ may they    │  reject?  │ verify     │
  └──────────────┘           │ ask for?    │           └────────────┘
        every tick_seconds    └────────────┘            lifecycle in Store
```

---

## Install

```bash
pip install lazypulse                # core
pip install 'lazypulse[webhook]'     # + HTTP intake
pip install 'lazypulse[gmail]'       # + Gmail polling & draft/send
pip install 'lazypulse[telegram]'    # + Telegram polling & send
pip install 'lazypulse[dev]'         # test + lint toolchain
```

A bare `pip install lazypulse` does **not** pull the Google libraries or
starlette — those come only with the extras.

You'll also need an API key for whatever model your agent uses (e.g.
`ANTHROPIC_API_KEY`), exactly as with lazybridge.

---

## 30-second example

This runs as-is — no API key, no network — because it uses the bundled mocks.
Swap `MockEngine` for `LLMEngine("claude-opus-4-7")` and `MockAdapter` for a
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

## A real agent: watch Gmail, draft replies, ask before sending

```python
from lazybridge import LLMEngine, Session, Store
from lazypulse import PulseAgent
from lazypulse.adapters.gmail import (
    GmailClient, GmailInbox, GmailInboxConfig, GmailPolicy, GmailTools,
)

OWNER = "you@example.com"
SCOPES = ["https://www.googleapis.com/auth/gmail.metadata"]

# One-time OAuth: opens a browser, caches token.json (git-ignored).
client = GmailClient.from_credentials(
    credentials_path="credentials.json", token_path="token.json", scopes=SCOPES,
)

pulse = PulseAgent(
    name="inbox-assistant",
    engine=LLMEngine("claude-opus-4-7", system="You triage and draft email replies."),
    tools=[GmailTools(client, allowed_recipients=[OWNER])],   # draft freely; send is gated
    store=Store(db="pulse.db"),         # persistent: survives restarts
    session=Session(),                  # observability
    policy=GmailPolicy(owner_emails=[OWNER]),   # only verified owner mail acts
    adapters=[GmailInbox(client, GmailInboxConfig(account=OWNER, query="is:unread"))],
    tick_seconds=15.0,                  # poll every 15s
)

pulse.serve()   # polls every 15s in the background, blocks until Ctrl-C
```

What happens each tick:

1. `GmailInbox` polls for unread mail and emits one message per email (each
   carrying its DKIM/SPF/DMARC result).
2. `GmailPolicy` classifies the sender. Mail from `OWNER` that passes DKIM +
   DMARC is `OWNER_VERIFIED_EMAIL`; a spoof or stranger is not.
3. The matrix decides: owner mail (default `READ_PUBLIC` intent) → **runs**;
   everyone else → **rejected** before the model ever sees the text. If you
   want the *policy* itself to gate risky actions before the worker runs,
   set `default_action=ActionClass.EXTERNAL_SEND` in `GmailInboxConfig` —
   then owner external-send requests park in `awaiting_review` until
   confirmed. With the default action class the send gate lives at the tool
   layer (step 4), which is equally effective.
4. `GmailTools.gmail_create_draft` works freely; `gmail_send` stays blocked
   until you grant a **one-shot** confirmation — `tools.confirm_send(to=addr)`
   (recipient-bound) or `tools.confirm_once()` — typically right after you
   `approve_task(...)`. Each grant authorizes exactly one send. Add
   `task_id=rec.task_id` to bind the grant to that one task, so under
   concurrent inbound it can't be consumed by a different task's send.

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
| `stale_after=` | `max(tick*60, 300)` | A `running` task older than this is treated as crashed and retried. Raise it for slow workers. |
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
```

A `schedule`-only agent (no adapters) needs no policy. Combine with a small
`tick_seconds` for reactive work, or a large one for cron-like jobs.

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
`GmailInbox`, `TelegramInbox`.

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

For tasks that need a human (`awaiting_review`, or a worker's `verify` step),
`StoreReviewerUI` parks the question in the Store instead of a terminal — so a
phone, a CLI, or a Slack bot on the same Store can answer it:

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
worker has a tool that sends email or runs code, guard that tool itself
(as `GmailTools` gates `gmail_send`) or wrap it — the policy won't stop a tool
call mid-run.

`GmailPolicy`'s DKIM/SPF/DMARC handling is a conservative MVP parser of
Gmail's own `Authentication-Results` header, not a full standalone email
authentication verifier. It's sound for "is this really my owner address",
but if you need rigorous multi-hop verification, validate upstream.

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

## Docs

- [`docs/security.md`](docs/security.md) — threat model & the trust matrix
- [`docs/plan_engine.md`](docs/plan_engine.md) — deterministic routing with `Plan`
- [`docs/architecture.md`](docs/architecture.md) — how it maps onto lazybridge (for contributors)

## License

Apache-2.0
