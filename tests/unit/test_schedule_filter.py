"""A schedule that fires on a timer regardless of state.

Measured on a live fleet: five self-created schedules woke a full agent
about a hundred times a week, each turn discovering there was nothing to
do and going back to sleep. The timer was right; the question "is there
anything to wake FOR" was simply never asked.

These tests hold the two halves that make the answer safe to act on: the
suppression is recorded with its reason, and a filter that fails does not
silence anything.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from lazybridge import Store

from lazypulse import Calendar, Cron, PulseAgent
from lazypulse.testing import FakeClock, MockEngine

_START = datetime(2026, 9, 18, 8, 0, tzinfo=UTC)


def _agent(clock: FakeClock, **kwargs) -> PulseAgent:
    return PulseAgent(
        name="cal",
        engine=MockEngine(["ok"]),
        store=Store(),
        clock=clock,
        calendar=Calendar([Cron("sweep", "look at the repositories", "0 * * * *")]),
        unsafe_allow_all=True,
        **kwargs,
    )


def test_a_filter_can_stop_a_firing_that_had_nothing_to_do() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock, schedule_filter=lambda name: "nothing_eligible")

    clock.advance(3600)
    report = agent.tick()

    assert report.fired == 0
    assert report.missed == 1


def test_a_filter_that_declines_to_skip_lets_the_schedule_fire() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock, schedule_filter=lambda name: None)

    clock.advance(3600)
    report = agent.tick()

    assert report.fired == 1
    assert report.missed == 0


def test_the_filter_is_told_which_schedule_it_is_deciding_about() -> None:
    """One filter serves every schedule, so it has to know which one it is
    answering for -- otherwise it can only be an all-or-nothing switch."""
    clock = FakeClock(start=_START)
    seen: list[str] = []

    def remember(name: str) -> str | None:
        seen.append(name)
        return None

    agent = _agent(clock, schedule_filter=remember)
    clock.advance(3600)
    agent.tick()

    assert seen == ["sweep"]


def test_a_filter_that_raises_does_not_silence_the_schedule() -> None:
    """Not knowing fires. A filter is an optimisation, and an optimisation
    that swallows a schedule when it breaks is worse than none: the
    schedule stops and nothing says why."""
    clock = FakeClock(start=_START)

    def broken(name: str) -> str | None:
        raise RuntimeError("the state store is unreachable")

    agent = _agent(clock, schedule_filter=broken)
    clock.advance(3600)
    report = agent.tick()

    assert report.fired == 1
    assert report.missed == 0


def test_no_filter_behaves_exactly_as_before() -> None:
    clock = FakeClock(start=_START)
    agent = _agent(clock)

    clock.advance(3600)
    report = agent.tick()

    assert report.fired == 1
    assert report.missed == 0


@pytest.mark.parametrize("reason", ["nothing_eligible", "project_paused"])
def test_the_reason_travels_so_a_quiet_schedule_is_not_a_silent_one(reason: str) -> None:
    """The whole safety argument. A suppressed firing has to be
    distinguishable from a scheduler that simply stopped working, and the
    only thing that distinguishes them is the recorded reason."""
    clock = FakeClock(start=_START)
    agent = _agent(clock, schedule_filter=lambda name: reason)
    emitted: list[tuple[str, dict]] = []
    original = agent._emit

    def capture(event: str, payload: dict) -> None:
        emitted.append((event, payload))
        original(event, payload)

    agent._emit = capture  # type: ignore[method-assign]
    clock.advance(3600)
    agent.tick()

    missed = [payload for event, payload in emitted if event == "pulse.schedule_missed"]
    assert len(missed) == 1
    assert missed[0]["schedule"] == "sweep"
    assert missed[0]["reason"] == reason


def test_both_schedule_events_name_the_agent_that_decided() -> None:
    """Several agents can share a Store and race for the same occurrence,
    and whoever wins the claim imposes ITS decision. With a filter
    installed unevenly, firing then depends on who got there first.

    Naming the decider does not remove that race -- it makes a divergence
    legible in the record, instead of looking like an intermittent
    scheduler. Found by Codex review on PR #50.
    """
    for skip, expected_event in ((True, "pulse.schedule_missed"), (False, "pulse.schedule_fired")):
        clock = FakeClock(start=_START)
        agent = _agent(clock, schedule_filter=lambda name, s=skip: "nothing_eligible" if s else None)
        emitted: list[tuple[str, dict]] = []
        original = agent._emit

        def capture(event: str, payload: dict, _seen=emitted, _orig=original) -> None:
            _seen.append((event, payload))
            _orig(event, payload)

        agent._emit = capture  # type: ignore[method-assign]
        clock.advance(3600)
        agent.tick()

        matching = [payload for event, payload in emitted if event == expected_event]
        assert len(matching) == 1, expected_event
        assert matching[0]["decided_by"] == "cal"
