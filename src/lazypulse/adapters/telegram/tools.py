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

from lazybridge import Tool

from lazypulse.adapters.telegram.client import TelegramService


class TelegramSendBlocked(PermissionError):
    """Raised when ``telegram_send_message`` is invoked without authorization."""


# Sentinel for a one-shot confirmation not bound to a specific chat.
_ANY_CHAT = "*"


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
        self._allowed_chat_ids = {str(c) for c in allowed_chat_ids} if allowed_chat_ids is not None else None
        self.require_confirmation = require_confirmation
        # Outstanding one-shot send grants. Keys are a stringified chat id or
        # ``_ANY_CHAT``; values are the remaining count. Each send pops one.
        self._send_grants: dict[str, int] = {}

    def confirm_once(self) -> None:
        """Authorize exactly one send to any chat (subject to the allow-list)."""
        self._send_grants[_ANY_CHAT] = self._send_grants.get(_ANY_CHAT, 0) + 1

    def confirm_send(self, *, chat_id: int | str) -> None:
        """Authorize exactly one send to a specific chat — the tighter grant."""
        key = str(chat_id)
        self._send_grants[key] = self._send_grants.get(key, 0) + 1

    def _consume_grant(self, chat_id: str) -> bool:
        # Prefer a chat-bound grant; fall back to an any-chat one.
        for key in (chat_id, _ANY_CHAT):
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
                self._send_message,
                name="telegram_send_message",
                description="Send a Telegram message. Requires a one-shot confirmation. Args: chat_id, text.",
            ),
        ]

    # ------------------------------------------------------------------ #
    # Tool implementation
    # ------------------------------------------------------------------ #
    def _send_message(self, chat_id: int | str, text: str) -> str:
        key = str(chat_id)
        if self._allowed_chat_ids is not None and key not in self._allowed_chat_ids:
            raise TelegramSendBlocked(f"telegram_send_message blocked: chat {key!r} is not in the allow-list")
        if self.require_confirmation and not self._consume_grant(key):
            raise TelegramSendBlocked("telegram_send_message blocked: no outstanding confirmation for this send")
        result = self._client.send_message(chat_id=chat_id, text=text)
        return f"sent: message_id={result.get('message_id', '<unknown>')}"
