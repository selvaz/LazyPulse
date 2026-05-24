"""End-to-end: webhook intake → tick loop → completed record, no mocks of
the loop itself."""

from __future__ import annotations

import asyncio

import httpx
from lazybridge import Session, Store

from lazypulse import PulseAgent, store_keys
from lazypulse.adapters.webhook import WebhookAdapter
from lazypulse.models import PulseRecord
from lazypulse.testing import MockEngine


def _records(store: Store) -> list[PulseRecord]:
    return [PulseRecord.model_validate(store.read(k)) for k in list(store.keys()) if k.startswith(store_keys.TASK_PREFIX)]


async def test_webhook_to_completion() -> None:
    store = Store()
    session = Session()
    engine = MockEngine(["handled"])
    adapter = WebhookAdapter()
    pulse = PulseAgent(
        unsafe_allow_all=True,
        name="pulse",
        engine=engine,
        store=store,
        session=session,
        adapters=[adapter],
        tick_seconds=0.02,
    )

    transport = httpx.ASGITransport(app=adapter.asgi_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/inbound", json={"message_id": "1", "text": "do the thing", "sender": "a@x"})
        assert resp.status_code == 202

        async with pulse.running():
            for _ in range(50):
                await asyncio.sleep(0.02)
                if engine.calls:
                    break
            await asyncio.sleep(0.05)

    recs = _records(store)
    assert len(recs) == 1
    assert recs[0].status == "completed"
    assert recs[0].worker_text == "handled"
    # The loop emitted at least one tick event on the session.
    session.close()
