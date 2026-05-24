# Architecture

LazyPulse adds exactly one new idea to lazybridge: **an agent that wakes
itself up.** Everything else is reused.

## The one rule: PulseAgent is an Agent

`PulseAgent` subclasses `lazybridge.Agent`. It does not wrap, proxy, or
re-implement it. The constructor takes the new kwargs (`adapters`, `policy`,
`tick_seconds`, `max_concurrent_inbound`, `clock`) and forwards everything
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
GmailInbox / Policy      adapters/gmail   polling + auth classification
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

## Why a policy is not a Guard, and an adapter is not a tool

- A lazybridge **Guard** inspects text entering/leaving an engine. A
  **policy** decides whether a message is even allowed to reach an engine —
  different lifecycle, and its outcomes include "queue for human review",
  which a Guard has no concept of.
- A **tool** is a capability the worker invokes mid-run. An **adapter**
  injects work into the loop from outside. Opposite directions.

Keeping them as distinct objects is what lets the matrix in `security.md`
stay readable and auditable.
