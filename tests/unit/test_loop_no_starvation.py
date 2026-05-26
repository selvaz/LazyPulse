"""The background tick loop must not let a slow worker stall other work.

A long-running task (or one parked in human review) used to block the whole
loop because each tick awaited every due task to completion. The loop now
dispatches due tasks as background asyncio tasks, so intake and other due work
keep flowing while a slow worker runs.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from lazybridge import Store

from lazypulse import PulseAgent
from lazypulse.testing import MockEngine


class _SlowFastEngine(MockEngine):
    """Tasks whose text contains 'slow' take a while; everything else is fast."""

    async def run(self, env: Any, **kwargs: Any) -> Any:
        if "slow" in (env.task or ""):
            await asyncio.sleep(1.0)
        return await super().run(env, **kwargs)


async def test_slow_task_does_not_starve_a_later_fast_task() -> None:
    store = Store()
    pulse = PulseAgent(
        name="p",
        engine=_SlowFastEngine(["done"]),
        store=store,
        tick_seconds=0.02,
        max_concurrent_inbound=4,
    )
    pulse.schedule("slow task")
    pulse.start()
    try:
        # Let the slow task get claimed and enter its 1.0s sleep.
        await asyncio.sleep(0.1)
        scheduled_at = time.monotonic()
        fast_id = pulse.schedule("fast task")

        rb: dict[str, Any] | None = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            rb = store.read(f"pulse:task:{fast_id}")
            if rb and rb["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        elapsed = time.monotonic() - scheduled_at
    finally:
        pulse.stop()

    assert rb is not None and rb["status"] == "completed"
    # If the loop were still blocking on the slow task, the fast task could only
    # complete after the slow task's ~1.0s run. Background dispatch lets it
    # finish in a couple of ticks.
    assert elapsed < 0.6, f"fast task was starved by the slow task: completed after {elapsed:.2f}s"
