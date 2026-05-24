"""Gmail outbound tools for the worker.

Exposes two tools via the lazybridge ``ToolProvider`` protocol:

* ``gmail_create_draft`` — always allowed. Drafting is harmless; a human
  still has to hit send in the Gmail UI.
* ``gmail_send`` — guarded. Sending is an ``EXTERNAL_SEND`` action, so it
  consumes a **single, explicit confirmation** and the recipient must pass
  the optional allow-list. A blocked send raises :class:`GmailSendBlocked`.

Confirmation is deliberately *not* a sticky boolean. A human approval (via the
review queue) authorizes **one** send — either any recipient (``confirm_once``)
or a specific one (``confirm_send(to=...)``). Each send consumes one matching
grant, so an approved single message can't silently authorize a flood.
"""

from __future__ import annotations

from lazybridge import Tool

from lazypulse.adapters.gmail.client import GmailService


class GmailSendBlocked(PermissionError):
    """Raised when ``gmail_send`` is invoked without authorization."""


# Sentinel for a one-shot confirmation that is not bound to a recipient.
_ANY_RECIPIENT = "*"


class GmailTools:
    """A ``ToolProvider`` wrapping a :class:`GmailService` for the worker."""

    _is_lazy_tool_provider = True

    def __init__(
        self,
        client: GmailService,
        *,
        allowed_recipients: list[str] | None = None,
        require_confirmation: bool = True,
    ) -> None:
        self._client = client
        self._allowed_recipients = [r.lower() for r in allowed_recipients] if allowed_recipients is not None else None
        self.require_confirmation = require_confirmation
        # Outstanding one-shot send grants. Keys are a lowercased recipient or
        # ``_ANY_RECIPIENT``; values are the remaining count. Each send pops one.
        self._send_grants: dict[str, int] = {}

    def confirm_once(self) -> None:
        """Authorize exactly one send to any recipient (subject to the
        allow-list). Call once per approved message."""
        self._send_grants[_ANY_RECIPIENT] = self._send_grants.get(_ANY_RECIPIENT, 0) + 1

    def confirm_send(self, *, to: str) -> None:
        """Authorize exactly one send to a specific recipient — the tighter,
        preferred grant."""
        key = to.lower()
        self._send_grants[key] = self._send_grants.get(key, 0) + 1

    def _consume_grant(self, to: str) -> bool:
        # Prefer a recipient-bound grant; fall back to an any-recipient one.
        for key in (to.lower(), _ANY_RECIPIENT):
            if self._send_grants.get(key, 0) > 0:
                self._send_grants[key] -= 1
                return True
        return False

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._create_draft,
                name="gmail_create_draft",
                description="Create a Gmail draft (not sent). Args: to, subject, body.",
            ),
            Tool.wrap(
                self._send,
                name="gmail_send",
                description="Send an email via Gmail. Requires a one-shot confirmation. Args: to, subject, body.",
            ),
        ]

    # ------------------------------------------------------------------ #
    # Tool implementations
    # ------------------------------------------------------------------ #
    def _create_draft(self, to: str, subject: str, body: str) -> str:
        result = self._client.create_draft(to=to, subject=subject, body=body)
        return f"draft created: {result.get('id', '<unknown>')}"

    def _send(self, to: str, subject: str, body: str) -> str:
        if self._allowed_recipients is not None and to.lower() not in self._allowed_recipients:
            raise GmailSendBlocked(f"gmail_send blocked: recipient {to!r} is not in the allow-list")
        if self.require_confirmation and not self._consume_grant(to):
            raise GmailSendBlocked("gmail_send blocked: no outstanding confirmation for this send")
        result = self._client.send_message(to=to, subject=subject, body=body)
        return f"sent: {result.get('id', '<unknown>')}"
