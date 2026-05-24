"""HTTP intake adapter.

Exposes a single ``POST /inbound`` endpoint that turns an HTTP request into
an :class:`~lazypulse.models.InboundMessage`. Requests are buffered in
memory and handed to the PulseAgent on the next :meth:`WebhookAdapter.drain`.

Security posture:

* **HMAC** — when ``shared_secret`` is set, the body must carry a valid
  ``X-Pulse-Signature`` header (hex HMAC-SHA256 of the raw body). Missing or
  wrong signatures are rejected with ``401``.
* **Replay protection** — a request may carry a ``nonce``; a nonce seen
  before is rejected with ``409``. Seen nonces are persisted to the Store
  (under ``store_keys.WEBHOOK_NONCE``) so protection survives across restarts
  and is shared between processes on one Store. Pass ``store=`` to the
  constructor to enable this from the very first request; otherwise the Store
  is bound on the first ``drain`` and the in-memory set covers the gap.
* **Bind host** — :meth:`serve` binds ``127.0.0.1`` by default; expose it
  behind a reverse proxy rather than binding ``0.0.0.0`` directly.

Requires the ``webhook`` extra (``pip install 'lazypulse[webhook]'``).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lazypulse import store_keys
from lazypulse.models import ActionClass, InboundMessage

if TYPE_CHECKING:
    from lazybridge import Session, Store


class WebhookAdapter:
    """An :class:`~lazypulse.Adapter` backed by an HTTP POST endpoint."""

    def __init__(
        self,
        *,
        name: str = "webhook",
        shared_secret: str | None = None,
        host: str = "127.0.0.1",
        port: int = 8099,
        path: str = "/inbound",
        store: Store | None = None,
    ) -> None:
        self.name = name
        self.shared_secret = shared_secret
        self.host = host
        self.port = port
        self.path = path
        self._buffer: list[InboundMessage] = []
        self._seen_nonces: set[str] = set()
        self._pending_nonces: set[str] = set()
        # Bound on the first drain (or up front via store=) so the request
        # handler can consult the Store for replay protection that outlives the
        # process. Passing store= here closes the gap before the first drain.
        self._store: Store | None = store
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Adapter protocol
    # ------------------------------------------------------------------ #
    async def drain(self, *, store: Store, session: Session | None = None) -> list[InboundMessage]:
        async with self._lock:
            self._store = store
            messages = self._buffer
            self._buffer = []
            nonces = self._pending_nonces
            self._pending_nonces = set()
        # Flush nonces seen before the Store was bound.
        for nonce in nonces:
            store.write(store_keys.WEBHOOK_NONCE.format(nonce=nonce), {"seen_at": _now_iso()})
        return messages

    # ------------------------------------------------------------------ #
    # ASGI
    # ------------------------------------------------------------------ #
    def asgi_app(self) -> Starlette:
        """Return a Starlette app exposing the inbound endpoint, mountable in
        an existing ASGI application."""
        return Starlette(routes=[Route(self.path, self._handle, methods=["POST"])])

    def serve(self) -> None:  # pragma: no cover — runs a blocking server
        """Run a standalone uvicorn server on ``host:port`` (blocking)."""
        import uvicorn

        uvicorn.run(self.asgi_app(), host=self.host, port=self.port)

    async def _handle(self, request: Request) -> JSONResponse:
        raw = await request.body()

        if self.shared_secret is not None:
            signature = request.headers.get("X-Pulse-Signature", "")
            if not self._valid_signature(raw, signature):
                return JSONResponse({"error": "invalid signature"}, status_code=401)

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "malformed JSON"}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        message_id = data.get("message_id")
        text = data.get("text")
        if not message_id or not isinstance(text, str):
            return JSONResponse({"error": "message_id and text are required"}, status_code=400)

        nonce = data.get("nonce")
        if nonce is not None:
            nonce_key = store_keys.WEBHOOK_NONCE.format(nonce=nonce)
            async with self._lock:
                seen = nonce in self._seen_nonces or (
                    self._store is not None and self._store.read(nonce_key) is not None
                )
                if seen:
                    return JSONResponse({"error": "replay detected"}, status_code=409)
                self._seen_nonces.add(nonce)
                if self._store is not None:
                    self._store.write(nonce_key, {"seen_at": _now_iso()})
                else:
                    # Store not bound yet (no drain has run) — persist on the
                    # next drain instead.
                    self._pending_nonces.add(nonce)

        action = _parse_action(data.get("requested_action"))
        message = InboundMessage(
            source=self.name,
            message_id=str(message_id),
            received_at=datetime.now(UTC),
            sender_raw=data.get("sender"),
            text=text,
            requested_action=action,
            metadata=data.get("metadata") or {},
        )
        async with self._lock:
            self._buffer.append(message)
        return JSONResponse({"status": "queued", "message_id": message.message_id}, status_code=202)

    def _valid_signature(self, raw: bytes, provided: str) -> bool:
        assert self.shared_secret is not None
        expected = hmac.new(self.shared_secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided)


def _parse_action(value: Any) -> ActionClass:
    if value is None:
        return ActionClass.READ_PUBLIC
    try:
        return ActionClass(value)
    except ValueError:
        return ActionClass.READ_PUBLIC


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
