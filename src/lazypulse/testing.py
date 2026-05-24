"""Deterministic test doubles for LazyPulse.

These let you exercise a PulseAgent end-to-end with no event-loop timing
games and no real LLM calls:

* :class:`FakeClock` — a controllable ``Callable[[], datetime]`` to pass as
  ``clock=`` so ``run_at`` / staleness logic is fully deterministic.
* :class:`MockEngine` — a duck-typed lazybridge engine that returns canned
  payloads and tracks peak concurrency (for ``max_concurrent_inbound``).
* :class:`MockAdapter` — an :class:`~lazypulse.Adapter` that emits a fixed
  batch of messages once, then nothing (idempotent re-drain).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from lazybridge import Envelope

from lazypulse.models import InboundMessage


class FakeClock:
    """A deterministic clock. Call the instance to read the current time;
    :meth:`advance` to move it forward."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class MockEngine:
    """A minimal lazybridge engine for tests — no network, canned output.

    Satisfies the ``Engine`` protocol (``run`` / ``stream``). Tracks every
    call and the peak number of concurrent ``run`` invocations so tests can
    assert the PulseAgent's concurrency cap is honoured.
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        delay: float = 0.0,
        raises: BaseException | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        self._responses = list(responses) if responses else ["ok"]
        self._delay = delay
        self._raises = raises
        self._cost_usd = cost_usd
        self._agent_name: str | None = None
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = asyncio.Lock()

    async def run(
        self,
        env: Envelope[Any],
        *,
        tools: list[Any],
        output_type: type,
        memory: Any | None = None,
        session: Any | None = None,
        store: Any | None = None,
        plan_state: Any | None = None,
    ) -> Envelope[Any]:
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            idx = len(self.calls)
            self.calls.append(env.task or "")
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._raises is not None:
                raise self._raises
            reply = self._responses[idx % len(self._responses)]
            out: Envelope[Any] = Envelope.from_task(env.task or "")
            out.payload = reply
            out.metadata.cost_usd = self._cost_usd
            return out
        finally:
            async with self._lock:
                self.active -= 1

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        raise NotImplementedError
        yield  # pragma: no cover — makes this an async generator


class MockAdapter:
    """Emits a fixed batch of messages on the first drain, then nothing."""

    def __init__(self, messages: list[InboundMessage], *, name: str = "mock") -> None:
        self.name = name
        self._messages = list(messages)
        self._drained = False

    async def drain(self, *, store: Any, session: Any | None) -> list[InboundMessage]:
        if self._drained:
            return []
        self._drained = True
        return list(self._messages)
