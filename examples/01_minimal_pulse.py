"""Minimal PulseAgent: a mock engine, a mock adapter — all synchronous.

No asyncio, no await, no event loop to manage — same zero-boilerplate feel as
lazybridge. Swap MockEngine for ``LLMEngine("claude-opus-4-8")`` and
MockAdapter for a real adapter to go live.

    python examples/01_minimal_pulse.py
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import InboundMessage, PulseAgent, PulseRecord, store_keys
from lazypulse.testing import MockAdapter, MockEngine


def main() -> None:
    store = Store()
    pulse = PulseAgent(
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
        unsafe_allow_all=True,  # dev only — pass policy=... in production
        tick_seconds=0.05,
    )

    with pulse.running():  # background loop; stops on block exit
        time.sleep(0.3)

    for key in list(store.keys()):
        if key.startswith(store_keys.TASK_PREFIX):
            rec = PulseRecord.model_validate(store.read(key))
            print(f"task {rec.task_id[:8]}  status={rec.status}  output={rec.worker_text!r}")


if __name__ == "__main__":
    main()
