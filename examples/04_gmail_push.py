"""Event-driven Gmail: push notifications instead of polling.

Instead of re-listing the mailbox every tick (03_gmail_polling.py), Gmail
tells us the moment mail arrives. Steady-state Gmail API usage drops to
one cheap ``history.list`` call per email received — zero calls while
the mailbox is quiet.

One-time GCP setup (~10 minutes, console.cloud.google.com):

1. Create a Pub/Sub topic, e.g. ``projects/<project>/topics/gmail-pulse``.
2. On that topic, grant the *Pub/Sub Publisher* role to
   ``gmail-api-push@system.gserviceaccount.com`` (that's Gmail itself).
3. Create a **push subscription** on the topic whose endpoint is this
   adapter's URL, including the token, e.g.
   ``https://your-host/gmail/push?token=<PULSE_PUSH_TOKEN>``.
   (Expose the local server through a TLS reverse proxy or a tunnel;
   the adapter itself binds 127.0.0.1.)

The adapter arms — and keeps re-arming — the Gmail ``users.watch`` for
you when ``topic_name`` is set. If the daemon is down longer than
Gmail's history retention (about a week), the cursor resyncs forward
with a warning.

Requires: pip install 'lazypulse[gmail,webhook]'   and an ANTHROPIC_API_KEY.
"""

import os
import threading

from lazybridge import LLMEngine, Store
from lazytools.connectors.gmail import GmailClient

from lazypulse import PulseAgent
from lazypulse.adapters.gmail import GmailPolicy, GmailPushConfig, GmailPushInbox

ACCOUNT = os.environ["PULSE_GMAIL_ACCOUNT"]          # e.g. you@gmail.com
TOPIC = os.environ["PULSE_PUBSUB_TOPIC"]             # projects/<p>/topics/gmail-pulse
TOKEN = os.environ["PULSE_PUSH_TOKEN"]               # shared secret in the push URL

client = GmailClient.from_credentials(
    credentials_path="credentials.json",
    token_path="token.json",
    scopes=["https://www.googleapis.com/auth/gmail.metadata"],
)

inbox = GmailPushInbox(
    client,
    GmailPushConfig(
        account=ACCOUNT,
        topic_name=TOPIC,        # adapter arms + renews users.watch itself
        shared_token=TOKEN,      # ?token= auth on the push endpoint
        port=8100,
    ),
)

# The push endpoint runs alongside the tick loop in a daemon thread.
threading.Thread(target=inbox.serve, daemon=True).start()

pulse = PulseAgent(
    name="mail-assistant",
    engine=LLMEngine("claude-opus-4-8"),
    store=Store(db="pulse.db"),  # durable: cursor + ledger survive restarts
    adapters=[inbox],
    policy=GmailPolicy(owner_emails=[ACCOUNT]),
    tick_seconds=5.0,            # ticks are cheap now — no Gmail call unless notified
)

pulse.start()
print(f"Watching {ACCOUNT} via push notifications on {TOPIC}. Ctrl-C to stop.")
try:
    threading.Event().wait()
except KeyboardInterrupt:
    pulse.stop()
