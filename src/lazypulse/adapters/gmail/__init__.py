"""Gmail adapters for LazyPulse: push (the default), polling, and the policy.

The Gmail **client** and **tools** (``GmailClient``, ``GmailService``,
``GmailTools``, ``GmailSendBlocked``) and the ``parse_authentication_results``
parser live in the sibling ``lazytoolkit`` package
(``pip install 'lazytoolkit[gmail]'``) — import them from
``lazytools.connectors.gmail``. The 0.2 deprecation shim that re-exported
them from here was removed in 0.3, as documented.
"""

from __future__ import annotations

from lazypulse.adapters.gmail.inbox import GmailInbox, GmailInboxConfig
from lazypulse.adapters.gmail.policy import GmailPolicy
from lazypulse.adapters.gmail.push import GmailPushConfig, GmailPushInbox

__all__ = [
    "GmailInbox",
    "GmailInboxConfig",
    "GmailPushInbox",
    "GmailPushConfig",
    "GmailPolicy",
]
