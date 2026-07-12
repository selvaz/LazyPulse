# LazyPulse

**Give an LLM agent a heartbeat.** LazyPulse turns a one-shot agent into an
always-on one: it watches a **Telegram** chat / inbox / webhook, decides *who is
allowed to ask it for what*, runs the work in the background, and pauses for your
approval — right in the chat — before anything risky actually happens.

!!! tip "Start with Telegram"
    It is the simplest and safest channel: the sender id is authenticated by
    Telegram's servers and **cannot be spoofed**, so the policy keys on
    `owner_ids=[...]` directly — no DKIM/DMARC to parse, no mailbox to hand over,
    and the bot is **two-way out of the box**. See [Telegram](telegram.md). Gmail,
    Outlook, and webhooks are there when you need them.

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
  │ Telegram     │ ────────> │ who sent    │ ────────> │ engine +   │
  │ Gmail        │           │ this? what  │  review?  │ tools +    │
  │ Webhook      │           │ may they    │  reject?  │ verify     │
  │ your adapter │           │ ask for?    │           └────────────┘
  └──────────────┘           └────────────┘            lifecycle in Store
        every tick_seconds     approve in Telegram ↩
```

## Install

```bash
pip install "lazypulse @ git+https://github.com/selvaz/LazyPulse.git"                    # core tick loop + policy
pip install "lazypulse[telegram] @ git+https://github.com/selvaz/LazyPulse.git"        # Telegram inbox & send  ← recommended start
pip install "lazypulse[gmail,webhook] @ git+https://github.com/selvaz/LazyPulse.git"   # Gmail intake — push notifications, the default
pip install "lazypulse[webhook] @ git+https://github.com/selvaz/LazyPulse.git"         # HTTP intake adapter
```

## Start with Telegram

A personal, always-on Telegram bot that only *you* can drive and that asks for
approval **in the chat** before doing anything risky is the recommended way to
run LazyPulse. The sender id is server-verified and unspoofable, so the trust
policy is a one-liner (`TelegramPolicy(owner_ids=[...])`) and the bot is two-way
out of the box. `TelegramReviewer` routes approvals through the same chat. See
[Telegram](telegram.md) and the ready-to-deploy
[`deploy/tg-bot/`](https://github.com/selvaz/LazyPulse/tree/main/deploy/tg-bot).

## Watching Gmail: push is the default

For email, Gmail can notify the agent the moment mail arrives (`users.watch` →
Cloud Pub/Sub → the adapter's HTTP endpoint): **zero Gmail API calls while the
mailbox is quiet, one cheap `history.list` per email received**. Polling remains
the zero-setup quick start. Email identity rests on a conservative MVP parser of
Gmail's `Authentication-Results` header, so it takes more care than Telegram —
see [Gmail (push & polling)](gmail.md).

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

- [Telegram](telegram.md) — the recommended channel: inbox, owner-only policy,
  and human-in-the-loop approval (`TelegramReviewer`) over the same bot.
- [Architecture](architecture.md) — the one rule (`PulseAgent` is an `Agent`)
  and the LazyBridge → LazyPulse mapping.
- [Plan as engine](plan_engine.md) — deterministic triage-then-specialist
  routing with `lazybridge.Plan`.
- [Security & threat model](security.md) — trust levels, the policy gate, and
  the one-shot send confirmation.
