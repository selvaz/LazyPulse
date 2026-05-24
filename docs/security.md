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
| Spoofed owner address | `GmailPolicy` only grants `OWNER_VERIFIED_EMAIL` when the parsed sender address (display names stripped) is an owner **and** DKIM + DMARC pass. A missing `Authentication-Results` header can never be owner-verified. The header parser strips CFWS comments and anchors method tokens, so a `pass` buried in a comment or an `x-dkim=pass` extension can't spoof a result. |
| Unauthorized external send | `EXTERNAL_SEND` / `DESTRUCTIVE` from a verified owner returns `REQUIRE_OWNER_CONFIRMATION` → the task parks in `awaiting_review`, never auto-runs until a human calls `approve_task`. `GmailTools.gmail_send` is independently gated on explicit confirmation + a recipient allow-list. |
| Replay of a captured webhook | Optional HMAC-SHA256 body signing, plus nonce tracking that consults the Store (409 on a repeat) so protection survives a restart. |
| Duplicate / re-delivered message | Deduped centrally on `message_id` via the EVENT marker. Adapters are at-least-once (they may re-emit until the task is recorded); a message still becomes at most one task. |
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
  Gmail's own `Authentication-Results` header (which Gmail adds after
  stripping inbound copies). It is hardened against comment/extension-field
  spoofing but is not a full multi-hop email-auth verifier — for that,
  validate upstream and pass the result in `metadata`.

## Operational notes

- **Always set `policy=` in production.** With adapters and no policy the
  agent refuses to start; never paper over that with `unsafe_allow_all=True`
  outside local dev.
- Bind the webhook to `127.0.0.1` (the default) and put TLS + auth in a
  reverse proxy; do not expose `0.0.0.0` directly.
- Treat the Store as sensitive — it holds task text and identities. Use the
  lazybridge encrypted Store adapter if it lives on shared disk.
- Never commit `credentials.json` / `token.json` (they are git-ignored).
