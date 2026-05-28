"""PulseAgent — an :class:`lazybridge.Agent` that wakes itself up.

``PulseAgent`` is a *subclass* of ``Agent``. It inherits the entire agent
surface unchanged — ``engine`` (LLMEngine, Plan, HumanEngine, custom),
``tools``, ``guard``, ``verify``, ``memory``, ``store``, ``session``,
``output``, ``sources``, ``cache``, ``fallback`` — and adds three things:

* a background **tick loop** (``start`` / ``stop`` / ``running``),
* an optional **policy** evaluated before any worker runs,
* a list of **adapters** that feed inbound messages into the loop.

The loop never reaches into ``Agent`` internals. Each authorized message is
executed through the ordinary public ``self.run(text)`` path, so every
future improvement to ``Agent.run`` (and every engine, including ``Plan``)
propagates for free.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from lazybridge import Agent

from lazypulse import store_keys
from lazypulse._context import active_task_id
from lazypulse.adapters.base import Responder
from lazypulse.models import (
    ActionClass,
    Identity,
    InboundMessage,
    PolicyDecision,
    PulseRecord,
    PulseStatus,
    TickReport,
    TrustLevel,
)
from lazypulse.retry import RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from lazybridge import EventType

    from lazypulse.adapters.base import Adapter
    from lazypulse.policy import PulsePolicy

#: Hard cap on automatic restarts of a stale ``running`` record before it is
#: marked ``failed``. Prevents a poison task from being retried forever.
_MAX_RESTARTS = 3

#: Minimum wall-clock seconds between terminal-record prunes. Pruning scans the
#: whole task space, so it runs at most this often rather than every tick.
_PRUNE_INTERVAL = 60.0


class PulseAgent(Agent):
    """Agent + tick loop + policy + adapters."""

    def __init__(
        self,
        *,
        adapters: list[Adapter] | None = None,
        policy: PulsePolicy | None = None,
        tick_seconds: float = 1.0,
        max_concurrent_inbound: int = 4,
        stale_after: float | None = None,
        terminal_retention: float | None = None,
        unsafe_allow_all: bool = False,
        clock: Callable[[], datetime] | None = None,
        retry_policy: RetryPolicy | None = None,
        **agent_kwargs: Any,
    ) -> None:
        # Risk note: super().__init__ MUST run first. Agent.__init__ registers
        # this instance with the Session and validates the engine/tool map;
        # attributes we set before it would be invisible to that wiring.
        super().__init__(**agent_kwargs)
        if self.store is None:
            raise ValueError(
                "PulseAgent requires store=. Task lifecycle (scheduled/running/"
                "completed) lives in the Store; pass store=Store() (in-memory) or "
                "store=Store(db='pulse.db') (persistent)."
            )
        self._adapters: list[Adapter] = list(adapters or [])
        # Name → adapter, for routing a completed task's reply back to its
        # source (see ``_maybe_reply``). Names must be unique: a reply is
        # routed by the source name recorded on the task, so two adapters
        # sharing a name (e.g. two default-named ``TelegramInbox``es) would
        # silently send replies through the wrong client. Fail fast instead.
        self._adapters_by_name: dict[str, Adapter] = {}
        for adapter in self._adapters:
            name = getattr(adapter, "name", repr(adapter))
            if name in self._adapters_by_name:
                raise ValueError(
                    f"Two adapters share name={name!r}. Adapter names must be unique so a "
                    "completed task's reply routes back to the adapter it came from. Pass a "
                    "distinct name= to each (e.g. TelegramInbox(client, cfg, name='telegram-2'))."
                )
            self._adapters_by_name[name] = adapter
        self._policy = policy
        self._unsafe_allow_all = unsafe_allow_all
        # Safety gate: a PulseAgent ingesting from external adapters with no
        # policy would run *every* inbound message (the no-policy path grants
        # SYSTEM trust). That is fine for local prototyping but a footgun in
        # production, so require an explicit opt-in.
        if self._adapters and self._policy is None and not unsafe_allow_all:
            raise ValueError(
                "PulseAgent has adapters but no policy=. Untrusted inbound would run "
                "with full trust. Pass a policy=PulsePolicy(...) (recommended) or "
                "unsafe_allow_all=True to explicitly opt into allow-all for local dev."
            )
        self._tick_seconds = tick_seconds
        self._max_concurrent = max_concurrent_inbound
        # A ``running`` record older than this many seconds is presumed to be
        # from a crashed process and is recovered. Default is 1 hour so both
        # legitimately slow LLM workers and tasks awaiting human review via
        # StoreReviewerUI (default timeout: 3600 s) are never falsely
        # recovered mid-run. Tune down for fast tasks (e.g. 300 s for a
        # pure-LLM agent); tune up if your review timeout exceeds 1 h.
        # See ``_recover_stale``.
        self._stale_after = stale_after if stale_after is not None else max(tick_seconds * 60, 3600.0)
        # Opt-in retention: when set, terminal records (completed/rejected/
        # failed) older than this many seconds are deleted during ticks so an
        # always-on agent's Store does not grow without bound. ``None`` (default)
        # keeps the full ledger forever — the historical behaviour.
        self._terminal_retention = terminal_retention
        self._last_prune_at: datetime | None = None
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._retry_policy = retry_policy
        # The loop runs on its own event loop in a daemon thread, so user code
        # stays synchronous — no asyncio.run / await / async with required.
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_task: asyncio.Task[None] | None = None
        # Background loop dispatch (see ``_tick_loop`` / ``_dispatch_due``): the
        # tick loop spawns each due task as its own asyncio task instead of
        # awaiting them inline, so a slow worker (or one parked in human review)
        # never stalls intake, recovery, or other due work. ``_inflight`` holds
        # the keys currently dispatched so a later tick doesn't re-collect them;
        # ``_sema`` bounds total concurrency across ticks (bound to the loop in
        # ``_tick_loop``); ``_bg_tasks`` keeps strong refs so tasks aren't GC'd.
        self._inflight: set[str] = set()
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._sema: asyncio.Semaphore | None = None
        # Background dispatch (``await_due=False``) returns before its workers
        # finish, so the tick that dispatched them can't report their terminal
        # outcome. Workers tally their result here; the next emitted tick folds
        # these in (and clears them) so live-loop ``pulse.tick`` events carry
        # accurate completed/failed counts. Only touched on the loop thread.
        self._bg_completed = 0
        self._bg_failed = 0

    # ------------------------------------------------------------------ #
    # Lifecycle — all synchronous; the event loop is hidden in a thread.
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Start the tick loop in a background thread. Non-blocking.

        Returns once the loop is live; keep doing whatever you like on the
        main thread (or just let the process idle). Call :meth:`stop` to end
        it, or use :meth:`running` / :meth:`serve`."""
        if self.is_running():
            raise RuntimeError(f"PulseAgent {self.name!r} already started")
        ready = threading.Event()

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._tick_task = loop.create_task(self._tick_loop())
            loop.call_soon(ready.set)
            try:
                loop.run_forever()
            finally:
                # Cancel the tick task and any still-running dispatched workers
                # so the loop drains cleanly (no "Task was destroyed but it is
                # pending" warnings). A worker cancelled mid-run leaves its
                # record in ``running``; crash recovery resets it on restart.
                pending = [t for t in (self._tick_task, *self._bg_tasks) if t is not None]
                for t in pending:
                    t.cancel()
                if pending:
                    try:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    except RuntimeError:
                        pass
                loop.close()

        self._thread = threading.Thread(target=_runner, name=f"pulse[{self.name}]", daemon=True)
        self._thread.start()
        if not ready.wait(timeout=5.0):
            # The loop thread never signalled readiness — don't hand back a
            # half-started agent whose is_running() lies.
            self._thread = None
            self._loop = None
            self._tick_task = None
            raise RuntimeError(f"PulseAgent {self.name!r} loop did not become ready within 5s")

    def stop(self) -> None:
        """Stop the tick loop and join its thread. Safe to call twice and safe
        to call without a prior :meth:`start`."""
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10.0)
        if thread.is_alive():
            # A worker is wedged (e.g. blocking sync work on the loop thread).
            # Don't pretend we stopped: leave the state so is_running() stays
            # truthful and a re-start raises instead of spawning a second loop.
            warnings.warn(
                f"PulseAgent {self.name!r} did not stop within 10s; the loop thread is still alive.",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        self._loop = None
        self._thread = None
        self._tick_task = None
        # The loop thread has exited, so its background-dispatch state can be
        # reset safely from here for a clean re-start.
        self._inflight.clear()
        self._bg_tasks.clear()
        self._sema = None
        self._bg_completed = 0
        self._bg_failed = 0

    def is_running(self) -> bool:
        """True while the background tick loop is active."""
        return self._thread is not None and self._thread.is_alive()

    @contextmanager
    def running(self) -> Iterator[None]:
        """Run the loop for the duration of a ``with`` block::

            with pulse.running():
                time.sleep(3600)        # serve for an hour
        """
        self.start()
        try:
            yield
        finally:
            self.stop()

    def serve(self) -> None:
        """Start the loop and block until interrupted (Ctrl-C). The one-liner
        for an always-on agent::

            PulseAgent(...).serve()
        """
        self.start()
        try:
            while self.is_running():
                time.sleep(0.25)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def tick(self) -> TickReport:
        """Run exactly one beat synchronously, without starting the loop.

        Handy for cron-style one-shots and scripts. Don't mix with a running
        background loop — use one or the other.

        Raises ``RuntimeError`` if the background loop is live: ``tick()`` spins
        a throwaway event loop and runs due tasks under a *per-call* semaphore
        (see :meth:`_run_due`), which is independent of the loop-wide
        ``max_concurrent_inbound`` gate the background loop enforces. Running
        both at once would exceed the intended global concurrency cap and let
        two tickers race over the same records, so refuse it. Use the loop's own
        ticks (or stop it first)."""
        if self.is_running():
            raise RuntimeError(
                f"PulseAgent {self.name!r} is already running its background loop; "
                "do not call tick() concurrently. Use the running loop's ticks, "
                "or call stop() first to drive ticks synchronously."
            )
        return _run_sync(self.tick_once())

    # ------------------------------------------------------------------ #
    # Scheduling (programmatic, trusted)
    # ------------------------------------------------------------------ #
    def schedule(
        self,
        text: str,
        *,
        run_at: datetime | None = None,
        action: ActionClass = ActionClass.READ_PUBLIC,
    ) -> str:
        """Enqueue a task to run at ``run_at`` (default: now). Returns its ``task_id``.

        This is the timer side of LazyPulse — pair it with ``tick_seconds`` to
        run work on a schedule. Programmatic scheduling is **trusted**: the
        caller is your own code, so it bypasses the policy (which exists to
        authorize *external* inbound messages). The loop runs it on the first
        tick where ``run_at <= now``."""
        now = self._clock()
        record = PulseRecord(
            text=text,
            status="scheduled",
            created_at=now,
            run_at=run_at or now,
            source_event_id=f"local:{uuid.uuid4()}",
            identity=Identity(trust=TrustLevel.SYSTEM),
            action_class=action,
            decision=PolicyDecision.ALLOW,
        )
        self._write_record(record)
        return record.task_id

    def schedule_after(self, text: str, seconds: float, **kwargs: Any) -> str:
        """Schedule a task to run ``seconds`` from now. Returns its ``task_id``."""
        return self.schedule(text, run_at=self._clock() + timedelta(seconds=seconds), **kwargs)

    def schedule_at(self, text: str, when: datetime, **kwargs: Any) -> str:
        """Schedule a task to run at the absolute time ``when``. Returns its ``task_id``."""
        return self.schedule(text, run_at=when, **kwargs)

    def schedule_cron(self, text: str, cron: str, tz: str = "UTC") -> str:
        """Register a recurring cron task. Returns the ``cron_id``.

        Requires the ``cron`` extra (``pip install 'lazypulse[cron]'``).
        """
        from lazypulse.cron import CronTrigger

        trigger = CronTrigger(cron, tz)
        now = self._clock()
        next_dt = trigger.next(now)
        cron_id = str(uuid.uuid4())
        key = store_keys.CRON.format(cron_id=cron_id)
        assert self.store is not None
        self.store.write(
            key,
            {
                "cron_id": cron_id,
                "text": text,
                "expr": cron,
                "tz": tz,
                "next_fire_at": next_dt.isoformat(),
                "created_at": now.isoformat(),
            },
        )
        return cron_id

    # ------------------------------------------------------------------ #
    # The tick
    # ------------------------------------------------------------------ #
    async def tick_once(self, *, await_due: bool = True) -> TickReport:
        """Execute a single beat. Exposed for deterministic testing.

        Order matters: recover crashed tasks first, prune old terminal records,
        then process cron jobs and due retries, then intake new inbound (which
        may become due immediately), then run everything due.

        ``await_due`` controls how due tasks are executed. ``True`` (default)
        runs them inline and waits for completion — the right behaviour for the
        synchronous one-shot :meth:`tick` and for deterministic tests, where the
        returned :class:`TickReport` must reflect terminal outcomes. The
        background :meth:`_tick_loop` passes ``False`` so each due task is
        dispatched as its own asyncio task and the loop keeps ticking; a slow or
        human-review task then never stalls intake or recovery.
        """
        now = self._clock()
        report = TickReport(at=now)

        self._recover_stale(now, report)
        self._prune_terminal(now, report)
        self._process_cron(now)
        self._reschedule_due_retries(now, report)

        for msg in await self._drain_adapters():
            report.drained += 1
            try:
                self._intake(msg, now, report)
            except Exception as exc:
                self._emit(
                    "pulse.intake_error",
                    {"message_id": msg.message_id, "error": f"{type(exc).__name__}: {exc}"},
                )

        due = self._collect_due(now)
        # Skip tasks already dispatched by an earlier tick (background mode):
        # they are still ``scheduled`` until they win their CAS claim, so they
        # would otherwise be collected again. The claim CAS makes a double
        # dispatch harmless regardless, but filtering avoids the wasted work.
        if self._inflight:
            due = [(key, raw) for key, raw in due if key not in self._inflight]
        report.due = len(due)
        if due:
            if await_due:
                await self._run_due(due, report)
            else:
                self._dispatch_due(due)

        # Fold in terminal outcomes of background workers that finished since the
        # last tick (background path only — ``_run_due`` already populates these
        # inline). Draining the counters here makes each live-loop ``pulse.tick``
        # carry the completed/failed counts for work that actually finished
        # during the preceding interval.
        if not await_due and (self._bg_completed or self._bg_failed):
            report.completed += self._bg_completed
            report.failed += self._bg_failed
            self._bg_completed = 0
            self._bg_failed = 0

        # Only emit when something happened — a quiet tick every ``tick_seconds``
        # would otherwise flood the Session of a long-running agent.
        if (
            report.drained
            or report.due
            or report.recovered
            or report.pruned
            or report.completed
            or report.failed
        ):
            self._emit("pulse.tick", report.model_dump(mode="json"))
        return report

    async def _tick_loop(self) -> None:
        """Loop forever until cancelled. An exception in a tick is logged to
        the Session but never escapes — the loop must outlive bad ticks."""
        # Bound here so the semaphore binds to *this* loop (the one running the
        # background ticks). Created per-loop-start, never carried across loops.
        self._sema = asyncio.Semaphore(self._max_concurrent)
        while True:
            try:
                await self.tick_once(await_due=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit("pulse.tick_error", {"error": f"{type(exc).__name__}: {exc}"})
            await asyncio.sleep(self._tick_seconds)

    # ------------------------------------------------------------------ #
    # Intake
    # ------------------------------------------------------------------ #
    async def _drain_adapters(self) -> list[InboundMessage]:
        if not self._adapters:
            return []
        assert self.store is not None  # guaranteed by the __init__ check
        out: list[InboundMessage] = []
        for adapter in self._adapters:
            try:
                msgs = await adapter.drain(store=self.store, session=self.session)
            except Exception as exc:
                self._emit(
                    "pulse.adapter_error",
                    {"adapter": getattr(adapter, "name", repr(adapter)), "error": f"{type(exc).__name__}: {exc}"},
                )
                continue
            out.extend(msgs)
        return out

    def _intake(self, msg: InboundMessage, now: datetime, report: TickReport) -> None:
        """Turn one inbound message into a PulseRecord, applying the policy.

        Dedupe on ``message_id`` via the EVENT marker so the same external
        message is never turned into two tasks, regardless of how many times
        an adapter re-drains it.
        """
        if self.store is None:
            raise RuntimeError("PulseAgent requires store= to track task lifecycle")

        event_key = store_keys.event_key(msg.message_id)
        if self.store.read(event_key) is not None:
            report.duplicates += 1
            return

        identity, decision = self._authorize(msg)

        # Per-sender rate limiting: only applied to messages the policy would ALLOW.
        rate_limited = False
        if decision == PolicyDecision.ALLOW and self._policy is not None and self._policy.rate_limit is not None:
            rl = self._policy.rate_limit
            sender = msg.sender_raw or "anonymous"
            window_bucket = int(now.timestamp()) // rl.window_seconds
            rate_key = store_keys.RATE_KEY.format(sender=sender, window_bucket=window_bucket)
            rate_limited = self._check_rate_limit(rate_key, rl.max_per_sender)
            if rate_limited:
                decision = PolicyDecision.REJECT if rl.on_exceeded == "reject" else PolicyDecision.QUEUE_FOR_REVIEW

        status, tally = _status_for_decision(decision)
        # A policy REJECT lands as terminal on first write; stamp completed_at
        # so terminal_retention can age it out. Without this, a spam-heavy
        # adapter would grow the ledger unbounded despite retention.
        completed_at = now if status == "rejected" else None
        record = PulseRecord(
            text=msg.text,
            status=status,
            created_at=now,
            run_at=now,
            completed_at=completed_at,
            source_event_id=msg.message_id,
            source=msg.source,
            inbound_metadata=msg.metadata,
            identity=identity,
            action_class=msg.requested_action,
            decision=decision,
            rate_limited=rate_limited,
        )
        self._write_record(record)
        setattr(report, tally, getattr(report, tally) + 1)

    def _write_record(self, record: PulseRecord) -> None:
        """Persist a record and its idempotency marker.

        The EVENT marker keyed on ``source_event_id`` is the single dedupe
        point: an adapter may re-emit a message until this marker exists
        (at-least-once), and intake skips anything already marked."""
        assert self.store is not None
        self.store.write(store_keys.task_key(record.task_id), record.model_dump(mode="json"))
        if record.source_event_id is not None:
            self.store.write(store_keys.event_key(record.source_event_id), {"task_id": record.task_id})

    def _authorize(self, msg: InboundMessage) -> tuple[Identity, PolicyDecision]:
        # No policy → dev mode: everything is allowed. Useful for local
        # prototyping where the only sources are trusted.
        if self._policy is None:
            return Identity(sender=msg.sender_raw, trust=TrustLevel.SYSTEM), PolicyDecision.ALLOW
        identity = self._policy.classify(msg)
        decision = self._policy.authorize(identity, msg.requested_action)
        return identity, decision

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def _collect_due(self, now: datetime) -> list[tuple[str, dict[str, Any]]]:
        due: list[tuple[str, dict[str, Any]]] = []
        for key, raw in self._scan_records():
            if raw.get("status") != "scheduled":
                continue
            run_at = _parse_dt(raw.get("run_at"))
            if run_at is not None and run_at <= now:
                due.append((key, raw))
        return due

    async def _run_due(self, due: list[tuple[str, dict[str, Any]]], report: TickReport) -> None:
        # The concurrency gate is created here, per tick, so it binds to the
        # event loop actually running this tick — never carried across loops
        # (sync tick() spins a throwaway loop; start() runs its own; an async
        # host has its own). An instance-level Semaphore would bind to the
        # first loop that touched it and raise everywhere else.
        sema = asyncio.Semaphore(self._max_concurrent)

        async def _guarded(key: str, raw: dict[str, Any]) -> str | None:
            async with sema:
                return await self._run_one(key, raw)

        results = await asyncio.gather(*(_guarded(key, raw) for key, raw in due), return_exceptions=True)
        for outcome in results:
            if isinstance(outcome, BaseException):
                # A store write failed unexpectedly — count it and keep going;
                # one bad task must not abort the rest of the batch.
                self._emit("pulse.run_error", {"error": f"{type(outcome).__name__}: {outcome}"})
                report.failed += 1
            elif outcome == "completed":
                report.completed += 1
            elif outcome in ("failed", "rejected"):
                report.failed += 1

    def _dispatch_due(self, due: list[tuple[str, dict[str, Any]]]) -> None:
        """Spawn each due task as its own background asyncio task (loop mode).

        Concurrency is bounded by ``self._sema`` (acquired inside the task, so
        dispatch returns immediately). The key is marked in-flight so a later
        tick won't re-collect a task that is dispatched but not yet CAS-claimed.
        Used only from :meth:`_tick_loop`; the loop keeps ticking while these
        run, so a slow worker can't starve intake or recovery."""
        for key, raw in due:
            self._inflight.add(key)
            task = asyncio.ensure_future(self._run_one_bg(key, raw))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _run_one_bg(self, key: str, raw: dict[str, Any]) -> None:
        """Background wrapper around :meth:`_run_one`: bound by the loop-wide
        semaphore, mirrors ``_run_due``'s error reporting, and always clears the
        in-flight marker so the key can be re-collected if its claim was lost."""
        assert self._sema is not None  # set in _tick_loop before any dispatch
        try:
            async with self._sema:
                outcome = await self._run_one(key, raw)
            if outcome == "completed":
                self._bg_completed += 1
            elif outcome in ("failed", "rejected"):
                self._bg_failed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit("pulse.run_error", {"error": f"{type(exc).__name__}: {exc}"})
            self._bg_failed += 1
        finally:
            self._inflight.discard(key)

    async def _run_one(self, key: str, expected: dict[str, Any]) -> str | None:
        """Claim one scheduled record via CAS, run it, and persist the result.

        Returns the terminal status, or ``None`` when a CAS lost — either the
        initial scheduled→running claim (another ticker owns it) or the final
        write (crash recovery reset the record mid-run). Persisting the result
        via CAS against our own ``running`` snapshot means a slow worker can
        never clobber a record that recovery has already taken over.

        Concurrency is bounded by the caller (``_run_due``)."""
        started = PulseRecord.model_validate(expected).model_copy(
            update={"status": "running", "started_at": self._clock()}
        )
        running_dict = started.model_dump(mode="json")
        assert self.store is not None  # guaranteed by the __init__ check
        if not self.store.compare_and_swap(key, expected, running_dict):
            return None  # lost the race — someone else owns this task

        # Expose the task id to the worker's tools for the duration of the run
        # so a gated send tool can bind its one-shot grant to *this* task and
        # not be consumed by a concurrent one. Reset afterwards; each _run_one
        # already runs in its own copied context (gathered task), so this is
        # task-isolated regardless.
        token = active_task_id.set(started.task_id)
        try:
            env = await self.run(started.text)
        except Exception as exc:
            error_type = type(exc).__name__
            will_retry = self._retry_policy is not None and self._retry_policy.should_retry(started.attempt, exc)
            next_retry_at_val = None
            if will_retry:
                delay = self._retry_policy.next_delay(started.attempt)  # type: ignore[union-attr]
                next_retry_at_val = self._clock() + timedelta(seconds=delay)
            final = started.model_copy(
                update={
                    "status": "failed",
                    "completed_at": None if will_retry else self._clock(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_type": error_type,
                    "next_retry_at": next_retry_at_val,
                }
            )
        else:
            final = _finalize(started, env, self._clock())
            if final.status == "failed" and self._retry_policy is not None and self._retry_policy.should_retry_by_count(started.attempt):
                delay = self._retry_policy.next_delay(started.attempt)
                now_ts = self._clock()
                err_obj = getattr(env, "error", None)
                err_type = getattr(err_obj, "type", "engine_error") if err_obj else "engine_error"
                final = final.model_copy(
                    update={
                        "completed_at": None,
                        "next_retry_at": now_ts + timedelta(seconds=delay),
                        "error_type": err_type,
                    }
                )
        finally:
            active_task_id.reset(token)

        if not self.store.compare_and_swap(key, running_dict, final.model_dump(mode="json")):
            # Recovery (or another process) took the record over while we
            # ran. Don't resurrect it — the worker's side effects already
            # happened, but the ledger is no longer ours to write.
            self._emit("pulse.write_conflict", {"task_id": started.task_id, "would_be_status": final.status})
            return None
        if final.status == "completed":
            await self._maybe_reply(final)
        return final.status

    async def _maybe_reply(self, record: PulseRecord) -> None:
        """Route a completed task's output back to its originating conversation.

        Only fires when the source adapter implements :class:`Responder` (e.g.
        ``TelegramInbox``) and the worker produced text. Best-effort: a reply
        failure is logged to the Session but never un-completes the task."""
        if record.source is None or not record.worker_text:
            return
        adapter = self._adapters_by_name.get(record.source)
        if not isinstance(adapter, Responder):
            return
        assert self.store is not None
        try:
            await adapter.reply(record, record.worker_text, store=self.store, session=self.session)
        except Exception as exc:
            self._emit("pulse.reply_error", {"task_id": record.task_id, "error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------------ #
    # Crash recovery
    # ------------------------------------------------------------------ #
    def _recover_stale(self, now: datetime, report: TickReport) -> None:
        """Reset ``running`` records that have been in flight implausibly long
        back to ``scheduled`` so a crashed process's work is retried. After
        ``_MAX_RESTARTS`` resets the task is marked ``failed``."""
        threshold = timedelta(seconds=self._stale_after)
        for key, raw in self._scan_records():
            if raw.get("status") != "running":
                continue
            started_at = _parse_dt(raw.get("started_at"))
            if started_at is None or now - started_at <= threshold:
                continue
            rec = PulseRecord.model_validate(raw)
            if rec.restart_count >= _MAX_RESTARTS:
                recovered = rec.model_copy(
                    update={"status": "failed", "completed_at": now, "error": "exceeded max restarts"}
                )
            else:
                recovered = rec.model_copy(
                    update={"status": "scheduled", "started_at": None, "restart_count": rec.restart_count + 1}
                )
            if self.store is not None and self.store.compare_and_swap(key, raw, recovered.model_dump(mode="json")):
                report.recovered += 1

    def _prune_terminal(self, now: datetime, report: TickReport) -> None:
        """Delete terminal records older than ``terminal_retention`` so an
        always-on agent's Store does not grow without bound. No-op unless
        ``terminal_retention=`` was set. Throttled to once per ``_PRUNE_INTERVAL``
        seconds because it scans the whole task space."""
        if self._terminal_retention is None or self.store is None:
            return
        if self._last_prune_at is not None and (now - self._last_prune_at).total_seconds() < _PRUNE_INTERVAL:
            return
        self._last_prune_at = now
        from lazypulse.tasks import purge_terminal_tasks

        report.pruned += purge_terminal_tasks(
            self.store, older_than=timedelta(seconds=self._terminal_retention), now=now
        )

    def _reschedule_due_retries(self, now: datetime, report: TickReport) -> None:
        """Re-schedule failed records whose ``next_retry_at`` has passed."""
        if self._retry_policy is None or self.store is None:
            return
        for key, raw in self._scan_records():
            if raw.get("status") != "failed":
                continue
            attempt = int(raw.get("attempt", 0))
            if attempt >= self._retry_policy.max_attempts - 1:
                continue
            next_retry_at = _parse_dt(raw.get("next_retry_at"))
            if next_retry_at is None or next_retry_at > now:
                continue
            rescheduled = PulseRecord.model_validate(raw).model_copy(
                update={
                    "status": "scheduled",
                    "started_at": None,
                    "run_at": now,
                    "attempt": attempt + 1,
                    "next_retry_at": None,
                    "completed_at": None,
                }
            )
            self.store.compare_and_swap(key, raw, rescheduled.model_dump(mode="json"))

    def _process_cron(self, now: datetime) -> None:
        """Fire any cron jobs whose ``next_fire_at`` has passed."""
        if self.store is None:
            return
        if not hasattr(self.store, "items"):
            return
        for key, raw in self.store.items(prefix=store_keys.CRON_PREFIX):
            if not isinstance(raw, dict):
                continue
            next_fire_at = _parse_dt(raw.get("next_fire_at"))
            if next_fire_at is None or next_fire_at > now:
                continue
            self.schedule(raw.get("text", ""), run_at=now)
            try:
                from lazypulse.cron import CronTrigger

                trigger = CronTrigger(raw["expr"], raw.get("tz", "UTC"))
                next_dt = trigger.next(now)
                updated = {**raw, "next_fire_at": next_dt.isoformat()}
                self.store.compare_and_swap(key, raw, updated)
            except Exception:
                pass

    def _check_rate_limit(self, rate_key: str, max_count: int) -> bool:
        """CAS-increment the rate counter. Returns ``True`` if limit exceeded."""
        assert self.store is not None
        for _ in range(10):
            current = self.store.read(rate_key)
            count = int(current.get("count", 0)) if isinstance(current, dict) else 0
            if count >= max_count:
                return True
            new_val = {"count": count + 1}
            if self.store.compare_and_swap(rate_key, current, new_val):
                return False
        return True  # CAS kept losing — treat as exceeded

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _scan_records(self) -> list[tuple[str, dict[str, Any]]]:
        # Uses an indexed B-tree range scan via Store.items(prefix=TASK_PREFIX)
        # when the store supports it (LazyBridge >= 0.9.1), giving O(M) in the
        # number of matching task keys rather than O(N) total keyspace.
        if self.store is None:
            return []
        if hasattr(self.store, "items"):
            return [(k, v) for k, v in self.store.items(prefix=store_keys.TASK_PREFIX) if isinstance(v, dict)]
        # Legacy fallback for stores without items(prefix=).
        out: list[tuple[str, dict[str, Any]]] = []
        for key in list(self.store.keys()):
            if not key.startswith(store_keys.TASK_PREFIX):
                continue
            raw = self.store.read(key)
            if isinstance(raw, dict):
                out.append((key, raw))
        return out

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        session = self.session
        if session is None:
            return
        try:
            session.emit(cast("EventType", event), payload)
        except Exception:
            pass


def _run_sync(coro: Any) -> Any:
    """Run a coroutine to completion from synchronous code.

    Mirrors lazybridge's ``Agent.__call__`` bridge: run on a fresh loop when
    there's no loop running, and offload to a worker thread when called from
    inside one (so a sync call from an async app doesn't explode)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _status_for_decision(decision: PolicyDecision) -> tuple[PulseStatus, str]:
    """Map a policy decision to an initial record status and the TickReport
    counter to bump."""
    if decision == PolicyDecision.ALLOW:
        return "scheduled", "scheduled"
    if decision in (PolicyDecision.QUEUE_FOR_REVIEW, PolicyDecision.REQUIRE_OWNER_CONFIRMATION):
        return "awaiting_review", "queued_for_review"
    return "rejected", "rejected"


def _finalize(started: PulseRecord, env: Any, now: datetime) -> PulseRecord:
    cost = _total_cost(env)
    if env.ok:
        return started.model_copy(
            update={
                "status": "completed",
                "completed_at": now,
                "worker_text": env.text(),
                "cost_usd": cost,
            }
        )
    err = env.error
    # A guard rejection is a *policy* outcome, not a crash — record it as
    # ``rejected`` so it reads distinctly from an engine failure.
    status = "rejected" if (err is not None and err.type == "GuardBlocked") else "failed"
    return started.model_copy(
        update={
            "status": status,
            "completed_at": now,
            "error": err.message if err is not None else "unknown error",
            "cost_usd": cost,
        }
    )


def _total_cost(env: Any) -> float:
    # ``nested_cost_usd`` aggregates agent-as-tool / Plan sub-agent spend, so a
    # Plan-engine task reports its full pipeline cost, not just the outer call.
    meta = getattr(env, "metadata", None)
    if meta is None:
        return 0.0
    return float(getattr(meta, "cost_usd", 0.0)) + float(getattr(meta, "nested_cost_usd", 0.0))


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None
