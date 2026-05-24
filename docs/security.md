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
| Spoofed owner address | `GmailPolicy` only grants `OWNER_VERIFIED_EMAIL` when DKIM **and** DMARC pass. A missing `Authentication-Results` header can never be owner-verified. |
| Unauthorized external send | `EXTERNAL_SEND` / `DESTRUCTIVE` from a verified owner returns `REQUIRE_OWNER_CONFIRMATION` → the task parks in `awaiting_review`, never auto-runs. `GmailTools.gmail_send` is independently gated on explicit confirmation + a recipient allow-list. |
| Replay of a captured webhook | Optional HMAC-SHA256 body signing, plus per-request nonce tracking (409 on a repeat; nonces persisted to the Store). |
| Duplicate / re-delivered message | Every message is deduped on `message_id`; adapters also record what they have emitted (`GMAIL_PROCESSED`). A message becomes at most one task. |
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

## Operational notes

- Bind the webhook to `127.0.0.1` (the default) and put TLS + auth in a
  reverse proxy; do not expose `0.0.0.0` directly.
- Treat the Store as sensitive — it holds task text and identities. Use the
  lazybridge encrypted Store adapter if it lives on shared disk.
- Never commit `credentials.json` / `token.json` (they are git-ignored).
