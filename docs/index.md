# LazyPulse

**Give an LLM agent a heartbeat.** LazyPulse turns a one-shot agent into an
always-on one: it watches an inbox / webhook / queue, decides *who is allowed to
ask it for what*, runs the work in the background, and pauses for your approval
before anything risky (like sending an email) actually happens.

!!! info "Part of the LazyBridge ecosystem"
    A `PulseAgent` **is** a `lazybridge.Agent` with three additions — a tick
    loop, a trust policy, and inbound adapters. Capabilities (Gmail/Telegram
    clients + guarded send tools) come from
    [LazyTools](https://tools.lazybridge.com/). See the
    [ecosystem overview](https://lazybridge.com/).

!!! warning "Compliance & liability — your responsibility"
    LazyPulse runs always-on agents that connect to external services (Gmail,
    Telegram, webhooks). **You are solely responsible for ensuring your use
    complies with each provider's terms** — in particular Google's
    [Terms of Service](https://policies.google.com/terms) and the
    [API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)
    for Gmail — and with any applicable laws. Polling inboxes and sending on a
    schedule can get an account rate-limited or suspended. LazyPulse is provided
    **"as is", without warranty, and the authors accept no liability** for how it
    is used (see
    [LICENSE](https://github.com/selvaz/LazyPulse/blob/main/LICENSE)). Use
    least-privilege scopes and obtain the necessary consent before deploying.

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
pip install 'lazypulse[gmail,webhook]'   # Gmail intake — push notifications, the default (pulls lazytoolkit[gmail] + HTTP pieces)
pip install 'lazypulse[telegram]'        # Telegram inbox (pulls lazytoolkit[telegram])
pip install 'lazypulse[webhook]'         # HTTP intake adapter
```

## Watching Gmail: push is the default

Gmail can notify the agent the moment mail arrives (`users.watch` → Cloud
Pub/Sub → the adapter's HTTP endpoint): **zero Gmail API calls while the
mailbox is quiet, one cheap `history.list` per email received** — the
configuration to run when you care about API quota. Polling remains the
zero-setup quick start. See [Gmail (push & polling)](gmail.md).

## How it relates to the other packages

- **lazybridge** — `PulseAgent` subclasses `Agent`, so the full Agent surface
  (engine, tools, guard, verify, memory, store, session) works unchanged.
- **lazytools** — the Gmail/Telegram **clients and guarded send tools** live in
  `lazytools.connectors.*`; the matching **inbound adapters** (inbox + trust
  policy) live here in LazyPulse. Installing `lazypulse[gmail]` pulls
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
