"""Gmail **push** adapter — event-driven intake instead of mailbox polling.

Where :class:`~lazypulse.adapters.gmail.inbox.GmailInbox` re-lists the
mailbox every tick, :class:`GmailPushInbox` waits for Gmail to say
"something changed":

1. ``users.watch`` is armed on the mailbox (and re-armed before its ≤7-day
   expiry), pointing Gmail at a Cloud Pub/Sub topic you own.
2. Pub/Sub **push-delivers** each notification to this adapter's HTTP
   endpoint. The handler only flips an in-memory "mailbox changed" flag —
   it never calls Gmail and acks immediately.
3. The next ``drain()`` makes **one** quota-cheap ``users.history.list``
   call from the persisted cursor (``store_keys.LAST_HISTORY``), fetches
   only the new message ids, and emits them through the same
   authentication-aware conversion as the polling inbox.

Between events the adapter makes **zero Gmail API calls** (plus an
optional low-frequency safety resync, since Pub/Sub delivery is
at-least-once but not guaranteed-forever). This is the configuration to
run when you're worried about API quota: steady-state load is one history
call per email received.

At-least-once contract: the history cursor advances only after every
message id in the previous batch has its central ``store_keys.EVENT``
marker — until then ``drain()`` re-emits the unrecorded ids and leaves
the cursor untouched, so a crash between drain and record cannot lose
mail. Central dedupe still makes each message at most one task.

Endpoint security: set ``shared_token`` and configure the Pub/Sub push
subscription URL as ``https://host/gmail/push?token=<value>`` —
requests with a missing/wrong token get ``403`` (Pub/Sub will retry, so
a misconfiguration stays visible instead of silently dropping mail).
Malformed bodies are acked (``204``) and ignored to avoid poison-message
retry storms. Bind ``127.0.0.1`` and expose via a TLS reverse proxy,
exactly like ``WebhookAdapter``.

GCP setup (one-time): create a Pub/Sub topic, grant
``gmail-api-push@system.gserviceaccount.com`` the *Pub/Sub Publisher*
role on it, and create a push subscription targeting this endpoint.
Requires the ``webhook`` extra for the HTTP server pieces.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lazytools.connectors.gmail.client import GmailHistoryExpired

from lazypulse import store_keys
from lazypulse.adapters.gmail.inbox import GmailInbox, GmailInboxConfig
from lazypulse.models import InboundMessage

if TYPE_CHECKING:
    from lazybridge import Session, Store
    from lazytools.connectors.gmail.client import GmailService
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response


#: History ids fetched per drain. The client's cursor never advances past
#: the last returned message, so a burst larger than this is drained over
#: consecutive ticks (the adapter re-arms its own notified flag while a
#: batch comes back full) — bounded work per tick, no mail skipped.
_HISTORY_BATCH = 100


@dataclass
class GmailPushConfig(GmailInboxConfig):
    """Configuration for :class:`GmailPushInbox`.

    Inherits every :class:`GmailInboxConfig` field (account, trusted
    authserv-id, default action, ...); ``query``/``max_results`` are unused
    by the push path (history sync replaces query polling).
    """

    #: Pub/Sub topic for ``users.watch`` (``projects/<p>/topics/<t>``).
    #: When set, the adapter arms the watch itself and re-arms it before
    #: expiry. Leave ``None`` if you arm/renew the watch out-of-band.
    topic_name: str | None = None
    #: Shared secret required as ``?token=`` on the push endpoint.
    shared_token: str | None = None
    #: HTTP endpoint the Pub/Sub push subscription targets.
    host: str = "127.0.0.1"
    port: int = 8100
    path: str = "/gmail/push"
    #: Re-arm the watch when less than this many seconds remain before its
    #: expiration (Gmail expires watches after at most 7 days).
    renew_margin_seconds: float = 86_400.0
    #: Safety net: run one history sync if no push notification has been
    #: processed for this long (Pub/Sub is reliable, not infallible).
    #: ``None`` disables the resync entirely.
    idle_resync_seconds: float | None = 900.0


class GmailPushInbox(GmailInbox):
    """An :class:`~lazypulse.Adapter` fed by Gmail push notifications.

    Reuses :class:`GmailInbox`'s authentication-results parsing and
    ``InboundMessage`` conversion; only the *discovery* of new mail differs
    (history cursor + push flag instead of query polling).
    """

    def __init__(
        self,
        client: GmailService,
        config: GmailPushConfig,
        *,
        name: str = "gmail",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(client, config, name=name)
        self._push_config = config
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        #: Flipped by the HTTP handler; consumed (reset) at the start of a
        #: sync so a notification landing mid-sync triggers the next drain.
        self._notified = True  # first drain always syncs (catch-up after restart)
        self._last_sync_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # Adapter protocol
    # ------------------------------------------------------------------ #
    async def drain(self, *, store: Store, session: Session | None = None) -> list[InboundMessage]:
        cfg = self._push_config
        key = store_keys.LAST_HISTORY.format(account=cfg.account)
        state: dict[str, Any] = store.read(key) or {}
        now = self._clock()

        if cfg.topic_name is not None:
            self._ensure_watch(state, now)
            # _ensure_watch mutates state in place; persist below with the
            # rest of the changes (single write per drain keeps Store churn
            # at one key).

        # Settle the previous batch: the cursor only advances once every id
        # we emitted has been durably recorded by the PulseAgent.
        if state.get("pending_history_id"):
            unrecorded = [mid for mid in state.get("pending_ids", []) if store.read(store_keys.event_key(mid)) is None]
            if unrecorded:
                store.write(key, state)
                return [self._to_inbound(mid, self._client.get_message(mid)) for mid in unrecorded]
            state["history_id"] = state.pop("pending_history_id")
            state.pop("pending_ids", None)

        # First run: anchor the cursor at "now" — only future mail is
        # event-driven. (Backfill, if wanted, is the polling inbox's job.)
        # Anchoring counts as the initial sync: consume the notified flag
        # and stamp the sync time so an idle mailbox stays at zero calls.
        if not state.get("history_id"):
            state["history_id"] = self._client.get_history_id()
            store.write(key, state)
            self._notified = False
            self._last_sync_at = now
            return []

        if not self._should_sync(now):
            store.write(key, state)
            return []
        self._notified = False  # consume the flag before listing (see __init__)

        try:
            ids, new_cursor = self._client.list_history_message_ids(
                start_history_id=str(state["history_id"]), max_results=_HISTORY_BATCH
            )
        except GmailHistoryExpired:
            # Cursor older than Gmail's retention window (e.g. the daemon
            # was down for a week+). Resync forward; the gap is not
            # recoverable through the history API.
            warnings.warn(
                f"GmailPushInbox({cfg.account}): history cursor expired; "
                "resyncing to the current mailbox state. Mail that arrived "
                "while the cursor was expired is NOT replayed — check the "
                "mailbox manually if the downtime mattered.",
                UserWarning,
                stacklevel=2,
            )
            state["history_id"] = self._client.get_history_id()
            state.pop("pending_history_id", None)
            state.pop("pending_ids", None)
            store.write(key, state)
            self._last_sync_at = now
            return []

        self._last_sync_at = now
        # A capped batch means more history is waiting behind the cursor
        # (the client guarantees the cursor stops at the last returned
        # message). Re-arm the notified flag so the backlog keeps draining
        # on subsequent ticks instead of waiting for the idle resync.
        if len(ids) >= _HISTORY_BATCH:
            self._notified = True
        fresh = [mid for mid in ids if store.read(store_keys.event_key(mid)) is None]
        if new_cursor != str(state["history_id"]) or fresh:
            state["pending_ids"] = fresh
            state["pending_history_id"] = new_cursor
        store.write(key, state)
        return [self._to_inbound(mid, self._client.get_message(mid)) for mid in fresh]

    # ------------------------------------------------------------------ #
    # Watch lifecycle
    # ------------------------------------------------------------------ #
    def _ensure_watch(self, state: dict[str, Any], now: datetime) -> None:
        cfg = self._push_config
        expiration_ms = state.get("watch_expiration_ms")
        if expiration_ms is not None:
            remaining = expiration_ms / 1000.0 - now.timestamp()
            if remaining > cfg.renew_margin_seconds:
                return
        assert cfg.topic_name is not None  # guarded by caller
        resp = self._client.watch(topic_name=cfg.topic_name)
        try:
            state["watch_expiration_ms"] = int(resp.get("expiration", 0))
        except (TypeError, ValueError):
            state["watch_expiration_ms"] = 0
        # A fresh watch response carries the current cursor — adopt it only
        # when we have none, so an active incremental chain is never reset.
        if not state.get("history_id") and resp.get("historyId"):
            state["history_id"] = str(resp["historyId"])

    def _should_sync(self, now: datetime) -> bool:
        if self._notified:
            return True
        idle = self._push_config.idle_resync_seconds
        if idle is None:
            return False
        if self._last_sync_at is None:
            return True
        return (now - self._last_sync_at).total_seconds() >= idle

    # ------------------------------------------------------------------ #
    # ASGI — the Pub/Sub push endpoint
    # ------------------------------------------------------------------ #
    def asgi_app(self) -> Starlette:
        """Starlette app exposing the push endpoint (requires the
        ``webhook`` extra), mountable in an existing ASGI application."""
        try:
            from starlette.applications import Starlette
            from starlette.routing import Route
        except ImportError as exc:  # pragma: no cover — exercised without the extra
            raise ImportError(
                "GmailPushInbox.asgi_app requires the 'webhook' extra "
                "(pip install 'lazypulse[webhook]') for the HTTP server pieces."
            ) from exc
        return Starlette(routes=[Route(self._push_config.path, self._handle, methods=["POST"])])

    def serve(self) -> None:  # pragma: no cover — runs a blocking server
        """Run a standalone uvicorn server on ``host:port`` (blocking)."""
        import uvicorn

        uvicorn.run(self.asgi_app(), host=self._push_config.host, port=self._push_config.port)

    async def _handle(self, request: Request) -> Response:
        from starlette.responses import JSONResponse, Response

        cfg = self._push_config
        if cfg.shared_token is not None:
            provided = request.query_params.get("token", "")
            if not hmac.compare_digest(cfg.shared_token, provided):
                # Non-2xx → Pub/Sub keeps retrying, so a token mismatch is
                # loud (visible in subscription metrics) instead of lost.
                return JSONResponse({"error": "invalid token"}, status_code=403)

        # Everything below acks (204) regardless of payload quality: Pub/Sub
        # redelivers nacked messages forever, and a malformed body will not
        # become well-formed on retry. The notification content is advisory
        # anyway — drain() trusts only the persisted cursor + history API.
        notified_account: str | None = None
        try:
            envelope = json.loads(await request.body())
            data_b64 = envelope["message"]["data"]
            payload = json.loads(base64.b64decode(data_b64))
            notified_account = payload.get("emailAddress")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, binascii.Error):
            return Response(status_code=204)

        if notified_account and notified_account != cfg.account:
            # A topic shared across mailboxes (or a spoofed body): ignore —
            # never sync on someone else's notification.
            return Response(status_code=204)

        self._notified = True
        return Response(status_code=204)
