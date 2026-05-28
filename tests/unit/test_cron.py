"""CronTrigger unit tests."""

from __future__ import annotations

import pytest

try:
    import croniter  # noqa: F401

    _HAS_CRONITER = True
except ImportError:
    _HAS_CRONITER = False

pytestmark = pytest.mark.skipif(not _HAS_CRONITER, reason="croniter not installed")


def test_cron_trigger_next() -> None:
    from datetime import UTC, datetime

    from lazypulse.cron import CronTrigger

    trigger = CronTrigger("0 * * * *")  # every hour on the dot
    after = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    nxt = trigger.next(after)
    assert nxt.hour == 1
    assert nxt.minute == 0
    assert nxt.tzinfo is not None


def test_cron_trigger_invalid_tz() -> None:
    from lazypulse.cron import CronTrigger

    with pytest.raises(ValueError, match="Unknown timezone"):
        CronTrigger("* * * * *", tz="Not/AReal/Zone")


def test_schedule_cron_fires_on_tick() -> None:
    from datetime import UTC, datetime

    from lazybridge import Store
    from lazypulse import PulseAgent
    from lazypulse.testing import FakeClock, MockEngine

    clock = FakeClock(start=datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    store = Store()
    engine = MockEngine(["ok"])
    agent = PulseAgent(
        name="cron-test",
        engine=engine,
        store=store,
        clock=clock,
        unsafe_allow_all=True,
    )
    cron_id = agent.schedule_cron("tick message", "* * * * *")
    assert cron_id

    # Advance 65 s so the first-minute mark (00:01:00) has passed
    clock.advance(65)
    report = agent.tick()
    assert report.completed == 1
