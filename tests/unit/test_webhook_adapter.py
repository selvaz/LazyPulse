"""WebhookAdapter: HTTP intake, HMAC, replay protection."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
from lazybridge import Store

from lazypulse import store_keys
from lazypulse.adapters.webhook import WebhookAdapter


def _client(adapter: WebhookAdapter) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=adapter.asgi_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_post_queues_message() -> None:
    adapter = WebhookAdapter()
    store = Store()
    async with _client(adapter) as client:
        resp = await client.post("/inbound", json={"message_id": "1", "text": "hello", "sender": "a@x"})
    assert resp.status_code == 202
    msgs = await adapter.drain(store=store, session=None)
    assert len(msgs) == 1
    assert msgs[0].text == "hello"
    assert msgs[0].sender_raw == "a@x"
    assert msgs[0].source == "webhook"


async def test_drain_is_idempotent() -> None:
    adapter = WebhookAdapter()
    store = Store()
    async with _client(adapter) as client:
        await client.post("/inbound", json={"message_id": "1", "text": "hi"})
    assert len(await adapter.drain(store=store, session=None)) == 1
    assert len(await adapter.drain(store=store, session=None)) == 0


async def test_malformed_json_rejected() -> None:
    adapter = WebhookAdapter()
    async with _client(adapter) as client:
        resp = await client.post("/inbound", content=b"{not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400


async def test_missing_required_fields_rejected() -> None:
    adapter = WebhookAdapter()
    async with _client(adapter) as client:
        resp = await client.post("/inbound", json={"text": "no id"})
    assert resp.status_code == 400


async def test_hmac_valid_signature_accepted() -> None:
    adapter = WebhookAdapter(shared_secret="topsecret")
    body = json.dumps({"message_id": "1", "text": "signed"}).encode()
    async with _client(adapter) as client:
        resp = await client.post(
            "/inbound", content=body, headers={"X-Pulse-Signature": _sign("topsecret", body)}
        )
    assert resp.status_code == 202


async def test_hmac_invalid_signature_rejected() -> None:
    adapter = WebhookAdapter(shared_secret="topsecret")
    body = json.dumps({"message_id": "1", "text": "signed"}).encode()
    async with _client(adapter) as client:
        resp = await client.post("/inbound", content=body, headers={"X-Pulse-Signature": "deadbeef"})
    assert resp.status_code == 401


async def test_hmac_missing_signature_rejected() -> None:
    adapter = WebhookAdapter(shared_secret="topsecret")
    async with _client(adapter) as client:
        resp = await client.post("/inbound", json={"message_id": "1", "text": "x"})
    assert resp.status_code == 401


async def test_nonce_replay_rejected() -> None:
    adapter = WebhookAdapter()
    async with _client(adapter) as client:
        first = await client.post("/inbound", json={"message_id": "1", "text": "x", "nonce": "n1"})
        second = await client.post("/inbound", json={"message_id": "2", "text": "y", "nonce": "n1"})
    assert first.status_code == 202
    assert second.status_code == 409


async def test_requested_action_propagates() -> None:
    adapter = WebhookAdapter()
    store = Store()
    async with _client(adapter) as client:
        await client.post("/inbound", json={"message_id": "1", "text": "delete it", "requested_action": "destructive"})
    msgs = await adapter.drain(store=store, session=None)
    assert msgs[0].requested_action.value == "destructive"


async def test_nonce_persisted_to_store_on_drain() -> None:
    adapter = WebhookAdapter()
    store = Store()
    async with _client(adapter) as client:
        await client.post("/inbound", json={"message_id": "1", "text": "x", "nonce": "abc"})
    await adapter.drain(store=store, session=None)
    assert store.read(store_keys.WEBHOOK_NONCE.format(nonce="abc")) is not None


async def test_nonce_replay_rejected_across_restart() -> None:
    # Simulate a process restart: a fresh adapter (empty in-memory set) bound
    # to the SAME persistent Store must still reject a nonce seen before.
    db_store = Store()  # shared store stands in for the persistent db
    a1 = WebhookAdapter()
    t1 = httpx.ASGITransport(app=a1.asgi_app())
    async with httpx.AsyncClient(transport=t1, base_url="http://t") as c:
        await c.post("/inbound", json={"message_id": "1", "text": "x", "nonce": "shared"})
    await a1.drain(store=db_store, session=None)  # persists the nonce

    a2 = WebhookAdapter()  # "restarted" — empty in-memory nonce set
    await a2.drain(store=db_store, session=None)  # bind the same store first
    t2 = httpx.ASGITransport(app=a2.asgi_app())
    async with httpx.AsyncClient(transport=t2, base_url="http://t") as c:
        resp = await c.post("/inbound", json={"message_id": "2", "text": "y", "nonce": "shared"})
    assert resp.status_code == 409


def test_default_bind_host_is_loopback() -> None:
    assert WebhookAdapter().host == "127.0.0.1"
