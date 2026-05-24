"""Telegram outbound tool for the worker.

Exposes ``telegram_send_message`` via the lazybridge ``ToolProvider``
protocol. Sending is an outbound action, so — exactly like ``GmailTools`` — it
consumes a **single, explicit confirmation** and the chat must pass the
optional allow-list. A blocked send raises :class:`TelegramSendBlocked`.

Two common setups:

* **Reply freely to a known chat** (typical chat-bot): pass
  ``allowed_chat_ids=[your_chat]`` and ``require_confirmation=False``. Sends
  are still bounded to the allow-list.
* **May message arbitrary chats**: keep ``require_confirmation=True`` and grant
  one send per approved task via ``confirm_send(chat_id=...)`` /
  ``confirm_once()`` — each grant authorizes exactly one send.
"""

from __future__ import annotations

import asyncio

from lazybridge import Tool

from lazypulse._context import current_task_id
from lazypulse._safety import ActionBlocked, Allowlist, ConfirmationGate
from lazypulse.adapters.telegram.client import TelegramService


class TelegramSendBlocked(ActionBlocked):
    """Raised when ``telegram_send_message`` is invoked without authorization."""


class TelegramTools:
    """A ``ToolProvider`` wrapping a :class:`TelegramService` for the worker."""

    _is_lazy_tool_provider = True

    def __init__(
        self,
        client: TelegramService,
        *,
        allowed_chat_ids: list[int | str] | None = None,
        require_confirmation: bool = True,
    ) -> None:
        self._client = client
        self._allowlist = Allowlist(allowed_chat_ids)
        self._gate = ConfirmationGate(enabled=require_confirmation)

    @property
    def require_confirmation(self) -> bool:
        """Whether a send needs an outstanding confirmation (public attribute)."""
        return self._gate.enabled

    def confirm_once(self, *, task_id: str | None = None) -> None:
        """Authorize exactly one send to any chat (subject to the allow-list).
        Pass ``task_id=`` to bind the grant to a single task so a concurrent
        task cannot consume it."""
        self._gate.grant_any(scope=task_id)

    def confirm_send(self, *, chat_id: int | str, task_id: str | None = None) -> None:
        """Authorize exactly one send to a specific chat — the tighter grant.
        Pass ``task_id=`` to also bind it to a single task."""
        self._gate.grant(chat_id, scope=task_id)

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._send_message,
                name="telegram_send_message",
                description="Send a Telegram message. Requires a one-shot confirmation. Args: chat_id, text.",
            ),
        ]

    # ------------------------------------------------------------------ #
    # Tool implementation
    # ------------------------------------------------------------------ #
    async def _send_message(self, chat_id: int | str, text: str) -> str:
        # Async so the task-bound grant check can read the worker's task
        # context (lazybridge runs async tools in-context). The blocking Bot
        # API call is offloaded to a thread so it never stalls the tick loop.
        key = str(chat_id)
        if not self._allowlist.permits(chat_id):
            raise TelegramSendBlocked(f"telegram_send_message blocked: chat {key!r} is not in the allow-list")
        if not self._gate.consume(chat_id, scope=current_task_id()):
            raise TelegramSendBlocked("telegram_send_message blocked: no outstanding confirmation for this send")
        result = await asyncio.to_thread(self._client.send_message, chat_id=chat_id, text=text)
        return f"sent: message_id={result.get('message_id', '<unknown>')}"
