"""Gmail polling with a conservative trust policy.

Requires the gmail extra:  pip install 'lazypulse[gmail]'

Setup:
1. Create an OAuth client (Desktop) in Google Cloud Console and download
   ``credentials.json``.
2. Enable the Gmail API for the project.
3. On first run a browser opens for consent; the token is cached in
   ``token.json`` (git-ignored).

The default ``metadata`` scope reads headers + snippets only, which keeps the
grant to ``gmail.metadata``. Only owner mail that passes DKIM + DMARC is
allowed to act; everyone else is rejected or queued for review.

    python examples/03_gmail_polling.py
"""

from __future__ import annotations

from lazybridge import LLMEngine, Session, Store

from lazypulse import PulseAgent
from lazypulse.adapters.gmail import GmailClient, GmailInbox, GmailInboxConfig, GmailPolicy

OWNER = "me@example.com"
GMAIL_METADATA_SCOPE = ["https://www.googleapis.com/auth/gmail.metadata"]


def main() -> None:
    client = GmailClient.from_credentials(
        credentials_path="credentials.json",
        token_path="token.json",
        scopes=GMAIL_METADATA_SCOPE,
    )
    inbox = GmailInbox(client, GmailInboxConfig(account=OWNER, query="is:unread", scope="metadata"))

    pulse = PulseAgent(
        name="gmail-pulse",
        engine=LLMEngine("claude-opus-4-7", system="You are a careful inbox assistant."),
        store=Store(db="pulse.db"),  # persistent so lifecycle survives restarts
        session=Session(),
        policy=GmailPolicy(owner_emails=[OWNER]),
        adapters=[inbox],
        tick_seconds=15.0,
    )

    print("Polling Gmail every 15s. Ctrl-C to stop.")
    pulse.serve()  # blocks until interrupted — no async, no event loop to manage


if __name__ == "__main__":
    main()
