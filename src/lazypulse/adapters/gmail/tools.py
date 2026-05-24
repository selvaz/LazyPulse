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
        # Outstanding one-shot send grants. Keys are ``(recipient, task_id)``
        # where recipient is a lowercased address or ``_ANY_RECIPIENT`` and
        # task_id is the bound task or ``None`` (any task). Values are the
        # remaining count; each send pops one.
        self._send_grants: dict[tuple[str, str | None], int] = {}

    def confirm_once(self, *, task_id: str | None = None) -> None:
        """Authorize exactly one send to any recipient (subject to the
        allow-list). Call once per approved message. Pass ``task_id=`` to bind
        the grant to a single task so a concurrent task cannot consume it."""
        self._grant(_ANY_RECIPIENT, task_id)

    def confirm_send(self, *, to: str, task_id: str | None = None) -> None:
        """Authorize exactly one send to a specific recipient — the tighter,
        preferred grant. Pass ``task_id=`` to also bind it to a single task."""
        self._grant(to.lower(), task_id)

    def _grant(self, recipient: str, task_id: str | None) -> None:
        key = (recipient, task_id)
        self._send_grants[key] = self._send_grants.get(key, 0) + 1

    def _consume_grant(self, to: str) -> bool:
        # Match from most to least specific. A task-bound grant is only ever
        # found when the running task id matches, so a grant approved for task
        # A can never be spent by a concurrent task B.
        current = current_task_id()
        to_l = to.lower()
        candidates: list[tuple[str, str | None]] = []
        if current is not None:
            candidates.append((to_l, current))
        candidates.append((to_l, None))
        if current is not None:
            candidates.append((_ANY_RECIPIENT, current))
        candidates.append((_ANY_RECIPIENT, None))
        for key in candidates:
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

    async def _send(self, to: str, subject: str, body: str) -> str:
        # Async so the task-bound grant check can read the worker's task
        # context (lazybridge runs async tools in-context). The blocking Gmail
        # API call is offloaded to a thread so it never stalls the tick loop.
        if self._allowed_recipients is not None and to.lower() not in self._allowed_recipients:
            raise GmailSendBlocked(f"gmail_send blocked: recipient {to!r} is not in the allow-list")
        if self.require_confirmation and not self._consume_grant(to):
            raise GmailSendBlocked("gmail_send blocked: no outstanding confirmation for this send")
        result = await asyncio.to_thread(self._client.send_message, to=to, subject=subject, body=body)
        return f"sent: {result.get('id', '<unknown>')}"
