# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.2.0] — 2026-05-28

### Added
- **`RetryPolicy`** — exponential backoff retry with configurable `max_attempts`,
  `backoff_base`, `backoff_max`, and `retry_on` exception filter. Import from
  `lazypulse` or `lazypulse.retry`. Set `retry_policy=RetryPolicy(...)` on
  `PulseAgent` to enable automatic retries.
- **`CronTrigger`** — cron-expression based recurring trigger (requires
  `pip install 'lazypulse[cron]'`). Use `PulseAgent.schedule_cron(text, expr)`
  to register a recurring task; the tick loop fires it on schedule and advances
  the next fire time atomically via CAS.
- **`RateLimit`** — per-sender rate limit on inbound intake. Set
  `PulsePolicy(rate_limit=RateLimit(max_per_sender=10, window_seconds=3600,
  on_exceeded="reject"))` to reject or queue messages from high-volume senders.
  Uses a CAS-based window counter keyed by `pulse:rate:{sender}:{bucket}`.
- **`PulseRecord` schema evolution** — five new fields with defaults so v0.1
  JSON round-trips cleanly under the v0.2 model:
  `attempt`, `next_retry_at`, `rate_limited`, `route`, `error_type`.
- **`Store.items(prefix=)` integration** — `_scan_records` now uses an indexed
  B-tree range scan (`store.items(prefix="pulse:task:")`) when the store
  supports it (lazybridge ≥ 0.9.1), replacing the O(N+1) loop.
- **`store_keys.CRON` / `CRON_PREFIX`** — key templates for cron job records.
- **`store_keys.RATE_KEY` / `RATE_PREFIX`** — key templates for rate-limit counters.

### Changed
- `lazybridge` pin kept at `>=0.7.9,<0.10`. The indexed `Store.items(prefix=)`
  fast path is used opportunistically (guarded by `hasattr`), so it engages on
  lazybridge ≥ 0.9.1 and falls back to the full-keyspace scan on older versions
  — no hard bump is required. (A first pass raised the floor to `>=0.9.1`; it
  was reverted because 0.9.1 is not yet on PyPI.)
- `__version__` bumped to `"0.2.0"`.
- `PulsePolicy` gains an optional `rate_limit: RateLimit | None = None` field.
- `tick_once` now calls `_process_cron` and `_reschedule_due_retries` before
  intake, so cron jobs and retries are handled every beat.

### Deprecated (shims removed in 0.3)
- None.

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
