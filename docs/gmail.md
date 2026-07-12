# Gmail intake — push notifications (the default) & polling

LazyPulse can watch a Gmail mailbox two ways. **Push notifications are the
default recommendation**: Gmail tells the agent the moment mail arrives, so
the agent never scans the mailbox.

| | **Push — `GmailPushInbox`** (default) | **Polling — `GmailInbox`** (fallback) |
|---|---|---|
| How mail is discovered | Gmail `users.watch` → Cloud Pub/Sub → HTTP push to the adapter | `messages.list` query every tick |
| Gmail API calls at steady state | **zero while quiet**; one `history.list` per email received | every tick, regardless |
| Latency | seconds after arrival | up to one tick interval |
| Setup | ~10 min one-time GCP setup + an HTTPS endpoint | none |
| Use when | always-on daemons, quota-sensitive accounts | quick start, no public endpoint available |

Both adapters emit the same authentication-aware
[`InboundMessage`](architecture.md)s (DKIM/SPF/DMARC parsed, first-wins
header selection), so the [trust policy](security.md) works identically.

## Install

```bash
pip install "lazypulse[gmail,webhook] @ git+https://github.com/selvaz/LazyPulse.git"   # push: gmail client + HTTP pieces
pip install "lazypulse[gmail] @ git+https://github.com/selvaz/LazyPulse.git"           # polling only
```

## Push, end to end

### 1. One-time Google Cloud setup (~10 minutes)

1. Create a Pub/Sub **topic**, e.g. `projects/<project>/topics/gmail-pulse`.
2. On that topic, grant the **Pub/Sub Publisher** role to
   `gmail-api-push@system.gserviceaccount.com` (that's Gmail itself).
3. Create a **push subscription** on the topic whose endpoint is the
   adapter's URL **including the shared token**, e.g.
   `https://your-host/gmail/push?token=<PULSE_PUSH_TOKEN>`.
   The adapter binds `127.0.0.1` — expose it through a TLS reverse proxy
   or a tunnel.

### 2. Run the adapter

```python
import threading

from lazybridge import LLMEngine, Store
from lazytools.connectors.gmail import GmailClient

from lazypulse import PulseAgent
from lazypulse.adapters.gmail import GmailPolicy, GmailPushConfig, GmailPushInbox

client = GmailClient.from_credentials(
    credentials_path="credentials.json", token_path="token.json",
    scopes=["https://www.googleapis.com/auth/gmail.metadata"],
)

inbox = GmailPushInbox(client, GmailPushConfig(
    account="you@gmail.com",
    topic_name="projects/<project>/topics/gmail-pulse",  # watch armed + renewed for you
    shared_token="<PULSE_PUSH_TOKEN>",                   # ?token= auth on the endpoint
))
threading.Thread(target=inbox.serve, daemon=True).start()  # the push endpoint

pulse = PulseAgent(
    name="mail-assistant",
    engine=LLMEngine("claude-opus-4-8"),
    store=Store(db="pulse.db"),            # durable: cursor + ledger survive restarts
    adapters=[inbox],
    policy=GmailPolicy(owner_emails=["you@gmail.com"]),
    tick_seconds=5.0,                      # ticks are cheap — no Gmail call unless notified
)
pulse.serve()
```

Full runnable script:
[`examples/04_gmail_push.py`](https://github.com/selvaz/LazyPulse/blob/main/examples/04_gmail_push.py).

### What the adapter handles for you

- **Watch lifecycle.** With `topic_name=` set, the adapter arms Gmail's
  `users.watch` itself and re-arms it before Google's ≤7-day expiry
  (`renew_margin_seconds`, default 24 h).
- **The notification is only a doorbell.** The HTTP handler verifies the
  `?token=` (constant-time; wrong token → `403`, so a misconfigured
  subscription stays visible in Pub/Sub metrics), flips a flag, and acks.
  Malformed bodies are acked and ignored (no poison-message redelivery);
  notifications for a different account are ignored.
- **One cheap sync per event.** The next tick makes a single
  `users.history.list` call from the cursor persisted in the Store
  (`store_keys.LAST_HISTORY`) and fetches only the new message ids.
- **At-least-once, no skipped mail.** The cursor advances only after every
  id in the previous batch has its `EVENT` marker — a crash between drain
  and record re-emits, never loses. Bursts larger than one batch (100 ids)
  drain over consecutive ticks; the cursor never jumps past unfetched mail.
- **Self-healing.** A cursor older than Gmail's ~1-week history retention
  resyncs forward with a warning; an optional idle resync
  (`idle_resync_seconds`, default 15 min) covers lost push deliveries.
  Adapter errors back off exponentially instead of hammering the API.

!!! warning "Use a durable Store"
    Pass `store=Store(db="pulse.db")`, not the in-memory default — the
    history cursor and the idempotency ledger must survive restarts.

## Polling fallback

No GCP project, no public endpoint — fine at gentle tick rates, and adapter
errors back off exponentially either way:

```python
from lazypulse.adapters.gmail import GmailInbox, GmailInboxConfig

pulse = PulseAgent(
    # ...
    adapters=[GmailInbox(client, GmailInboxConfig(account="you@gmail.com", query="is:unread"))],
    tick_seconds=60.0,   # be gentle: one mailbox scan per tick
)
```

## See also

- [Security](security.md) — the trust policy that decides which senders may
  do what; push and polling feed it identically.
- [Architecture](architecture.md) — where the adapters sit in the tick loop.
- [Gmail client & guarded send tools](https://tools.lazybridge.com/gmail/) —
  `GmailClient` (incl. the `watch`/`history` surface) and the gated
  `gmail_send`, in LazyTools.
