"""Gmail adapter for LazyPulse.

The inbound polling inbox and the auth-aware policy live here. The Gmail
**client** and **tools** (``GmailClient``, ``GmailService``, ``GmailTools``,
``GmailSendBlocked``) and the ``parse_authentication_results`` parser were
extracted to the sibling ``lazytoolkit`` package (``pip install
'lazytoolkit[gmail]'``). They remain importable from here via a lazy deprecation
shim that is removed in 0.2.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from lazypulse.adapters.gmail.inbox import GmailInbox, GmailInboxConfig
from lazypulse.adapters.gmail.policy import GmailPolicy

if TYPE_CHECKING:
    from lazytools.connectors.gmail import (
        GmailClient,
        GmailSendBlocked,
        GmailService,
        GmailTools,
        parse_authentication_results,
    )

__all__ = [
    "GmailInbox",
    "GmailInboxConfig",
    "GmailPolicy",
    "GmailTools",
    "GmailSendBlocked",
    "GmailClient",
    "GmailService",
    "parse_authentication_results",
]

# Symbols extracted to lazytools.connectors.gmail — re-exported lazily.
_MOVED = {
    "GmailClient",
    "GmailService",
    "GmailTools",
    "GmailSendBlocked",
    "parse_authentication_results",
}


def __getattr__(name: str):  # PEP 562 — fires only for moved symbols
    if name in _MOVED:
        warnings.warn(
            f"lazypulse.adapters.gmail.{name} was extracted to lazytools.connectors.gmail; "
            "install 'lazytoolkit' and import from there. This shim is removed in 0.2.",
            DeprecationWarning,
            stacklevel=2,
        )
        try:
            from lazytools.connectors import gmail as _moved
        except ImportError as exc:
            raise ImportError(
                "lazypulse.adapters.gmail now requires 'lazytoolkit' "
                "(pip install 'lazytoolkit[gmail]')."
            ) from exc
        return getattr(_moved, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
