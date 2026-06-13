"""Outlook adapters for LazyPulse: a local-desktop polling inbox + the policy.

The Outlook **client** and **tools** (``OutlookClient``, ``OutlookService``,
``OutlookTools``, ``OutlookSendBlocked``) live in the sibling ``lazytoolkit``
package (``pip install 'lazytoolkit[outlook]'``) — import them from
``lazytools.connectors.outlook``.

This is the low-setup alternative to the Gmail adapters: it reads the copy of
Outlook already signed in on the local Windows machine over COM, so there is no
OAuth, no API quota, and no Pub/Sub push plumbing to configure.
"""

from __future__ import annotations

from lazypulse.adapters.outlook.inbox import OutlookInbox, OutlookInboxConfig
from lazypulse.adapters.outlook.policy import OutlookPolicy

__all__ = [
    "OutlookInbox",
    "OutlookInboxConfig",
    "OutlookPolicy",
]
