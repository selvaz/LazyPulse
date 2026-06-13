"""Local Outlook desktop intake — the low-setup alternative to Gmail.

No OAuth, no API quota, no Cloud Pub/Sub: this reads the copy of Outlook
**already running and signed in** on the same Windows machine, over COM. The
trade is that it must run on that machine (Windows + Outlook desktop).

Requires the outlook extra on Windows:  pip install 'lazypulse[outlook]'
(pulls lazytoolkit[outlook] → pywin32; the connector imports without it on
other platforms, only the live OutlookClient.connect() needs it.)

Trust comes from the same DKIM/SPF/DMARC signals as the Gmail path: the
genuine, server-stamped ``Authentication-Results`` header is lifted out of
each message's transport headers, and only owner mail that passes DKIM+DMARC
is allowed to act. Pin ``trusted_authserv_id`` to your inbound mail host
(e.g. your M365 domain) for defence-in-depth.

    python examples/08_outlook_local.py
"""

from __future__ import annotations

from lazybridge import LLMEngine, Session, Store
from lazytools.connectors.outlook import OutlookClient

from lazypulse import PulseAgent
from lazypulse.adapters.outlook import OutlookInbox, OutlookInboxConfig, OutlookPolicy

OWNER = "me@example.com"
#: Your tenant's inbound mail host, as it appears as the authserv-id on the
#: ``Authentication-Results`` header your server stamps. Pin it.
INBOUND_AUTHSERV_ID = "yourdomain.onmicrosoft.com"


def main() -> None:
    client = OutlookClient.connect()  # attaches to the running Outlook desktop
    inbox = OutlookInbox(
        client,
        OutlookInboxConfig(
            account=OWNER,
            query="[Unread] = true",
            trusted_authserv_id=INBOUND_AUTHSERV_ID,
        ),
    )

    pulse = PulseAgent(
        name="outlook-pulse",
        engine=LLMEngine("claude-opus-4-8", system="You are a careful inbox assistant."),
        store=Store(db="pulse.db"),  # persistent so lifecycle survives restarts
        session=Session(),
        policy=OutlookPolicy(owner_emails=[OWNER]),
        adapters=[inbox],
        tick_seconds=15.0,
    )

    print("Polling local Outlook every 15s. Ctrl-C to stop.")
    pulse.serve()  # blocks until interrupted


if __name__ == "__main__":
    main()
