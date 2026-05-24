"""Gmail polling adapter.

Polls a mailbox for new messages, classifies their authentication signals,
and emits one :class:`~lazypulse.models.InboundMessage` per message. The
adapter is at-least-once: it re-emits a message until the PulseAgent has
recorded it (the central ``store_keys.EVENT`` marker exists), so a crash
between drain and record-write cannot lose mail. Central dedupe means a
message still becomes at most one task.

Depends only on the duck-typed :class:`~lazytools.connectors.gmail.client.GmailService`,
so it imports without the Gmail extra and is testable with a fake client.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from lazytools.connectors.gmail.auth import parse_authentication_results
from lazytools.connectors.gmail.client import GmailService

from lazypulse import store_keys
from lazypulse.models import ActionClass, InboundMessage

if TYPE_CHECKING:
    from lazybridge import Session, Store


@dataclass
class GmailInboxConfig:
    """Configuration for :class:`GmailInbox`."""

    account: str
    query: str = "is:unread"
    max_results: int = 25
    #: Only affects the reminder warning, not the API call format (which always
    #: uses ``format="metadata"`` + snippet). Set ``"readonly"`` to remind
    #: yourself that the OAuth scope you configured in
    #: :meth:`~lazypulse.adapters.gmail.client.GmailClient.from_credentials`
    #: is broader than strictly necessary; the agent still only reads headers
    #: and snippet. Prefer ``"metadata"`` in production.
    scope: Literal["metadata", "readonly"] = "metadata"
    default_action: ActionClass = ActionClass.READ_PUBLIC
    #: Authserv-id that the receiving MTA stamps on its
    #: ``Authentication-Results`` header. When set, only headers whose leading
    #: authserv-id *exactly* equals this value are trusted; any other
    #: authserv-id (e.g. a forged one carried inside the message body, or a
    #: look-alike like ``mx.google.com.evil.com``) is rejected as all-fail.
    #: Set to ``None`` to disable pinning (not recommended).
    #:
    #: Gmail's authserv-id is ``"mx.google.com"``, which is the default.
    trusted_authserv_id: str | None = field(default="mx.google.com")

    def __post_init__(self) -> None:
        if self.scope == "readonly":
            warnings.warn(
                "GmailInboxConfig(scope='readonly') indicates a broad OAuth scope. "
                "The API call always uses metadata format; prefer 'metadata' unless "
                "you have configured gmail.readonly OAuth scope intentionally.",
                UserWarning,
                stacklevel=2,
            )


class GmailInbox:
    """An :class:`~lazypulse.Adapter` that polls a Gmail mailbox."""

    def __init__(self, client: GmailService, config: GmailInboxConfig, *, name: str = "gmail") -> None:
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
    """Flatten a Gmail message resource's header list into a lowercase dict.

    Uses first-wins semantics: when multiple headers share the same name, only
    the first occurrence is kept. This is critical for
    ``Authentication-Results``: the receiving MTA (Gmail) prepends its own
    header at the top of the message, making it first. A forged
    ``Authentication-Results`` header carried inside the attacker's message
    appears later and is silently ignored.
    """
    payload = raw.get("payload", {})
    result: dict[str, str] = {}
    for header in payload.get("headers", []):
        name = header.get("name", "").lower()
        if name and name not in result:  # first-wins: skip duplicate names
            result[name] = header.get("value", "")
    return result
