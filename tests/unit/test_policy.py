"""The trust × action authorization matrix."""

from __future__ import annotations

import pytest

from lazypulse import InboundMessage
from lazypulse.models import ActionClass, Identity, PolicyDecision, TrustLevel
from lazypulse.policy import DEFAULT_ACTION_RULES, PulsePolicy

ALL_ACTIONS = list(ActionClass)


@pytest.mark.parametrize("trust", list(TrustLevel))
@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_allowed_cells_return_allow(trust: TrustLevel, action: ActionClass) -> None:
    policy = PulsePolicy()
    decision = policy.authorize(Identity(trust=trust), action)
    if action in DEFAULT_ACTION_RULES[trust]:
        assert decision == PolicyDecision.ALLOW
    else:
        assert decision != PolicyDecision.ALLOW


def test_system_allows_everything() -> None:
    policy = PulsePolicy()
    for action in ALL_ACTIONS:
        assert policy.authorize(Identity(trust=TrustLevel.SYSTEM), action) == PolicyDecision.ALLOW


def test_unknown_rejects_everything() -> None:
    policy = PulsePolicy()
    for action in ALL_ACTIONS:
        assert policy.authorize(Identity(trust=TrustLevel.UNKNOWN), action) == PolicyDecision.REJECT


def test_owner_claim_unverified_rejects_everything() -> None:
    policy = PulsePolicy()
    for action in ALL_ACTIONS:
        assert policy.authorize(Identity(trust=TrustLevel.OWNER_CLAIM_UNVERIFIED), action) == PolicyDecision.REJECT


def test_owner_verified_external_send_requires_confirmation() -> None:
    policy = PulsePolicy()
    assert (
        policy.authorize(Identity(trust=TrustLevel.OWNER_VERIFIED_EMAIL), ActionClass.EXTERNAL_SEND)
        == PolicyDecision.REQUIRE_OWNER_CONFIRMATION
    )


def test_owner_verified_destructive_requires_confirmation() -> None:
    policy = PulsePolicy()
    assert (
        policy.authorize(Identity(trust=TrustLevel.OWNER_VERIFIED_EMAIL), ActionClass.DESTRUCTIVE)
        == PolicyDecision.REQUIRE_OWNER_CONFIRMATION
    )


def test_owner_verified_local_actions_allowed() -> None:
    policy = PulsePolicy()
    for action in (ActionClass.READ_PUBLIC, ActionClass.WRITE_LOCAL):
        assert policy.authorize(Identity(trust=TrustLevel.OWNER_VERIFIED_EMAIL), action) == PolicyDecision.ALLOW


def test_external_verified_unallowed_action_queues_for_review() -> None:
    policy = PulsePolicy()
    # EXTERNAL_VERIFIED is allowed READ_PUBLIC; anything else → review.
    assert (
        policy.authorize(Identity(trust=TrustLevel.EXTERNAL_VERIFIED), ActionClass.WRITE_LOCAL)
        == PolicyDecision.QUEUE_FOR_REVIEW
    )


def test_external_verified_read_public_allowed() -> None:
    policy = PulsePolicy()
    assert (
        policy.authorize(Identity(trust=TrustLevel.EXTERNAL_VERIFIED), ActionClass.READ_PUBLIC)
        == PolicyDecision.ALLOW
    )


def test_approved_session_destructive_requires_confirmation() -> None:
    policy = PulsePolicy()
    assert (
        policy.authorize(Identity(trust=TrustLevel.APPROVED_SESSION), ActionClass.DESTRUCTIVE)
        == PolicyDecision.REQUIRE_OWNER_CONFIRMATION
    )


def test_custom_action_rules_override_default() -> None:
    # Lock everything down: even SYSTEM gets nothing.
    policy = PulsePolicy(action_rules={t: set() for t in TrustLevel})
    assert policy.authorize(Identity(trust=TrustLevel.SYSTEM), ActionClass.READ_PUBLIC) == PolicyDecision.REJECT


def test_base_classify_returns_unknown(make_msg) -> None:
    policy = PulsePolicy()
    identity = policy.classify(make_msg(sender="someone@x"))
    assert identity.trust == TrustLevel.UNKNOWN
    assert identity.sender == "someone@x"


def test_subclass_classify_override(make_msg) -> None:
    class OwnerPolicy(PulsePolicy):
        def classify(self, inbound: InboundMessage) -> Identity:
            if inbound.sender_raw in self.owner_emails:
                return Identity(sender=inbound.sender_raw, trust=TrustLevel.OWNER_VERIFIED_EMAIL)
            return Identity(sender=inbound.sender_raw, trust=TrustLevel.UNKNOWN)

    policy = OwnerPolicy(owner_emails=["me@x"])
    assert policy.classify(make_msg(sender="me@x")).trust == TrustLevel.OWNER_VERIFIED_EMAIL
    assert policy.classify(make_msg(sender="other@x")).trust == TrustLevel.UNKNOWN


def test_default_rules_not_mutated_by_instance() -> None:
    # The per-instance action_rules must be a copy, not the shared default.
    policy = PulsePolicy()
    policy.action_rules[TrustLevel.UNKNOWN].add(ActionClass.DESTRUCTIVE)
    assert ActionClass.DESTRUCTIVE not in DEFAULT_ACTION_RULES[TrustLevel.UNKNOWN]
