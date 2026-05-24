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


async def test_store_bound_at_construction_protects_before_first_drain() -> None:
    # Two adapters sharing a store= bound up front: the second rejects a nonce
    # the first saw, with no drain ever called (closes the first-drain gap).
    store = Store()
    a1 = WebhookAdapter(store=store)
    t1 = httpx.ASGITransport(app=a1.asgi_app())
    async with httpx.AsyncClient(transport=t1, base_url="http://t") as c:
        first = await c.post("/inbound", json={"message_id": "1", "text": "x", "nonce": "n"})
    assert first.status_code == 202

    a2 = WebhookAdapter(store=store)
    t2 = httpx.ASGITransport(app=a2.asgi_app())
    async with httpx.AsyncClient(transport=t2, base_url="http://t") as c:
        second = await c.post("/inbound", json={"message_id": "2", "text": "y", "nonce": "n"})
    assert second.status_code == 409


def test_default_bind_host_is_loopback() -> None:
    assert WebhookAdapter().host == "127.0.0.1"


async def test_buffer_full_returns_503() -> None:
    adapter = WebhookAdapter(max_buffer_size=2)
    async with _client(adapter) as client:
        r1 = await client.post("/inbound", json={"message_id": "1", "text": "a"})
        r2 = await client.post("/inbound", json={"message_id": "2", "text": "b"})
        r3 = await client.post("/inbound", json={"message_id": "3", "text": "c"})
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r3.status_code == 503


async def test_buffer_full_does_not_burn_nonce() -> None:
    # A 503 (buffer full) must NOT consume the nonce: once the buffer drains,
    # a retry with the same nonce is accepted, not rejected as a replay.
    adapter = WebhookAdapter(max_buffer_size=1)
    store = Store()
    async with _client(adapter) as client:
        r1 = await client.post("/inbound", json={"message_id": "1", "text": "a", "nonce": "n1"})
        r2 = await client.post("/inbound", json={"message_id": "2", "text": "b", "nonce": "n2"})
        assert r1.status_code == 202
        assert r2.status_code == 503  # buffer full → n2 must stay unused
        await adapter.drain(store=store, session=None)  # frees the buffer
        r3 = await client.post("/inbound", json={"message_id": "2", "text": "b", "nonce": "n2"})
    assert r3.status_code == 202  # retry accepted — the nonce was never burned


async def test_seen_nonces_cleared_after_drain() -> None:
    # Nonces received before first drain are held in _seen_nonces;
    # after drain flushes them to the Store, the in-memory set is cleared.
    adapter = WebhookAdapter()
    async with _client(adapter) as client:
        await client.post("/inbound", json={"message_id": "1", "text": "x", "nonce": "pre-drain"})
    assert "pre-drain" in adapter._seen_nonces
    await adapter.drain(store=Store(), session=None)
    assert "pre-drain" not in adapter._seen_nonces
