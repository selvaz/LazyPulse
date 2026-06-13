"""Outlook-aware policy.

``OutlookPolicy`` turns email authentication signals (DKIM/SPF/DMARC, carried
on the InboundMessage metadata by ``OutlookInbox``) into a
:class:`~lazypulse.models.TrustLevel`. Because both connectors normalise to the
same ``{"dkim", "spf", "dmarc"}`` signals, the classification is identical to
``GmailPolicy`` — the trust tier comes from the mail's authentication, not from
which client fetched it.

The classification is intentionally conservative:

* **Fail closed without a pinned authserv-id.** When the inbox was built
  without ``trusted_authserv_id`` (carried as ``auth_pinned=False`` on the
  message), the ``Authentication-Results`` header is not trusted at all — no
  message can rise above an unverified owner claim. A non-stamping inbound
  server would otherwise let a forged passing header earn owner trust.
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
        # Default False (fail closed) so a message lacking the flag — a custom
        # adapter, or an inbox built without a pin — is never auto-verified.
        pinned = bool(inbound.metadata.get("auth_pinned", False))
        # A real From header is ``"Display Name <addr@host>"``; extract just
        # the address so owner matching works (parseaddr also handles a bare
        # address and an encoded display name).
        sender = parseaddr(inbound.sender_raw or "")[1].lower()
        is_owner = sender in {e.lower() for e in self.owner_emails}
        is_allowed_external = sender in {e.lower() for e in self.allowed_external_senders}

        signals = {"dkim": dkim, "spf": spf, "dmarc": dmarc, "sender": sender, "authserv_pinned": pinned}

        # Fail closed without a pinned authserv-id. Unlike Gmail (which pins
        # ``mx.google.com`` and strips inbound copies of its own header), an
        # arbitrary inbound server may not stamp its own Authentication-Results
        # — letting an attacker carry a forged passing header that first-wins
        # would then trust. With no pin we cannot tell genuine from forged, so
        # the header grants no trust: an owner address is at most an unverified
        # claim, everyone else stays unknown.
        if not pinned:
            trust = TrustLevel.OWNER_CLAIM_UNVERIFIED if is_owner else TrustLevel.UNKNOWN
            return Identity(
                sender=sender,
                trust=trust,
                auth_signals=signals,
                notes="authserv-id not pinned; Authentication-Results not trusted",
            )

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
