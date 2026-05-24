"""Minimal PulseAgent: a mock engine, a mock adapter, the running() loop.

Runs with no credentials and no network. Swap MockEngine for
``LLMEngine("claude-opus-4-7")`` and MockAdapter for a real adapter to go live.

    python examples/01_minimal_pulse.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.models import PulseRecord
from lazypulse.testing import MockAdapter, MockEngine


async def main() -> None:
    store = Store()
    pulse = PulseAgent(
        unsafe_allow_all=True,
        name="pulse",
        engine=MockEngine(["I handled your request."]),
        store=store,
        adapters=[
            MockAdapter(
                [
                    InboundMessage(
                        source="mock",
                        message_id="1",
                        received_at=datetime.now(UTC),
                        text="summarise my unread mail",
                    )
                ]
            )
        ],
        tick_seconds=0.05,
    )

    async with pulse.running():
        await asyncio.sleep(0.3)  # let a few ticks run

    for key in list(store.keys()):
        if key.startswith(store_keys.TASK_PREFIX):
            rec = PulseRecord.model_validate(store.read(key))
            print(f"task {rec.task_id[:8]}  status={rec.status}  output={rec.worker_text!r}")


if __name__ == "__main__":
    asyncio.run(main())
