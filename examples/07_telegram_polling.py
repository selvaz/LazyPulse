"""Watch a Telegram bot, triage messages, reply only to the owner.

Unlike email, Telegram authenticates the sender for us: ``message.from.id`` is
verified server-side and cannot be spoofed, so the policy keys on it directly —
no DKIM/DMARC dance. A stranger (or a bot) is rejected before the model ever
sees the text.

One-time setup
--------------
1. Talk to @BotFather, send ``/newbot``, copy the token.
2. Message your new bot once, then open
   ``https://api.telegram.org/bot<TOKEN>/getUpdates`` and read your numeric
   id from ``result[].message.from.id``.
3. Configure and run::

       pip install 'lazypulse[telegram]'
       export TELEGRAM_TOKEN=123456:ABC...
       python examples/07_telegram_polling.py

This one needs a token + network (not offline like 01/05/06).
"""

from __future__ import annotations

import os

from lazybridge import LLMEngine, Store

from lazypulse import PulseAgent
from lazypulse.adapters.telegram import (
    TelegramClient,
    TelegramInbox,
    TelegramInboxConfig,
    TelegramPolicy,
)

TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_ID = 123456789  # <-- your numeric Telegram user id

client = TelegramClient.from_token(TOKEN)

pulse = PulseAgent(
    name="tg-assistant",
    engine=LLMEngine("claude-opus-4-7", system="You are a concise personal assistant. Reply directly to the user."),
    store=Store(db="pulse.db"),                       # persistent: survives restarts
    policy=TelegramPolicy(owner_ids=[OWNER_ID]),      # only the verified owner acts
    # TelegramInbox is a Responder: each tick it polls for messages, and when
    # the worker finishes, its reply is sent straight back to your chat — a
    # full two-way conversation with no extra wiring (reply_with_output=True).
    adapters=[TelegramInbox(client, TelegramInboxConfig(bot_id="tg-assistant"))],
    tick_seconds=3.0,                                 # poll every 3s
)

# Want the agent to also message *other* chats (not just reply to the sender)?
# Add a gated send tool — it stays confirmation-bound so it can't spam:
#
#   from lazypulse.adapters.telegram import TelegramTools
#   tools=[TelegramTools(client, allowed_chat_ids=[OWNER_ID])]

if __name__ == "__main__":
    print("Polling Telegram every 3s. Message your bot; Ctrl-C to stop.")
    pulse.serve()
