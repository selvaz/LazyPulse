"""Telegram adapter for LazyPulse.

A polling inbox (Bot API ``getUpdates``), an identity-aware policy keyed on the
platform-authenticated sender id, and a guarded send tool. Only building a real
:class:`TelegramClient` from a bot token needs the ``telegram`` extra
(``httpx``); the rest of the surface imports without it and is fully testable
with a fake client.
"""

from __future__ import annotations

from lazypulse.adapters.telegram.client import TelegramClient, TelegramService
from lazypulse.adapters.telegram.inbox import TelegramInbox, TelegramInboxConfig
from lazypulse.adapters.telegram.policy import TelegramPolicy
from lazypulse.adapters.telegram.tools import TelegramSendBlocked, TelegramTools

__all__ = [
    "TelegramInbox",
    "TelegramInboxConfig",
    "TelegramPolicy",
    "TelegramTools",
    "TelegramSendBlocked",
    "TelegramClient",
    "TelegramService",
]
