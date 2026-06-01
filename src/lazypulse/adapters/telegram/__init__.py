"""Telegram adapter for LazyPulse.

The inbound polling inbox and the identity-aware policy live here. The Telegram
**client** and **tool** (``TelegramClient``, ``TelegramService``,
``TelegramTools``, ``TelegramSendBlocked``) were extracted to the sibling
``lazytoolkit`` package (``pip install 'lazytoolkit[telegram]'``). They remain
importable from here via a lazy deprecation shim that is removed in 0.3.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from lazypulse.adapters.telegram.inbox import TelegramInbox, TelegramInboxConfig
from lazypulse.adapters.telegram.policy import TelegramPolicy

if TYPE_CHECKING:
    from lazytools.connectors.telegram import (
        TelegramClient,
        TelegramSendBlocked,
        TelegramService,
        TelegramTools,
    )

__all__ = [
    "TelegramInbox",
    "TelegramInboxConfig",
    "TelegramPolicy",
    "TelegramTools",
    "TelegramSendBlocked",
    "TelegramClient",
    "TelegramService",
]

# Symbols extracted to lazytools.connectors.telegram — re-exported lazily.
_MOVED = {
    "TelegramClient",
    "TelegramService",
    "TelegramTools",
    "TelegramSendBlocked",
}


def __getattr__(name: str):  # PEP 562 — fires only for moved symbols
    if name in _MOVED:
        warnings.warn(
            f"lazypulse.adapters.telegram.{name} was extracted to lazytools.connectors.telegram; "
            "install 'lazytoolkit' and import from there. This shim is removed in 0.3.",
            DeprecationWarning,
            stacklevel=2,
        )
        try:
            from lazytools.connectors import telegram as _moved
        except ImportError as exc:
            raise ImportError(
                "lazypulse.adapters.telegram now requires 'lazytoolkit' "
                "(pip install 'lazytoolkit[telegram]')."
            ) from exc
        return getattr(_moved, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
