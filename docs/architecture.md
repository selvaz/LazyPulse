# Architecture

LazyPulse adds exactly one new idea to lazybridge: **an agent that wakes
itself up.** Everything else is reused.

## The one rule: PulseAgent is an Agent

`PulseAgent` subclasses `lazybridge.Agent`. It does not wrap, proxy, or
re-implement it. The constructor takes the new kwargs (`adapters`, `policy`,
`tick_seconds`, `max_concurrent_inbound`, `stale_after`, `terminal_retention`,
`clock`) and forwards everything
else to `super().__init__(**agent_kwargs)`. So a PulseAgent has the full
Agent surface — `engine`, `tools`, `guard`, `verify`, `memory`, `store`,
`session`, `output`, `sources`, `cache`, `fallback` — and every Agent
improvement propagates for free.

The tick loop dispatches each authorized message through the **ordinary
`self.run(text)`** path. It never touches Agent internals and never overrides
`run`. That is why a `Plan` engine, a `HumanEngine`, or a custom engine all
work with no special-casing.

## LazyBridge → LazyPulse mapping

| Need | LazyBridge primitive | LazyPulse uses it for |
|---|---|---|
| Decide what to do | `Agent` + any `Engine` | The worker. PulseAgent *is* the Agent. |
| Deterministic routing | `Plan` / `Step` / `routes_by` | Triage-then-specialist pipelines (see `plan_engine.md`). |
| Shared, atomic state | `Store` (+ `compare_and_swap`) | Task lifecycle ledger; CAS claims; idempotency markers; review channel. |
| Observability | `Session.emit` | `pulse.tick`, `pulse.tick_error`, `pulse.adapter_error` events. |
| Human approval | `HumanEngine(ui=…)` | `StoreReviewerUI` routes the prompt through the Store. |
| Worker capabilities | `ToolProvider` | `GmailTools` (draft/send) — in `lazytools.connectors.gmail`. |
| Output typing | `output=Model` | Structured triage for routing. |

## Components (all new code)

```
PulseAgent(Agent)        pulse_agent.py   tick loop + start/stop/running
PulsePolicy + enums      policy.py        pre-execution authorization
Adapter (Protocol)       adapters/base.py inbound message source contract
InboundMessage           models.py        what an adapter produces
PulseRecord              models.py        per-task lifecycle ledger (Store)
TickReport               models.py        per-tick summary / event payload
StoreReviewerUI          review.py        Store-backed HumanEngine UI
WebhookAdapter           adapters/webhook HTTP intake (extra: webhook)
GmailPushInbox           adapters/gmail   push notifications + history sync —
                                          the default Gmail intake (extras: gmail, webhook)
GmailInbox / Policy      adapters/gmail   polling fallback + auth classification
                                          (send tools: lazytools.connectors.gmail, extra: gmail)
```

## One tick, start to finish

`PulseAgent.tick_once()` does, in order:

1. **Recover** — scan the Store for `running` records older than the
   staleness threshold; CAS them back to `scheduled` (incrementing
   `restart_count`, or failing them past the cap).
2. **Intake** — `drain()` every adapter; for each message, dedupe on
   `message_id`, run the policy (`classify` → `authorize`), and write a
   `PulseRecord` whose initial status reflects the decision
   (`scheduled` / `awaiting_review` / `rejected`).
3. **Execute** — collect `scheduled` records whose `run_at <= now`; for each,
   CAS `scheduled → running` (losers skip — that is the multi-agent guard),
   `await self.run(text)` under a concurrency semaphore, and write the
   terminal record (`completed` / `failed` / `rejected`).
4. **Emit** — a `pulse.tick` event with the `TickReport`.

The background loop (`start()`) just calls `tick_once()` every
`tick_seconds`, swallowing per-tick exceptions so the loop outlives bad ticks.

## Bounding the ledger — `terminal_retention`

Every task leaves a `PulseRecord` in the Store, and terminal records
(`completed` / `rejected` / `failed`) are retained forever by default. The
per-tick recovery, collection, and prune steps scan the task records via an
indexed `Store.items(prefix="pulse:task:")` range scan (lazybridge ≥ 0.9.1) —
O(M) in the number of task records, not O(N) over the whole keyspace — and fall
back to a full `keys()` walk on older stores. Either way they walk *all* task
records regardless of status, so for an **always-on** agent the unbounded ledger
(an ever-growing M) is the main scaling cliff over time.

`terminal_retention=<seconds>` makes the prune step delete finished records once
they age out, keeping the ledger bounded:

```python
PulseAgent(store=Store(db="pulse.db"), terminal_retention=7 * 24 * 3600, ...)
```

**Set `terminal_retention` in production.** Pick a window long enough for the
auditing/observability you need but short enough to keep the ledger bounded.
`None` (the default) keeps the full history and is fine for tests and
short-lived runs. Making the scans proportional to *due* work rather than to all
task records — a status-indexed key scheme (e.g. `pulse:task:scheduled:*`) — is
a documented follow-up.

## Why a policy is not a Guard, and an adapter is not a tool

- A lazybridge **Guard** inspects text entering/leaving an engine. A
  **policy** decides whether a message is even allowed to reach an engine —
  different lifecycle, and its outcomes include "queue for human review",
  which a Guard has no concept of.
- A **tool** is a capability the worker invokes mid-run. An **adapter**
  injects work into the loop from outside. Opposite directions.

Keeping them as distinct objects is what lets the matrix in `security.md`
stay readable and auditable.
