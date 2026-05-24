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

A grant may additionally be **bound to a task** with ``task_id=`` (the
``task_id`` returned by ``approve_task`` / ``schedule``). A task-bound grant is
only consumable by *that* task's worker, so under
``max_concurrent_inbound > 1`` an approval for one task can never be spent by a
different task running at the same time. The binding works because the gated
send is an ``async`` tool: lazybridge runs it in the worker's own context,
where ``PulseAgent`` has published the active task id.
"""

from __future__ import annotations

import asyncio

from lazybridge import Tool

from lazypulse._context import current_task_id
from lazypulse._safety import ActionBlocked, Allowlist, ConfirmationGate
from lazypulse.adapters.gmail.client import GmailService


class GmailSendBlocked(ActionBlocked):
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
    ) -> None:
        self._client = client
        self._allowlist = Allowlist(allowed_recipients)
        self._gate = ConfirmationGate(enabled=require_confirmation)

    @property
    def require_confirmation(self) -> bool:
        """Whether a send needs an outstanding confirmation (public attribute)."""
        return self._gate.enabled

    def confirm_once(self, *, task_id: str | None = None) -> None:
        """Authorize exactly one send to any recipient (subject to the
        allow-list). Call once per approved message. Pass ``task_id=`` to bind
        the grant to a single task so a concurrent task cannot consume it."""
        self._gate.grant_any(scope=task_id)

    def confirm_send(self, *, to: str, task_id: str | None = None) -> None:
        """Authorize exactly one send to a specific recipient — the tighter,
        preferred grant. Pass ``task_id=`` to also bind it to a single task."""
        self._gate.grant(to, scope=task_id)

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

    async def _send(self, to: str, subject: str, body: str) -> str:
        # Async so the task-bound grant check can read the worker's task
        # context (lazybridge runs async tools in-context). The blocking Gmail
        # API call is offloaded to a thread so it never stalls the tick loop.
        if not self._allowlist.permits(to):
            raise GmailSendBlocked(f"gmail_send blocked: recipient {to!r} is not in the allow-list")
        if not self._gate.consume(to, scope=current_task_id()):
            raise GmailSendBlocked("gmail_send blocked: no outstanding confirmation for this send")
        result = await asyncio.to_thread(self._client.send_message, to=to, subject=subject, body=body)
        return f"sent: {result.get('id', '<unknown>')}"
