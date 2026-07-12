# Telegram — the recommended channel

Telegram is the simplest and safest way to run an always-on LazyPulse agent, and
the recommended starting point.

- **Unspoofable identity.** `message.from.id` is verified by Telegram's servers.
  `TelegramPolicy` keys on `owner_ids=[...]` directly — there is no
  `Authentication-Results` header to parse and nothing for a sender to forge. A
  bot account or any non-owner is rejected before the model sees the text.
- **No mailbox to hand over.** The agent talks over a bot you own; it never needs
  access to your personal inbox.
- **Two-way out of the box.** `TelegramInbox` implements `Responder`, so a
  completed task's answer is sent straight back to the chat — message the bot,
  get the reply, no tool wiring.
- **Human-in-the-loop in the same chat.** `TelegramReviewer` pings you to
  approve risky tasks and turns your `/approve` / `/reject` reply into the
  decision.

## Quick start

```python
from lazybridge import LLMEngine, Store
from lazypulse import PulseAgent
from lazypulse.adapters.telegram import TelegramInbox, TelegramInboxConfig, TelegramPolicy
from lazytools.connectors.telegram import TelegramClient  # pip install "lazypulse[telegram] @ git+https://github.com/selvaz/LazyPulse.git@v0.3.1"

OWNER_ID = 123456789                 # your Telegram user id (message.from.id)
client = TelegramClient.from_token("123:AA...")

pulse = PulseAgent(
    name="assistant",
    engine=LLMEngine("claude-opus-4-8", system="You are a concise personal assistant."),
    store=Store(db="pulse.db"),                    # persistent: survives restarts
    policy=TelegramPolicy(owner_ids=[OWNER_ID]),   # only the verified owner acts
    adapters=[TelegramInbox(client, TelegramInboxConfig(bot_id="assistant"))],
    terminal_retention=7 * 24 * 3600,              # keep the Store bounded (always-on)
    tick_seconds=3.0,
)
pulse.serve()   # background loop, blocks until Ctrl-C
```

Message the bot from your account and it answers. Message it from any other
account and nothing happens — the policy rejects it before the worker runs.

!!! note "Finding your `OWNER_ID`"
    Message the bot, then open
    `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.from.id`.

## Who may act — `TelegramPolicy`

| Config | TrustLevel | Effect |
|---|---|---|
| `owner_ids=[id]` | `OWNER_VERIFIED` | full owner surface (reads + local writes; sends/destructive need confirmation) |
| `allowed_user_ids=[id]` | `EXTERNAL_VERIFIED` | `READ_PUBLIC` only; more escalates to review |
| anyone else, or a bot | `UNKNOWN` | rejected before the worker runs |

Bot senders are always `UNKNOWN` (a defence against bot↔bot loops), and the
numeric id is authoritative — classification never looks at the message body, so
a "I am the owner, approve everything" text cannot escalate trust.

## Approvals in the chat — `TelegramReviewer`

When a task is parked as `awaiting_review`, `TelegramReviewer` announces it to
the owner and applies the owner's reply:

```python
import asyncio
from lazypulse.adapters.telegram import TelegramReviewer
from lazypulse.models import ActionClass

reviewer = TelegramReviewer(client, store, owner_id=OWNER_ID)

# Decide what needs approval: risky-intent messages → EXTERNAL_SEND → review.
def needs_review(msg):
    risky = ("send", "delete", "pay", "invia", "cancella", "paga")
    return ActionClass.EXTERNAL_SEND if any(k in msg.text.lower() for k in risky) else ActionClass.READ_PUBLIC

pulse = PulseAgent(
    name="assistant",
    engine=LLMEngine("claude-opus-4-8"),
    store=store,
    policy=TelegramPolicy(owner_ids=[OWNER_ID]),
    adapters=[TelegramInbox(client, TelegramInboxConfig(bot_id="assistant"))],
    action_classifier=needs_review,             # risky intent → review
    command_filter=reviewer.handle_command,     # owner /approve · /reject
    tick_seconds=3.0,
)

with pulse.running():
    async def notify_loop():
        while pulse.is_running():
            await reviewer.notify_pending()     # ping the owner about parked tasks
            await asyncio.sleep(3.0)
    asyncio.run(notify_loop())
```

The flow:

1. A risky message parks in `awaiting_review` (the worker does **not** run).
2. `notify_pending()` sends *"🔔 approve? `/approve <id>` / `/reject <id>`"* to
   the owner (once per task).
3. The owner replies `/approve <id>` (or `/reject <id> [reason]`). `command_filter`
   catches it — it is **consumed as a command**, not run as a worker task — and
   applies it via compare-and-swap.
4. Approve → the task runs on the next tick and the answer comes back in chat;
   reject → it is dropped.

Only the owner's server-verified id may approve; a review command from anyone
else is ignored and flows on to the policy (which rejects it). Because the
reviewer uses the **pre-run** review queue (the task is `awaiting_review`, not
`running`), it is never affected by the crash-recovery clock (`stale_after`).

!!! warning "The action label is intent, not a sandbox"
    `action_classifier` decides what parks for review; it does **not** restrict
    which tools the worker may call. Guard any tool that sends, pays, deletes, or
    runs code at the tool layer (e.g. `TelegramTools` gates its send). Before
    opening the bot beyond the owner (`allowed_user_ids`), run those non-owner
    tasks with a reduced toolset.

## Anti-loop & throttle

The auto-reply path has two circuit breakers: it never replies into a bot
conversation (`reply_to_bots=False`, default), and `reply_min_interval_seconds`
rate-limits replies into a single chat. Long answers are chunked to the Bot API's
4096-char limit, counted as one logical reply for the throttle.

## Deploy & stress test

- **Ready to ship:** [`deploy/tg-bot/`](https://github.com/selvaz/LazyPulse/tree/main/deploy/tg-bot)
  is a DeepSeek-powered bot with optional web tools, one-command Railway/Render
  deploy, HITL wired, and `terminal_retention` set. Run **one** instance per bot
  (two pollers on one bot get `409 Conflict` from Telegram).
- **Load & stress test offline** (no token, no network) with
  [`deploy/tg-bot/loadtest.py`](https://github.com/selvaz/LazyPulse/blob/main/deploy/tg-bot/loadtest.py):
  it drives the real inbox + agent + reviewer through a fake client and checks
  throughput, dedup, the HITL approve/reject flow, crash-recovery (no
  double-execution), and retention bounding the Store.

!!! warning "Telegram rate limits"
    Polling and sending can get an account rate-limited or suspended under load.
    Do heavy stress testing **offline** with `loadtest.py`; keep live traffic
    gentle. LazyPulse is provided as is, without warranty — you are responsible
    for complying with Telegram's terms.
