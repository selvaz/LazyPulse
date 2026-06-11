"""Exponential backoff on adapter.drain() failures.

Pre-fix, a failing adapter was re-drained at full tick rate — with a
1-second tick against a rate-limited Gmail API, that's 86 400 failing
calls a day, the classic way to turn a 429 into an account suspension.
Now consecutive failures back off exponentially (base doubling, capped),
and one success resets the schedule.
"""

from __future__ import annotations

from lazybridge import Store

from lazypulse import PulseAgent
from lazypulse.testing import FakeClock, MockEngine


class FlakyAdapter:
    name = "flaky"

    def __init__(self, fail_times: int | None = None) -> None:
        # fail_times=None → always fail; N → fail the first N drains.
        self.calls = 0
        self._fail_times = fail_times

    async def drain(self, *, store, session=None):
        self.calls += 1
        if self._fail_times is None or self.calls <= self._fail_times:
            raise RuntimeError("upstream 429")
        return []


def _pulse(adapter: FlakyAdapter, clock: FakeClock, **kwargs) -> PulseAgent:
    return PulseAgent(
        name="p",
        engine=MockEngine(["ok"]),
        store=Store(),
        clock=clock,
        adapters=[adapter],
        unsafe_allow_all=True,
        **kwargs,
    )


async def test_failures_back_off_exponentially() -> None:
    clock = FakeClock()
    adapter = FlakyAdapter()
    pulse = _pulse(adapter, clock, adapter_backoff_base=10.0)

    await pulse.tick_once()
    assert adapter.calls == 1

    # Cooling down (10s): immediate ticks must NOT re-hit the upstream.
    await pulse.tick_once()
    await pulse.tick_once()
    assert adapter.calls == 1

    clock.advance(11)
    await pulse.tick_once()
    assert adapter.calls == 2

    # Second failure doubles the delay (20s).
    clock.advance(11)
    await pulse.tick_once()
    assert adapter.calls == 2
    clock.advance(10)
    await pulse.tick_once()
    assert adapter.calls == 3


async def test_delay_is_capped() -> None:
    clock = FakeClock()
    adapter = FlakyAdapter()
    pulse = _pulse(adapter, clock, adapter_backoff_base=100.0, adapter_backoff_cap=150.0)

    await pulse.tick_once()  # failure #1 → delay 100s
    clock.advance(101)
    await pulse.tick_once()  # failure #2 → min(200, 150) = 150s
    assert adapter.calls == 2
    clock.advance(149)
    await pulse.tick_once()
    assert adapter.calls == 2
    clock.advance(2)
    await pulse.tick_once()
    assert adapter.calls == 3


async def test_success_resets_backoff() -> None:
    clock = FakeClock()
    adapter = FlakyAdapter(fail_times=2)
    pulse = _pulse(adapter, clock, adapter_backoff_base=10.0)

    await pulse.tick_once()  # fail #1
    clock.advance(11)
    await pulse.tick_once()  # fail #2 (delay now 20s)
    clock.advance(21)
    await pulse.tick_once()  # success — resets
    assert adapter.calls == 3

    # Healthy adapter drains every tick again, no residual cooldown.
    await pulse.tick_once()
    assert adapter.calls == 4
