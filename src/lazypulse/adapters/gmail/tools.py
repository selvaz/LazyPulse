"""Gmail outbound tools for the worker.

Exposes two tools via the lazybridge ``ToolProvider`` protocol:

* ``gmail_create_draft`` — always allowed. Drafting is harmless; a human
  still has to hit send in the Gmail UI.
* ``gmail_send`` — guarded. Sending is an ``EXTERNAL_SEND`` action, so it is
  blocked unless the send has been explicitly confirmed (the PulseAgent flips
  ``confirmed`` after a human approval round) and the recipient passes the
  optional allow-list. A blocked send raises :class:`GmailSendBlocked`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lazybridge import Tool

from lazypulse.adapters.gmail.client import GmailService

if TYPE_CHECKING:
    pass


class GmailSendBlocked(PermissionError):
    """Raised when ``gmail_send`` is invoked without authorization."""


class GmailTools:
    """A ``ToolProvider`` wrapping a :class:`GmailService` for the worker."""

    _is_lazy_tool_provider = True

    def __init__(
        self,
        client: GmailService,
        *,
        allowed_recipients: list[str] | None = None,
        require_confirmation: bool = True,
        confirmed: bool = False,
    ) -> None:
        self._client = client
        self._allowed_recipients = [r.lower() for r in allowed_recipients] if allowed_recipients is not None else None
        self.require_confirmation = require_confirmation
        # Flipped to True by the PulseAgent after an explicit human approval.
        self.confirmed = confirmed

    def confirm(self) -> None:
        """Authorize the next send(s). Called after human confirmation."""
        self.confirmed = True

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
                description="Send an email via Gmail. Requires prior confirmation. Args: to, subject, body.",
            ),
        ]

    # ------------------------------------------------------------------ #
    # Tool implementations
    # ------------------------------------------------------------------ #
    def _create_draft(self, to: str, subject: str, body: str) -> str:
        result = self._client.create_draft(to=to, subject=subject, body=body)
        return f"draft created: {result.get('id', '<unknown>')}"

    def _send(self, to: str, subject: str, body: str) -> str:
        if self.require_confirmation and not self.confirmed:
            raise GmailSendBlocked("gmail_send blocked: send has not been confirmed")
        if self._allowed_recipients is not None and to.lower() not in self._allowed_recipients:
            raise GmailSendBlocked(f"gmail_send blocked: recipient {to!r} is not in the allow-list")
        result = self._client.send_message(to=to, subject=subject, body=body)
        return f"sent: {result.get('id', '<unknown>')}"
