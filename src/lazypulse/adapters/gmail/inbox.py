"""Gmail polling adapter.

Polls a mailbox for new messages, classifies their authentication signals,
and emits one :class:`~lazypulse.models.InboundMessage` per message. The
adapter is at-least-once: it re-emits a message until the PulseAgent has
recorded it (the central ``store_keys.EVENT`` marker exists), so a crash
between drain and record-write cannot lose mail. Central dedupe means a
message still becomes at most one task.

Depends only on the duck-typed :class:`~lazypulse.adapters.gmail.client.GmailService`,
so it imports without the Gmail extra and is testable with a fake client.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from lazypulse import store_keys
from lazypulse.adapters.gmail.auth import parse_authentication_results
from lazypulse.adapters.gmail.client import GmailService
from lazypulse.models import ActionClass, InboundMessage

if TYPE_CHECKING:
    from lazybridge import Session, Store


@dataclass
class GmailInboxConfig:
    """Configuration for :class:`GmailInbox`."""

    account: str
    query: str = "is:unread"
    max_results: int = 25
    #: ``"metadata"`` keeps the deployment on the narrow ``gmail.metadata``
    #: OAuth scope (headers + snippet only). ``"readonly"`` can read full
    #: bodies but is a far broader grant — emit a warning so the operator
    #: makes that trade-off deliberately.
    scope: Literal["metadata", "readonly"] = "metadata"
    default_action: ActionClass = ActionClass.READ_PUBLIC

    def __post_init__(self) -> None:
        if self.scope == "readonly":
            warnings.warn(
                "GmailInboxConfig(scope='readonly') grants full message-body access. "
                "Prefer 'metadata' unless the worker genuinely needs body text.",
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
        auth = parse_authentication_results(headers.get("authentication-results"))
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
    """Flatten a Gmail message resource's header list into a lowercase dict."""
    payload = raw.get("payload", {})
    result: dict[str, str] = {}
    for header in payload.get("headers", []):
        name = header.get("name", "").lower()
        if name:
            result[name] = header.get("value", "")
    return result
