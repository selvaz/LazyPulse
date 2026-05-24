"""Two PulseAgents, one shared Store.

A common pattern: one agent polls Gmail, another takes webhooks, and both
write task lifecycle into the same Store. The CAS-based scheduled->running
claim guarantees no task is ever run twice, even when both agents tick at the
same time. A single reviewer client can then drain reviews from either.

Runs offline with mock engines/adapters.

    python examples/06_multi_pulse_shared_store.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.models import PulseRecord
from lazypulse.testing import MockAdapter, MockEngine


def _msg(source: str, mid: str, text: str) -> InboundMessage:
    return InboundMessage(source=source, message_id=mid, received_at=datetime.now(UTC), text=text)


async def main() -> None:
    store = Store()  # the single shared blackboard

    gmail_pulse = PulseAgent(
        name="gmail-pulse",
        engine=MockEngine(["handled email"]),
        store=store,
        adapters=[MockAdapter([_msg("gmail", "g1", "reply to the client")])],
        tick_seconds=0.05,
    )
    webhook_pulse = PulseAgent(
        name="webhook-pulse",
        engine=MockEngine(["handled webhook"]),
        store=store,
        adapters=[MockAdapter([_msg("webhook", "w1", "deploy finished, summarise logs")])],
        tick_seconds=0.05,
    )

    async with gmail_pulse.running(), webhook_pulse.running():
        await asyncio.sleep(0.3)

    for key in list(store.keys()):
        if key.startswith(store_keys.TASK_PREFIX):
            rec = PulseRecord.model_validate(store.read(key))
            print(f"[{rec.source_event_id}] status={rec.status} output={rec.worker_text!r}")


if __name__ == "__main__":
    asyncio.run(main())
