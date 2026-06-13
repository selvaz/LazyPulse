"""Outlook **local-desktop** polling adapter.

The simpler sibling of the Gmail adapters. Where ``GmailInbox`` polls Gmail's
cloud API (OAuth + quota) and ``GmailPushInbox`` needs Cloud Pub/Sub, a
re-armed ``watch`` and a public HTTPS endpoint, ``OutlookInbox`` polls the copy
of Outlook **already running and signed in** on the same Windows machine, over
COM. That means **no cloud credentials, no API quota, no push infrastructure
to set up** — the trade is that it only works where Outlook desktop runs, and
the PulseAgent must run on that machine.

Polling a *local* store has none of the rate-limit / account-suspension risk
of hammering a cloud mail API, so a plain per-tick poll is the right model
here — there is no push variant to reach for.

Behaviourally it mirrors ``GmailInbox`` exactly: at-least-once (a message is
re-emitted until the PulseAgent records its central ``store_keys.EVENT``
marker, so a crash between drain and record cannot lose mail), central dedupe
(each message becomes at most one task), and the **same** authentication-aware
conversion — the genuine top-most ``Authentication-Results`` header (lifted out
of the message's transport headers by the connector) is parsed with the same
``parse_authentication_results`` the Gmail path uses.

Depends only on the duck-typed
:class:`~lazytools.connectors.outlook.client.OutlookService`, so it imports
without the Outlook extra (or Windows) and is testable with a fake client.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

# parse_authentication_results is provider-agnostic RFC 8601 parsing; it lives
# in the gmail connector (its first home) and is reused verbatim here.
from lazytools.connectors.gmail.auth import parse_authentication_results
from lazytools.connectors.outlook.client import OutlookService

from lazypulse import store_keys
from lazypulse.models import ActionClass, InboundMessage

if TYPE_CHECKING:
    from lazybridge import Session, Store


@dataclass
class OutlookInboxConfig:
    """Configuration for :class:`OutlookInbox`."""

    account: str
    #: Outlook **Restrict** filter (DASL or ``"[Field] = 'value'"`` macro
    #: syntax). The default returns unread mail; set ``""`` / ``None`` to take
    #: the whole folder.
    query: str | None = "[Unread] = true"
    max_results: int = 25
    default_action: ActionClass = ActionClass.READ_PUBLIC
    #: Authserv-id that your receiving server stamps on the
    #: ``Authentication-Results`` header. When set, only a header whose leading
    #: authserv-id *exactly* equals this value is trusted (defends against a
    #: forged header carried inside the message). Unlike Gmail there is no
    #: single well-known value — it is your tenant's inbound mail host (e.g.
    #: your M365 domain). ``None`` disables pinning: the connector still takes
    #: the genuine *first* (server-prepended) header via first-wins, so a body
    #: forgery is ignored, but pinning is recommended defence-in-depth.
    trusted_authserv_id: str | None = None

    def __post_init__(self) -> None:
        if self.trusted_authserv_id is None:
            warnings.warn(
                "OutlookInboxConfig(trusted_authserv_id=None) disables authserv-id "
                "pinning. First-wins on the server-stamped header still rejects a "
                "body-forged Authentication-Results, but pin your inbound mail "
                "host (e.g. your M365 domain) for defence-in-depth.",
                UserWarning,
                stacklevel=2,
            )


class OutlookInbox:
    """An :class:`~lazypulse.Adapter` that polls a local Outlook folder."""

    def __init__(self, client: OutlookService, config: OutlookInboxConfig, *, name: str = "outlook") -> None:
        self.name = name
        self._client = client
        self._config = config

    async def drain(self, *, store: Store, session: Session | None = None) -> list[InboundMessage]:
        # At-least-once: a message is skipped only once the PulseAgent has
        # durably recorded it (its central EVENT marker exists). Until then we
        # re-emit on every poll, so a crash between drain and record-write
        # cannot lose the message. Central dedupe means it still becomes at
        # most one task.
        out: list[InboundMessage] = []
        for message_id in self._client.list_message_ids(
            query=self._config.query, max_results=self._config.max_results
        ):
            if store.read(store_keys.event_key(message_id)) is not None:
                continue
            raw = self._client.get_message(message_id)
            out.append(self._to_inbound(message_id, raw))
        return out

    def _to_inbound(self, message_id: str, raw: dict[str, Any]) -> InboundMessage:
        headers = _headers(raw)
        sender = headers.get("from")
        subject = headers.get("subject", "")
        snippet = raw.get("snippet", "")
        auth = parse_authentication_results(
            headers.get("authentication-results"),
            trusted_authserv_id=self._config.trusted_authserv_id,
        )
        text = f"Subject: {subject}\n\n{snippet}".strip()
        return InboundMessage(
            source=self.name,
            message_id=message_id,
            received_at=datetime.now(UTC),
            sender_raw=sender,
            text=text,
            requested_action=self._config.default_action,
            metadata={"auth": auth, "subject": subject, "account": self._config.account},
        )


def _headers(raw: dict[str, Any]) -> dict[str, str]:
    """Flatten the message resource's header list into a lowercase dict.

    First-wins semantics: when a header name repeats, only the first
    occurrence is kept. The connector already places the genuine,
    server-stamped ``Authentication-Results`` first, so a forged copy lower in
    the message is ignored.
    """
    payload = raw.get("payload", {})
    result: dict[str, str] = {}
    for header in payload.get("headers", []):
        name = header.get("name", "").lower()
        if name and name not in result:  # first-wins: skip duplicate names
            result[name] = header.get("value", "")
    return result
