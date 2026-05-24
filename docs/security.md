# Security & threat model

LazyPulse runs an agent against messages from the outside world. The design
assumption is simple: **the worker's reasoning is not a security boundary.**
A prompt-injected email can convince an LLM of anything. So authorization
happens *before* the worker runs, in code, based on who the sender is — not
what the message says.

## Threat model

| Threat | Mitigation |
|---|---|
| Prompt injection ("ignore previous instructions…") | The policy classifies and authorizes by **sender identity**, never by message text. An unknown sender is rejected before the worker sees the text. `classify()` ignores the body entirely. |
| Forgotten policy in production | A `PulseAgent` with adapters but no `policy=` **refuses to construct** (raises). Allow-all requires the explicit `unsafe_allow_all=True` opt-in, reserved for local dev. |
| Spoofed owner address | `GmailPolicy` only grants `OWNER_VERIFIED_EMAIL` when the parsed sender address (display names stripped) is an owner **and** DKIM + DMARC pass. A missing `Authentication-Results` header can never be owner-verified. Three defences guard the parser: (1) **authserv-id pinning** — only the header whose leading authserv-id *exactly* equals `trusted_authserv_id` (default `"mx.google.com"`) is parsed; a forged header with any other authserv-id — including a look-alike like `mx.google.com.evil.com` — is rejected outright; (2) **first-wins header selection** — `GmailInbox` keeps only the first `Authentication-Results` header, which is the one Gmail prepends (RFC 8601 §5.7); a forged copy carried inside the message body appears later and is silently discarded; (3) **comment stripping + token anchoring** — a `pass` inside an RFC comment or an `x-dkim=pass` extension field cannot spoof a result. |
| Unauthorized external send | `EXTERNAL_SEND` / `DESTRUCTIVE` from a verified owner returns `REQUIRE_OWNER_CONFIRMATION` → the task parks in `awaiting_review`, never auto-runs until a human calls `approve_task`. `GmailTools.gmail_send` is independently gated on explicit confirmation + a recipient allow-list. Each confirmation is one-shot; pass `task_id=` to bind it to a single task so a concurrent task can't spend it. |
| Replay of a captured webhook | Optional HMAC-SHA256 body signing, plus nonce tracking that consults the Store (409 on a repeat) so protection survives a restart. |
| Duplicate / re-delivered message | Deduped centrally on `message_id` via the EVENT marker. Adapters are at-least-once (they may re-emit until the task is recorded); a message still becomes at most one task. |
| Auto-reply loop / amplification | The Telegram Responder never replies into a bot conversation (`reply_to_bots=False`, default) and supports a per-chat rate limit (`reply_min_interval_seconds`) that drops replies firing inside the window — a circuit breaker against runaway reply loops. |
| Double execution under crash/retry or multi-agent | The `scheduled → running` transition is a Store compare-and-swap. Only one ticker can claim a task. Stale `running` records are recovered with a restart cap so a poison task can't loop forever. |
| Over-broad OAuth scope | Gmail defaults to the `metadata` scope (headers + snippet). `readonly` works but emits a `UserWarning` so the broader grant is a deliberate choice. |

## The trust × action matrix

`PulsePolicy.authorize(identity, action)` consults `action_rules` (a
`dict[TrustLevel, set[ActionClass]]`). The default:

| TrustLevel | Allowed actions |
|---|---|
| `UNKNOWN` | *(none)* |
| `OWNER_CLAIM_UNVERIFIED` | *(none)* |
| `EXTERNAL_VERIFIED` | `READ_PUBLIC` |
| `OWNER_VERIFIED_EMAIL` | `READ_PUBLIC`, `WRITE_LOCAL` |
| `APPROVED_SESSION` | `READ_PUBLIC`, `READ_PRIVATE`, `WRITE_LOCAL`, `EXTERNAL_SEND` |
| `SYSTEM` | *(all)* |

Anything not in the allowed set escalates rather than silently allowing:

- verified owner asking for `EXTERNAL_SEND` / `DESTRUCTIVE` → `REQUIRE_OWNER_CONFIRMATION`
- externally-verified stranger asking for more than `READ_PUBLIC` → `QUEUE_FOR_REVIEW`
- everyone else → `REJECT`

Override `action_rules` (or subclass and override `classify`) to tighten or
loosen this for your deployment. Tightening is always safe; loosening
`UNKNOWN` is where you take on risk.

## What the policy does *not* cover

The policy gates **inbound execution** — whether a message reaches the worker.
It is not a tool sandbox. Once the worker runs, any tool wired onto the Agent
can be called. So:

- **Guard sensitive tools at the tool layer.** `GmailTools.gmail_send` is
  blocked until confirmed and filtered by a recipient allow-list; do the same
  for any tool that sends, pays, deletes, or executes. The `ActionClass` on a
  message expresses *intent*; it does not automatically constrain tool calls.
- **Authentication parsing is a conservative MVP.** `GmailPolicy` reads
  the first `Authentication-Results` header (prepended by Gmail, authserv-id
  `mx.google.com`). Gmail strips inbound copies that *claim its own*
  authserv-id (RFC 8601 §5.7), but headers with a different authserv-id can
  survive forwarding — `GmailInbox` pins the authserv-id and uses first-wins
  selection to reject them. For full multi-hop email-auth verification,
  validate upstream and pass the result in `metadata`.
- **Chat platforms are authenticated for you.** `TelegramPolicy` keys on the
  numeric `message.from.id`, which Telegram verifies server-side and a sender
  cannot spoof — a stronger, simpler signal than email (no header to forge).
  `TelegramPolicy` also rejects bot accounts. The same shape applies to any
  platform-authenticated channel: classify on the verified user id, not on
  any field the sender can set. When intaking over a webhook, still verify the
  platform's signature (Slack signing secret, Telegram secret token, Discord
  Ed25519) the way `WebhookAdapter` checks its HMAC.

## Operational notes

- **Always set `policy=` in production.** With adapters and no policy the
  agent refuses to start; never paper over that with `unsafe_allow_all=True`
  outside local dev.
- Bind the webhook to `127.0.0.1` (the default) and put TLS + auth in a
  reverse proxy; do not expose `0.0.0.0` directly.
- Treat the Store as sensitive — it holds task text and identities. Use the
  lazybridge encrypted Store adapter if it lives on shared disk.
- Never commit `credentials.json` / `token.json` (they are git-ignored).
