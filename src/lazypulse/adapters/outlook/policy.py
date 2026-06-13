"""Outlook-aware policy.

``OutlookPolicy`` turns email authentication signals (DKIM/SPF/DMARC, carried
on the InboundMessage metadata by ``OutlookInbox``) into a
:class:`~lazypulse.models.TrustLevel`. Because both connectors normalise to the
same ``{"dkim", "spf", "dmarc"}`` signals, the classification is identical to
``GmailPolicy`` — the trust tier comes from the mail's authentication, not from
which client fetched it.

The classification is intentionally conservative:

* A message **without** a parsed ``Authentication-Results`` header can never
  be ``OWNER_VERIFIED_EMAIL`` — a missing header is the easiest thing to
  arrange.
* The sender address must be in ``owner_emails`` **and** DKIM+DMARC must pass
  for owner verification.
* A non-owner whose DKIM+DMARC pass is ``EXTERNAL_VERIFIED``.
* Everything else is at most an unverified owner claim, i.e. trusted with
  nothing.

Pure-Python: importable without the Outlook extra.
"""

from __future__ import annotations

from email.utils import parseaddr

from lazypulse.models import Identity, InboundMessage, TrustLevel
from lazypulse.policy import PulsePolicy


class OutlookPolicy(PulsePolicy):
    def classify(self, inbound: InboundMessage) -> Identity:
        auth = inbound.metadata.get("auth", {}) or {}
        dkim = bool(auth.get("dkim"))
        spf = bool(auth.get("spf"))
        dmarc = bool(auth.get("dmarc"))
        # A real From header is ``"Display Name <addr@host>"``; extract just
        # the address so owner matching works (parseaddr also handles a bare
        # address and an encoded display name).
        sender = parseaddr(inbound.sender_raw or "")[1].lower()
        is_owner = sender in {e.lower() for e in self.owner_emails}
        is_allowed_external = sender in {e.lower() for e in self.allowed_external_senders}

        signals = {"dkim": dkim, "spf": spf, "dmarc": dmarc, "sender": sender}

        # Authenticated owner: address matches AND DKIM + DMARC pass.
        if is_owner and dkim and dmarc:
            return Identity(sender=sender, trust=TrustLevel.OWNER_VERIFIED_EMAIL, auth_signals=signals)

        # Owner address but failing authentication → unverified *claim*.
        if is_owner:
            return Identity(
                sender=sender,
                trust=TrustLevel.OWNER_CLAIM_UNVERIFIED,
                auth_signals=signals,
                notes="owner address but DKIM/DMARC did not both pass",
            )

        # Known external correspondent whose authentication checks out.
        if is_allowed_external and dkim and dmarc:
            return Identity(sender=sender, trust=TrustLevel.EXTERNAL_VERIFIED, auth_signals=signals)

        return Identity(sender=sender, trust=TrustLevel.UNKNOWN, auth_signals=signals)
