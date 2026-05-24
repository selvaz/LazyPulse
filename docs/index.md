# LazyPulse

**Give an LLM agent a heartbeat.** LazyPulse turns a one-shot agent into an
always-on one: it watches an inbox / webhook / queue, decides *who is allowed to
ask it for what*, runs the work in the background, and pauses for your approval
before anything risky (like sending an email) actually happens.

!!! info "Part of the LazyBridge ecosystem"
    A `PulseAgent` **is** a `lazybridge.Agent` with three additions — a tick
    loop, a trust policy, and inbound adapters. Capabilities (Gmail/Telegram
    clients + guarded send tools) come from
    [LazyTools](https://selvaz.github.io/LazyTools/). See the
    [ecosystem overview](https://lazybridge.com/ecosystem/).

```
   inbound message            PulsePolicy                 your Agent
  ┌──────────────┐   drain   ┌────────────┐   allow?   ┌────────────┐
  │ Gmail        │ ────────> │ who sent    │ ────────> │ engine +   │
  │ Webhook      │           │ this? what  │  review?  │ tools +    │
  │ your adapter │           │ may they    │  reject?  │ verify     │
  └──────────────┘           │ ask for?    │           └────────────┘
        every tick_seconds    └────────────┘            lifecycle in Store
```

## Install

```bash
pip install lazypulse                    # core tick loop + policy
pip install 'lazypulse[gmail]'           # Gmail inbox (pulls lazytoolkit[gmail])
pip install 'lazypulse[telegram]'        # Telegram inbox (pulls lazytoolkit[telegram])
pip install 'lazypulse[webhook]'         # HTTP intake adapter
```

## How it relates to the other packages

- **lazybridge** — `PulseAgent` subclasses `Agent`, so the full Agent surface
  (engine, tools, guard, verify, memory, store, session) works unchanged.
- **lazytools** — the Gmail/Telegram **inbound adapters** (inbox + trust policy)
  live here in LazyPulse; the matching **clients and guarded send tools** live
  in `lazytools.connectors.*`. Installing `lazypulse[gmail]` pulls
  `lazytoolkit[gmail]` for you.

The division of labour: a **Tool** the worker invokes mid-run lives in
`lazytools`; an **inbound adapter / policy** that produces messages and decides
trust lives in `lazypulse`.

## Where to go next

- [Architecture](architecture.md) — the one rule (`PulseAgent` is an `Agent`)
  and the LazyBridge → LazyPulse mapping.
- [Plan as engine](plan_engine.md) — deterministic triage-then-specialist
  routing with `lazybridge.Plan`.
- [Security & threat model](security.md) — trust levels, the policy gate, and
  the one-shot send confirmation.
