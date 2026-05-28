"""RateLimit unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import PulseAgent
from lazypulse.models import Identity, InboundMessage, TickReport, TrustLevel
from lazypulse.policy import PulsePolicy
from lazypulse.ratelimit import RateLimit
from lazypulse.testing import FakeClock, MockEngine


def _make_agent(rl: RateLimit, store: Store | None = None, clock: FakeClock | None = None) -> PulseAgent:
    if store is None:
        store = Store()
    if clock is None:
        clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))

    class AllowPolicy(PulsePolicy):
        """Policy that grants SYSTEM trust to every sender (for rate-limit testing)."""

        def classify(self, inbound: InboundMessage) -> Identity:
            return Identity(sender=inbound.sender_raw, trust=TrustLevel.SYSTEM)

    policy = AllowPolicy(rate_limit=rl)
    return PulseAgent(
        name="rl-test",
        engine=MockEngine(["ok"]),
        store=store,
        policy=policy,
        clock=clock,
    )


def _msg(sender: str, idx: int) -> InboundMessage:
    return InboundMessage(
        source="test",
        message_id=f"{sender}-{idx}",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        sender_raw=sender,
        text="hello",
    )


def test_rate_limit_reject_after_max() -> None:
    store = Store()
    clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    rl = RateLimit(max_per_sender=2, window_seconds=3600, on_exceeded="reject")
    agent = _make_agent(rl, store=store, clock=clock)

    now = clock()
    report = TickReport(at=now)
    agent._intake(_msg("alice", 1), now, report)
    agent._intake(_msg("alice", 2), now, report)
    agent._intake(_msg("alice", 3), now, report)  # should be rejected

    assert report.scheduled == 2
    assert report.rejected == 1


def test_rate_limit_queue_after_max() -> None:
    store = Store()
    clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    rl = RateLimit(max_per_sender=1, window_seconds=3600, on_exceeded="queue")
    agent = _make_agent(rl, store=store, clock=clock)

    now = clock()
    report = TickReport(at=now)
    agent._intake(_msg("bob", 1), now, report)
    agent._intake(_msg("bob", 2), now, report)  # should be queued

    assert report.scheduled == 1
    assert report.queued_for_review == 1


def test_rate_limit_different_senders_independent() -> None:
    store = Store()
    clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    rl = RateLimit(max_per_sender=1, window_seconds=3600, on_exceeded="reject")
    agent = _make_agent(rl, store=store, clock=clock)

    now = clock()
    report = TickReport(at=now)
    agent._intake(_msg("alice", 1), now, report)
    agent._intake(_msg("bob", 1), now, report)  # different sender — independent counter

    assert report.scheduled == 2
    assert report.rejected == 0
