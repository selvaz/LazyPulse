"""Pre-execution authorization.

A :class:`PulsePolicy` answers two questions about an inbound message,
*before* any worker runs:

1. **classify** — who is the sender, and how much do we trust them?
2. **authorize** — given that trust level, may they request this action?

This is deliberately **not** a lazybridge ``Guard``. A Guard inspects the
text flowing into / out of an engine; a policy decides whether a message is
even allowed to reach the engine, and routes the rest to human review or a
rejection log. Different lifecycle, different object.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from lazypulse.models import (
    ActionClass,
    Identity,
    InboundMessage,
    PolicyDecision,
    TrustLevel,
)
from lazypulse.ratelimit import RateLimit

# Re-exported for convenience: ``from lazypulse.policy import TrustLevel`` works
# as well as importing from ``lazypulse.models``.
__all__ = [
    "DEFAULT_ACTION_RULES",
    "ActionClass",
    "Identity",
    "PolicyDecision",
    "PulsePolicy",
    "RateLimit",
    "TrustLevel",
]


#: The default trust → allowed-actions matrix. Conservative by design: an
#: unknown or merely *claimed* owner gets nothing; a verified owner gets
#: local reads/writes but external sends and destructive actions still need
#: explicit confirmation; only an already-approved session gets the full
#: interactive surface.
DEFAULT_ACTION_RULES: dict[TrustLevel, set[ActionClass]] = {
    TrustLevel.UNKNOWN: set(),
    TrustLevel.EXTERNAL_VERIFIED: {ActionClass.READ_PUBLIC},
    TrustLevel.OWNER_CLAIM_UNVERIFIED: set(),
    TrustLevel.OWNER_VERIFIED_EMAIL: {
        ActionClass.READ_PUBLIC,
        ActionClass.WRITE_LOCAL,
    },
    TrustLevel.APPROVED_SESSION: {
        ActionClass.READ_PUBLIC,
        ActionClass.READ_PRIVATE,
        ActionClass.WRITE_LOCAL,
        ActionClass.EXTERNAL_SEND,
    },
    TrustLevel.SYSTEM: set(ActionClass),
}


class PulsePolicy(BaseModel):
    """Trust resolution + action authorization.

    Subclass and override :meth:`classify` to plug in adapter-specific
    identity logic (e.g. ``GmailPolicy`` reads DKIM/SPF/DMARC signals).
    The base implementation trusts nobody — every message resolves to
    ``UNKNOWN`` — which is the safe default for a custom adapter that has
    not taught the policy how to verify its senders.
    """

    owner_emails: list[str] = Field(default_factory=list)
    allowed_external_senders: list[str] = Field(default_factory=list)
    action_rules: dict[TrustLevel, set[ActionClass]] = Field(
        default_factory=lambda: {k: set(v) for k, v in DEFAULT_ACTION_RULES.items()}
    )
    rate_limit: RateLimit | None = None

    def classify(self, inbound: InboundMessage) -> Identity:
        """Resolve the sender's identity. Override in adapter subclasses."""
        return Identity(sender=inbound.sender_raw, trust=TrustLevel.UNKNOWN)

    def authorize(
        self,
        identity: Identity,
        requested_action: ActionClass,
    ) -> PolicyDecision:
        """Map ``(trust, action)`` to a decision.

        The matrix decides ``ALLOW`` outright. Everything else falls through
        to graduated escalation: a verified owner asking for a sensitive
        action is asked to confirm; an externally-verified stranger is
        queued for human review; anyone else is rejected.
        """
        allowed = self.action_rules.get(identity.trust, set())
        if requested_action in allowed:
            return PolicyDecision.ALLOW

        if identity.trust == TrustLevel.OWNER_VERIFIED_EMAIL and requested_action in {
            ActionClass.EXTERNAL_SEND,
            ActionClass.DESTRUCTIVE,
            ActionClass.CODE_OR_COMPUTER,
        }:
            return PolicyDecision.REQUIRE_OWNER_CONFIRMATION

        if identity.trust == TrustLevel.APPROVED_SESSION and requested_action in {
            ActionClass.DESTRUCTIVE,
            ActionClass.CODE_OR_COMPUTER,
        }:
            return PolicyDecision.REQUIRE_OWNER_CONFIRMATION

        if identity.trust == TrustLevel.EXTERNAL_VERIFIED:
            return PolicyDecision.QUEUE_FOR_REVIEW

        return PolicyDecision.REJECT
