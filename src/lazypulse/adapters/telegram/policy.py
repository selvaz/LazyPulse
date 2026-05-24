"""Telegram-aware policy.

``TelegramPolicy`` overrides :meth:`classify` to turn the **platform-
authenticated** sender id (``message.from.id``, which Telegram verifies
server-side and a sender cannot spoof) into a :class:`TrustLevel`. This is a
stronger and simpler signal than email: there is no DKIM/DMARC to parse and no
header to forge — the numeric user id is authoritative.

* A message from a bot account, or with no resolvable sender id, is never
  trusted (``UNKNOWN``).
* A user id in ``owner_ids`` gets ``OWNER_VERIFIED_EMAIL`` — the "verified
  owner" trust tier (the enum name is historical, from the Gmail origin; it
  denotes the tier, not the channel).
* A user id in ``allowed_user_ids`` gets ``EXTERNAL_VERIFIED``.
* Everyone else is ``UNKNOWN`` → rejected before the worker runs.

Pure-Python: importable without the ``telegram`` extra.
"""

from __future__ import annotations

from pydantic import Field

from lazypulse.models import Identity, InboundMessage, TrustLevel
from lazypulse.policy import PulsePolicy


class TelegramPolicy(PulsePolicy):
    #: Telegram numeric user ids granted full owner trust.
    owner_ids: list[int] = Field(default_factory=list)
    #: Telegram numeric user ids granted external-verified trust.
    allowed_user_ids: list[int] = Field(default_factory=list)

    def classify(self, inbound: InboundMessage) -> Identity:
        meta = inbound.metadata or {}
        user_id = meta.get("user_id")
        is_bot = bool(meta.get("is_bot", False))
        signals = {"user_id": user_id, "username": meta.get("username"), "is_bot": is_bot}
        sender = str(user_id) if user_id is not None else None

        if user_id is None or is_bot:
            return Identity(
                sender=sender,
                trust=TrustLevel.UNKNOWN,
                auth_signals=signals,
                notes="bot sender" if is_bot else "no sender id",
            )
        if user_id in self.owner_ids:
            return Identity(sender=sender, trust=TrustLevel.OWNER_VERIFIED_EMAIL, auth_signals=signals)
        if user_id in self.allowed_user_ids:
            return Identity(sender=sender, trust=TrustLevel.EXTERNAL_VERIFIED, auth_signals=signals)
        return Identity(sender=sender, trust=TrustLevel.UNKNOWN, auth_signals=signals)
