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
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from lazybridge import Agent

from lazypulse import store_keys
from lazypulse.models import (
    ActionClass,
    Identity,
    InboundMessage,
    PolicyDecision,
    PulseRecord,
    TickReport,
    TrustLevel,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from lazybridge import EventType

    from lazypulse.adapters.base import Adapter
    from lazypulse.policy import PulsePolicy

#: Hard cap on automatic restarts of a stale ``running`` record before it is
#: marked ``failed``. Prevents a poison task from being retried forever.
_MAX_RESTARTS = 3


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
        unsafe_allow_all: bool = False,
        clock: Callable[[], datetime] | None = None,
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
        # from a crashed process and is recovered. Default is generous (at
        # least 5 minutes) so a legitimately slow worker is not re-run; tune
        # down for fast tasks, up for long ones. See ``_recover_stale``.
        self._stale_after = stale_after if stale_after is not None else max(tick_seconds * 60, 300.0)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._tick_task: asyncio.Task[None] | None = None
        # Created here (not as a parameter default) so each PulseAgent gets
        # its own semaphore rather than sharing one across instances.
        self._sema = asyncio.Semaphore(max_concurrent_inbound)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Start the background tick loop. Returns immediately."""
        if self._tick_task is not None:
            raise RuntimeError(f"PulseAgent {self.name!r} already started")
        self._tick_task = asyncio.create_task(self._tick_loop(), name=f"pulse_loop[{self.name}]")

    async def stop(self) -> None:
        """Stop the tick loop. Tolerates being called without a prior start
        and being called twice."""
        task = self._tick_task
        if task is None:
            return
        self._tick_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def is_running(self) -> bool:
        """True while the background tick loop is active."""
        return self._tick_task is not None and not self._tick_task.done()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Context manager that runs the loop for the duration of the block::

        async with pulse.running():
            await asyncio.Event().wait()   # serve forever
        """
        await self.start()
        try:
            yield
        finally:
            await self.stop()

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

    # ------------------------------------------------------------------ #
    # The tick
    # ------------------------------------------------------------------ #
    async def tick_once(self) -> TickReport:
        """Execute a single beat. Exposed for deterministic testing.

        Order matters: recover crashed tasks first, then intake new inbound
        (which may become due immediately), then run everything due.
        """
        now = self._clock()
        report = TickReport(at=now)

        self._recover_stale(now, report)

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
        report.due = len(due)
        if due:
            await self._run_due(due, report)

        # Only emit when something happened — a quiet tick every ``tick_seconds``
        # would otherwise flood the Session of a long-running agent.
        if report.drained or report.due or report.recovered:
            self._emit("pulse.tick", report.model_dump(mode="json"))
        return report

    async def _tick_loop(self) -> None:
        """Loop forever until cancelled. An exception in a tick is logged to
        the Session but never escapes — the loop must outlive bad ticks."""
        while True:
            try:
                await self.tick_once()
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
        status, tally = _status_for_decision(decision)
        record = PulseRecord(
            text=msg.text,
            status=status,
            created_at=now,
            run_at=now,
            source_event_id=msg.message_id,
            identity=identity,
            action_class=msg.requested_action,
            decision=decision,
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
        results = await asyncio.gather(*(self._run_one(key, raw) for key, raw in due), return_exceptions=True)
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

    async def _run_one(self, key: str, expected: dict[str, Any]) -> str | None:
        """Claim one scheduled record via CAS, run it, and persist the result.

        Returns the terminal status, or ``None`` when a CAS lost — either the
        initial scheduled→running claim (another ticker owns it) or the final
        write (crash recovery reset the record mid-run). Persisting the result
        via CAS against our own ``running`` snapshot means a slow worker can
        never clobber a record that recovery has already taken over."""
        async with self._sema:
            started = PulseRecord.model_validate(expected).model_copy(
                update={"status": "running", "started_at": self._clock()}
            )
            running_dict = started.model_dump(mode="json")
            assert self.store is not None  # guaranteed by the __init__ check
            if not self.store.compare_and_swap(key, expected, running_dict):
                return None  # lost the race — someone else owns this task

            try:
                env = await self.run(started.text)
            except Exception as exc:
                final = started.model_copy(
                    update={"status": "failed", "completed_at": self._clock(), "error": f"{type(exc).__name__}: {exc}"}
                )
            else:
                final = _finalize(started, env, self._clock())

            if not self.store.compare_and_swap(key, running_dict, final.model_dump(mode="json")):
                # Recovery (or another process) took the record over while we
                # ran. Don't resurrect it — the worker's side effects already
                # happened, but the ledger is no longer ours to write.
                self._emit("pulse.write_conflict", {"task_id": started.task_id, "would_be_status": final.status})
                return None
            return final.status

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

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _scan_records(self) -> list[tuple[str, dict[str, Any]]]:
        if self.store is None:
            return []
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


def _status_for_decision(decision: PolicyDecision) -> tuple[str, str]:
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
