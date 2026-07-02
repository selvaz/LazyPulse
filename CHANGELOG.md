# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Fixed
- **Blocking network I/O no longer stalls the tick loop.** `TelegramInbox`
  (`drain`/`reply`), `GmailInbox`, and `GmailPushInbox` called their
  synchronous clients directly inside `async` methods running on the tick
  loop's event loop — a slow API round-trip (up to the client's HTTP timeout)
  froze every in-flight worker, intake, and crash recovery. All client calls
  are now offloaded via `asyncio.to_thread`. `OutlookInbox` is deliberately
  excluded: its COM client is apartment-threaded and local-IPC only.

### Removed
- **The `lazypulse.adapters.telegram` deprecation shim is gone** (overdue: its
  own message promised removal in 0.3, and 0.3.0 removed the equivalent Gmail
  shim). The lazy PEP 562 re-exports of `TelegramClient`, `TelegramService`,
  `TelegramTools`, and `TelegramSendBlocked` (moved to
  `lazytools.connectors.telegram` in 0.2) now raise `AttributeError` instead of
  emitting a `DeprecationWarning`. Import from `lazytools.connectors.telegram`
  directly; the `lazypulse.TelegramTools` convenience re-export is unaffected.

### Added — local Outlook desktop intake (low-setup alternative to Gmail)
- **`OutlookInbox` / `OutlookInboxConfig` / `OutlookPolicy`** (new `outlook`
  extra → `lazytoolkit[outlook]`). Polls the copy of Outlook **already running
  and signed in** on the same Windows machine over COM, instead of a cloud
  mail API — so there is **no OAuth, no API quota, and no Pub/Sub push
  plumbing** to set up. Because it polls a *local* store there is no
  rate-limit / suspension risk, so a plain per-tick poll is the model (no push
  variant needed).
  - Behaviourally identical to `GmailInbox`: at-least-once (re-emits until the
    central `EVENT` marker exists), central dedupe, and the **same**
    authentication-aware conversion. The genuine, server-stamped
    `Authentication-Results` header is parsed with the same
    `parse_authentication_results`; `OutlookPolicy` maps DKIM/SPF/DMARC to a
    `TrustLevel` exactly as `GmailPolicy` does.
  - `OutlookInboxConfig.query` is an Outlook Restrict filter (default
    `"[Unread] = true"`); `trusted_authserv_id` pins your inbound mail host
    (no universal value as with Gmail). When it is unset, `OutlookPolicy`
    **fails closed** — an arbitrary inbound server may not stamp its own
    `Authentication-Results`, so an unpinned header is not trusted and no
    message can be owner/external verified; set the pin to enable verified
    trust.
  - The `OutlookClient` / `OutlookTools` clients live in
    `lazytools.connectors.outlook`; re-exported as `lazypulse.OutlookTools`
    for convenience. See `examples/08_outlook_local.py`.

## [0.3.0] — 2026-06-12

### Removed (breaking, as documented in 0.2)
- **The `lazypulse.adapters.gmail` deprecation shim is gone.** The lazy
  PEP 562 re-exports of `GmailClient`, `GmailService`, `GmailTools`,
  `GmailSendBlocked`, and `parse_authentication_results` (moved to
  `lazytools.connectors.gmail` in 0.2) now raise `AttributeError` instead
  of emitting a `DeprecationWarning`. Import from
  `lazytools.connectors.gmail` directly.

### Added
- **`GmailPushInbox` / `GmailPushConfig` — event-driven Gmail intake, now
  the default recommendation over polling.** Gmail's `users.watch` is
  armed onto a Cloud Pub/Sub topic (and re-armed before its <=7-day
  expiry); the push subscription targets the adapter's HTTP endpoint
  (shared `?token=` auth — 403 on mismatch so misconfiguration stays
  visible; malformed bodies are acked to avoid poison-message
  redelivery; notifications for other accounts are ignored). The
  handler only flips a flag; the next `drain()` makes **one**
  `users.history.list` call from the cursor persisted under
  `store_keys.LAST_HISTORY` (previously reserved, now live) and emits
  new mail through the same authentication-aware conversion as the
  polling `GmailInbox`. At-least-once: the cursor advances only after
  every id in the prior batch has its `EVENT` marker; expired cursors
  resync forward with a warning; an optional idle resync
  (`idle_resync_seconds`, default 900) covers lost push deliveries.
  Steady-state Gmail API usage is zero calls while the mailbox is
  quiet. See `examples/04_gmail_push.py` for the Pub/Sub setup.
  Burst safety (post-review hardening): history syncs are capped at 100
  ids per drain, the cursor never advances past unreturned mail (client
  contract), and the adapter re-arms its own notified flag while batches
  come back full — a burst larger than one batch drains over consecutive
  ticks with no skipped messages.
- **Exponential backoff on `adapter.drain()` failures.** A failing
  adapter was previously re-drained at full tick rate — with a
  1-second tick that is 86,400 failing calls/day against an already
  rate-limited upstream. Consecutive failures now back off
  (`adapter_backoff_base`, default 2s, doubling up to
  `adapter_backoff_cap`, default 300s; reset on first success), and
  `pulse.adapter_error` events carry `consecutive_failures` /
  `backoff_seconds`.

### Changed
- **`lazytoolkit` pin raised to `>=0.3.0,<0.4`** (was `<0.2.0`, stale the
  moment LazyTools 0.2.0 shipped). 0.3.0 is required — not just allowed —
  because `GmailPushInbox` burst-safety depends on the cursor-safety
  contract introduced after the 0.2.0 release; the released 0.2.0 build
  would silently drop mail on capped history walks. CI installs the
  sibling at the commit carrying the fixed contract.
- **`pending_tasks` / `purge_terminal_tasks` now use the indexed
  `Store.items(prefix=)` range scan** (with a `keys()` fallback for older
  stores), matching `PulseAgent._scan_records`. Both previously walked the whole
  keyspace; they are now O(M) in the number of task records. Notably the
  retention prune path — the mechanism meant to keep an always-on Store bounded —
  no longer does a full O(N) keyspace scan itself.

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
