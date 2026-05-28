# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.1.0] — first release

First tagged release of LazyPulse: an always-on orchestration runtime (tick
loop + trust policy + inbound adapters) on top of lazybridge.

> The first 0.1.0 upload to PyPI is **manual** — `lazytoolkit` is not on PyPI
> yet, so the release workflow's trusted-publishing path is staged but the
> initial upload is performed by a maintainer.

### Fixed
- **Tick-loop head-of-line blocking / starvation.** The background loop now
  dispatches each due task as its own asyncio task (bounded by a loop-wide
  semaphore) instead of awaiting workers inline, so a slow worker — or one
  parked in human review — no longer stalls intake, recovery, or other due
  work.
- **Unbounded Store growth.** Added opt-in `terminal_retention=` so terminal
  records (`completed` / `rejected` / `failed`) older than the configured age
  are pruned during ticks. Recommended for always-on agents in production.
- **Terminal-record retention correctness.** Terminal records now consistently
  stamp `completed_at`, including policy-rejected intake, so retention can
  actually purge them.

### Added
- `PulseAgent` with a synchronous lifecycle (`start` / `stop` / `running` /
  `serve` / `tick`) hiding an event loop in a daemon thread.
- `PulsePolicy` pre-execution authorization (sender-based trust, never message
  text), Gmail / Telegram / webhook inbound adapters, `StoreReviewerUI`
  human-in-the-loop review, and `schedule*` timers.
- `py.typed` marker so downstream consumers get LazyPulse's types.

### Changed — lazytoolkit extraction (Phases 0–2)

The Gmail and Telegram **clients** and **tools** were extracted to the new
sibling package **`lazytoolkit`** (repo: `selvaz/LazyTools`). LazyPulse keeps
the inbound adapters (inbox + policy) and the orchestration runtime.

- Extracted a reusable safety layer (`Allowlist` + `ConfirmationGate`) and
  refactored `GmailTools` / `TelegramTools` onto it (public surface unchanged:
  `confirm_once`, `confirm_send`, `require_confirmation`, `*SendBlocked`), then
  moved the tools to `lazytools.connectors.{gmail,telegram}`.
- `parse_authentication_results`, `GmailClient`/`GmailService`,
  `TelegramClient`/`TelegramService` moved to `lazytools.connectors.*`.
- The `gmail` and `telegram` extras now resolve to `lazytoolkit[gmail]` /
  `lazytoolkit[telegram]` instead of pulling the Google/httpx libraries directly.
- `lazypulse._context` shares its task-id contextvar with
  `lazytools.safety.active_scope` when `lazytoolkit` is installed, so a moved
  guarded tool can still bind a one-shot send grant to the running task. The
  import is guarded — `import lazypulse` works without `lazytoolkit`.
- Widened the `lazybridge` pin to `>=0.7.9,<0.10` so LazyPulse installs
  alongside lazybridge 0.9.0 (verified runtime-compatible).

### Deprecated (shims removed in 0.2)
- `from lazypulse.adapters.gmail import GmailTools` (and `GmailClient`,
  `GmailService`, `GmailSendBlocked`, `parse_authentication_results`) and the
  Telegram equivalents still work via a lazy re-export that emits a
  `DeprecationWarning`. Import from `lazytools.connectors.*` instead.
