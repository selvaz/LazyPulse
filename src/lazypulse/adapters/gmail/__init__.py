"""Gmail adapter for LazyPulse.

Polling inbox, auth-aware policy, and guarded draft/send tools. The Google
client libraries are only needed to build a real :class:`GmailClient` from
credentials (``GmailClient.from_credentials``); the rest of the surface
imports without the ``gmail`` extra and is fully testable with a fake client.
"""

from __future__ import annotations

from lazypulse.adapters.gmail.auth import parse_authentication_results
from lazypulse.adapters.gmail.client import GmailClient, GmailService
from lazypulse.adapters.gmail.inbox import GmailInbox, GmailInboxConfig
from lazypulse.adapters.gmail.policy import GmailPolicy
from lazypulse.adapters.gmail.tools import GmailSendBlocked, GmailTools

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
