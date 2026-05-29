"""RetryPolicy unit tests."""

from __future__ import annotations

from lazypulse.retry import RetryPolicy


def test_should_retry_within_limit() -> None:
    policy = RetryPolicy(max_attempts=3)
    assert policy.should_retry_by_count(0) is True
    assert policy.should_retry_by_count(1) is True
    assert policy.should_retry_by_count(2) is False  # 2 == max_attempts - 1


def test_should_retry_exc_type_match() -> None:
    policy = RetryPolicy(max_attempts=3, retry_on=(ValueError,))
    assert policy.should_retry(0, ValueError("oops")) is True
    assert policy.should_retry(0, RuntimeError("oops")) is False


def test_next_delay_exponential() -> None:
    policy = RetryPolicy(max_attempts=5, backoff_base=2.0, backoff_max=300.0)
    assert policy.next_delay(0) == 1.0  # 2.0**0 = 1
    assert policy.next_delay(1) == 2.0  # 2.0**1
    assert policy.next_delay(2) == 4.0  # 2.0**2


def test_next_delay_capped() -> None:
    policy = RetryPolicy(max_attempts=10, backoff_base=2.0, backoff_max=10.0)
    assert policy.next_delay(5) == 10.0  # 2**5=32, capped at 10


def test_retry_integration() -> None:
    """3 attempts with backoff_max=0 (instant retries): permanently failed after exhausting."""
    from datetime import UTC, datetime

    from lazybridge import Store

    from lazypulse import PulseAgent, store_keys
    from lazypulse.models import PulseRecord
    from lazypulse.retry import RetryPolicy
    from lazypulse.testing import FakeClock, MockEngine

    clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    store = Store()
    engine = MockEngine(raises=RuntimeError("boom"))
    retry_policy = RetryPolicy(max_attempts=3, backoff_base=0.0, backoff_max=0.0)
    agent = PulseAgent(
        name="retry-test",
        engine=engine,
        store=store,
        retry_policy=retry_policy,
        clock=clock,
        unsafe_allow_all=True,
    )
    agent.schedule("do something")

    # Tick 1: runs attempt=0, fails, sets next_retry_at=now (delay=0)
    report1 = agent.tick()
    assert report1.failed == 1

    # Tick 2: reschedules to attempt=1 then runs, fails again
    report2 = agent.tick()
    assert report2.failed == 1

    # Tick 3: reschedules to attempt=2 (last), runs, fails permanently
    report3 = agent.tick()
    assert report3.failed == 1

    keys = [k for k in store if k.startswith(store_keys.TASK_PREFIX)]
    assert len(keys) == 1
    record = PulseRecord.model_validate(store.read(keys[0]))
    assert record.status == "failed"
    assert record.attempt == 2
    assert record.next_retry_at is None  # permanently failed
