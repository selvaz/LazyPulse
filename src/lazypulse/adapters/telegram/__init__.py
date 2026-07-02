"""Telegram adapter for LazyPulse.

The inbound polling inbox and the identity-aware policy live here. The Telegram
**client** and **tool** (``TelegramClient``, ``TelegramService``,
``TelegramTools``, ``TelegramSendBlocked``) live in the sibling
``lazytoolkit`` package: import them from ``lazytools.connectors.telegram``
(``pip install 'lazytoolkit[telegram]'``).
"""

from __future__ import annotations

from lazypulse.adapters.telegram.inbox import TelegramInbox, TelegramInboxConfig
from lazypulse.adapters.telegram.policy import TelegramPolicy

__all__ = [
    "TelegramInbox",
    "TelegramInboxConfig",
    "TelegramPolicy",
]
