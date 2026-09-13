"""Durable metadata for a supervised fleet's registered agents.

Promoted from LazyCEO's specialist registry because declared agent identity,
schedule, lifecycle state, and opaque framework configuration are generic
fleet infrastructure.  The registry deliberately does not infer live process
state: durable intent and momentary OS liveness are different facts, and
callers need to see mismatches between them rather than have one hide the
other.

The default Store namespace is neutral.  A LazyCEO caller migrating an
existing on-disk Store can pass ``prefix="ceo:specialist:"`` explicitly.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from lazybridge import Store
from pydantic import BaseModel, Field

#: Neutral default for registered agents. Existing LazyCEO stores can opt
#: back into ``"ceo:specialist:"`` through :class:`AgentRegistry`'s prefix.
DEFAULT_AGENT_PREFIX = "fleet:agent:"


class AgentRecord(BaseModel):
    """One agent's durable definition and declared lifecycle state.

    ``config`` stays opaque on purpose: prompts, tool choices, and other
    framework-specific construction details belong to the caller.  This
    generic registry only preserves them.
    """

    name: str
    function: str
    tick_cron: str
    status: Literal["active", "stopped"]
    created_at: datetime
    stopped_at: datetime | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class AgentRegistry:
    """Small Store-backed registry shared by fleet lifecycle adapters."""

    def __init__(self, store: Store, *, prefix: str = DEFAULT_AGENT_PREFIX) -> None:
        self._store = store
        self._prefix = prefix

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    def register(
        self,
        *,
        name: str,
        function: str,
        tick_cron: str,
        config: dict[str, Any] | None = None,
    ) -> AgentRecord:
        """Write a new active record and return the validated model."""
        record = AgentRecord(
            name=name,
            function=function,
            tick_cron=tick_cron,
            config={} if config is None else config,
            status="active",
            created_at=datetime.now(UTC),
        )
        self._store.write(self._key(name), record.model_dump(mode="json"))
        return record

    def register_and_launch(
        self,
        *,
        name: str,
        function: str,
        tick_cron: str,
        config: dict[str, Any] | None,
        spawn: Callable[[], subprocess.Popen[Any]],
    ) -> subprocess.Popen[Any]:
        """Launch first, then register; the safe combined lifecycle primitive.

        A registry write before ``Popen`` leaves a declared-active orphan when
        process creation fails.  Calling ``spawn`` first is load-bearing: its
        exception propagates unchanged and no durable record is created.
        Found by Codex review during the LazyCEO extraction.

        The reverse gap matters too: if ``spawn`` succeeds but the
        subsequent registry write then fails (Store locked, disk full,
        ...), the caller's exception handler never sees a ``Popen`` to
        clean up -- the child would otherwise keep running, live but
        completely untracked. On a registration failure the just-spawned
        process is terminated before re-raising, so a failed
        ``register_and_launch`` call never leaves an orphan in EITHER
        direction. Found by Codex review before this ever shipped.
        """
        process = spawn()
        try:
            self.register(name=name, function=function, tick_cron=tick_cron, config=config)
        except Exception:
            # terminate() alone is not a guarantee: a child that traps or
            # ignores the signal outlives it, and swallowing the resulting
            # TimeoutExpired would re-raise while leaving exactly the live,
            # untracked orphan this rollback exists to prevent. Escalate to
            # kill() the same way the supervisor's own shutdown path does.
            # Found by Codex review before this ever shipped.
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=10)
            raise
        return process

    def get(self, name: str) -> AgentRecord | None:
        raw = self._store.read(self._key(name))
        return AgentRecord.model_validate(raw) if isinstance(raw, dict) else None

    def list(self) -> list[AgentRecord]:
        """Return every registered agent oldest first, including stopped ones.

        A stopped agent stays listed, not deleted, so its function and history
        remain visible.  Durable declared state is metadata, not a process
        cache.
        """
        records = [
            AgentRecord.model_validate(raw)
            for _key, raw in self._store.items(prefix=self._prefix)
            if isinstance(raw, dict)
        ]
        records.sort(key=lambda record: record.created_at)
        return records

    def update(
        self,
        name: str,
        *,
        function: str | None = None,
        tick_cron: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> AgentRecord | None:
        """Read-modify-write ANY subset of an agent's declared config fields.

        ``None`` (the default) on any parameter means "leave this field
        exactly as it is", not "clear it". Updating nothing at all is a
        valid, meaningful call -- a caller bouncing a process without a
        config change -- and returns the record unchanged rather than
        treating it as an error.

        Uses ``compare_and_swap``, not the bare write ``mark_stopped`` uses:
        a stop is idempotent so last-writer-wins costs nothing, but a lost
        concurrent edit HERE would silently discard one of two different
        field changes landing close together (a stale prompt in ``config``,
        or a ``tick_cron`` that quietly reverts). Exactly ONE attempt, no
        retry loop -- returning ``None`` on a lost race (the same value as
        "not registered"; a caller that just confirmed the record exists
        knows which case applies) beats retrying against a moving target.

        Deliberately leaves ``status``/``created_at``/``stopped_at`` alone:
        editing config is not, by itself, stopping or starting anything.
        The process lifecycle around a config change belongs to the caller
        (see :meth:`mark_active` for the other half).
        """
        raw = self._store.read(self._key(name))
        if not isinstance(raw, dict):
            return None
        record = AgentRecord.model_validate(raw)
        updates = {
            key: value
            for key, value in {"function": function, "tick_cron": tick_cron, "config": config}.items()
            if value is not None
        }
        if not updates:
            return record
        updated = record.model_copy(update=updates)
        if not self._store.compare_and_swap(self._key(name), raw, updated.model_dump(mode="json")):
            return None
        return updated

    def mark_stopped(self, name: str) -> bool:
        """Mark an agent stopped; return false only when it is unregistered.

        Stopping an already-stopped record remains successful.  Last-writer
        wins is acceptable for this deliberately idempotent lifecycle mark.
        """
        record = self.get(name)
        if record is None:
            return False
        updated = record.model_copy(update={"status": "stopped", "stopped_at": datetime.now(UTC)})
        self._store.write(self._key(name), updated.model_dump(mode="json"))
        return True

    def mark_active(self, name: str) -> bool:
        """The other direction of :meth:`mark_stopped`, for a confirmed respawn.

        Call this ONLY after a replacement process is actually confirmed
        started, never as part of a plain field edit (:meth:`update` leaves
        ``status`` alone for exactly that case). Without it, restarting an
        agent that had been stopped brings its process back while the
        registry still declares it "stopped" -- a permanent, self-inflicted
        mismatch between declared and observed state that fleet telemetry
        would keep reporting forever for an agent that is, correctly,
        running.
        """
        record = self.get(name)
        if record is None:
            return False
        updated = record.model_copy(update={"status": "active", "stopped_at": None})
        self._store.write(self._key(name), updated.model_dump(mode="json"))
        return True


__all__ = ["DEFAULT_AGENT_PREFIX", "AgentRecord", "AgentRegistry"]
