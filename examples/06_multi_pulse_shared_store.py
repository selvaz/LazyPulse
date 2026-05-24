"""Two PulseAgents, one shared Store.

A common pattern: one agent polls Gmail, another takes webhooks, and both
write task lifecycle into the same Store. The CAS-based scheduled->running
claim guarantees no task is ever run twice, even when both agents tick at the
same time.

Runs offline with mock engines/adapters, fully synchronous.

    python examples/06_multi_pulse_shared_store.py
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import InboundMessage, PulseAgent, PulseRecord, store_keys
from lazypulse.testing import MockAdapter, MockEngine


def _msg(source: str, mid: str, text: str) -> InboundMessage:
    return InboundMessage(source=source, message_id=mid, received_at=datetime.now(UTC), text=text)


def main() -> None:
    store = Store()  # the single shared blackboard

    gmail_pulse = PulseAgent(
        name="gmail-pulse",
        engine=MockEngine(["handled email"]),
        store=store,
        adapters=[MockAdapter([_msg("gmail", "g1", "reply to the client")])],
        unsafe_allow_all=True,
        tick_seconds=0.05,
    )
    webhook_pulse = PulseAgent(
        name="webhook-pulse",
        engine=MockEngine(["handled webhook"]),
        store=store,
        adapters=[MockAdapter([_msg("webhook", "w1", "deploy finished, summarise logs")])],
        unsafe_allow_all=True,
        tick_seconds=0.05,
    )

    with gmail_pulse.running(), webhook_pulse.running():
        time.sleep(0.3)

    for key in list(store.keys()):
        if key.startswith(store_keys.TASK_PREFIX):
            rec = PulseRecord.model_validate(store.read(key))
            print(f"[{rec.source_event_id}] status={rec.status} output={rec.worker_text!r}")


if __name__ == "__main__":
    main()
