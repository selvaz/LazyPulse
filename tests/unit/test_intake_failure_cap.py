"""``PulseAgent`` intake failure cap: no infinite retries regardless of why
``_intake`` keeps failing.

The bug this backs: an incident on 2026-09-20 (see
``docs/piano-affidabilita-2026-09-23.md`` Passo 11 in LazyCEO) showed the
same ``telegram:<bot>:<update_id>`` redrained and re-failing on ten
consecutive ticks, each a fresh Telegram ``sendMessage`` 400. The exception
was raised synchronously inside ``PulseAgent._intake`` (there, by a
``command_filter``'s own reply) *before* the event marker ``_intake`` would
otherwise write. An at-least-once adapter's watermark (e.g.
``TelegramInbox.drain``) only advances past a *recorded* update, so an
unrecorded one is handed back on every following ``drain()`` and re-raises
indefinitely -- a busy loop with no backoff and no cap.

An earlier version of this fix tried to recognize a "permanent" failure by
parsing an HTTP status out of the exception's message and dead-lettering it
immediately. Review caught that this was both narrower than the actual goal
and unsafe: the same text shape (``"Client error 'NNN ...' for url ..."``)
can come from *any* HTTP call a ``command_filter``, an ``action_classifier``,
or a policy happens to make -- not only Telegram -- so a 400/403 from some
unrelated service would have been dropped forever on the first failure.
Since every failure is now bounded by ``MAX_INTAKE_ATTEMPTS`` regardless of
its shape, that immediate special case bought nothing: a per-message failure
counter (``store_keys.intake_failures_key``) is bumped on every failed
intake and the message is dead-lettered once it hits the cap, full stop.
The counter is deleted the moment a message either succeeds or is
dead-lettered, so it never survives past the message it was tracking.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lazybridge import Session, Store
from lazybridge.exporters import CallbackExporter

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.models import Identity, TrustLevel
from lazypulse.models import InboundMessage as IM
from lazypulse.policy import PulsePolicy
from lazypulse.pulse_agent import MAX_INTAKE_ATTEMPTS
from lazypulse.testing import FakeClock, MockEngine


def _capturing_session() -> tuple[Session, list[dict[str, object]]]:
    """A Session whose emitted events are collected into a plain list, so
    tests can assert on ``pulse.intake_error`` / ``pulse.intake_dead_letter``
    payloads (``attempt`` / ``attempts`` / ``reason``) instead of only on
    Store state."""
    events: list[dict[str, object]] = []
    return Session(exporters=[CallbackExporter(fn=events.append)]), events


def _msg(mid: str, sender: str | None = None) -> InboundMessage:
    return InboundMessage(
        source="tg",
        message_id=mid,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        text="hi",
        sender_raw=sender,
        requested_action="read_public",  # type: ignore[arg-type]
    )


class WatermarkAdapter:
    """Mimics ``TelegramInbox.drain``'s at-least-once watermark: a message is
    handed back on every ``drain()`` call until the Store shows its event
    marker recorded -- exactly the mechanism that turned one failed intake
    into ten (see the incident this test file documents)."""

    name = "tg"

    def __init__(self, messages: list[InboundMessage]) -> None:
        self._messages = list(messages)

    async def drain(self, *, store: Store, session: object | None = None) -> list[InboundMessage]:
        return [m for m in self._messages if store.read(store_keys.event_key(m.message_id)) is None]


class AlwaysRaisingPolicy(PulsePolicy):
    """Stands in for the real live failure: some intake step (there, a
    ``command_filter``'s synchronous Telegram reply) always raises -- the
    specific exception type/shape is deliberately unremarkable, since the
    cap applies the same way regardless of it."""

    def classify(self, inbound: IM) -> Identity:
        raise RuntimeError("boom")

    def authorize(self, identity: Identity, action: object) -> object:  # pragma: no cover - unreached
        raise AssertionError("classify raises before authorize is ever called")


async def test_generic_failure_retried_then_dead_lettered_at_the_attempt_cap() -> None:
    # No special-casing by exception type or shape: this is exercised the
    # same way a Telegram 4xx, a 5xx, a timeout, or a bug anywhere in
    # _intake's call chain would be.
    clock = FakeClock()
    store = Store()
    session, events = _capturing_session()
    message_id = "telegram:bot:boom"
    adapter = WatermarkAdapter([_msg(message_id)])
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["unused"]),
        store=store,
        session=session,
        clock=clock,
        policy=AlwaysRaisingPolicy(),
        adapters=[adapter],
    )

    for attempt in range(1, MAX_INTAKE_ATTEMPTS):
        report = await pulse.tick_once()
        assert report.drained == 1, f"expected a redrain on attempt {attempt}"
        assert store.read(store_keys.event_key(message_id)) is None  # not dead-lettered yet
        counter = store.read(store_keys.intake_failures_key(message_id))
        assert counter == {"count": attempt, "last_error": "RuntimeError: boom"}

    # The MAX_INTAKE_ATTEMPTS-th failure trips the cap.
    report_final = await pulse.tick_once()
    assert report_final.drained == 1
    assert store.read(store_keys.event_key(message_id)) == {"dead_letter": True}
    assert store.read(store_keys.intake_failures_key(message_id)) is None  # counter cleared

    error_events = [e for e in events if e["event_type"] == "pulse.intake_error"]
    assert [e["attempt"] for e in error_events] == list(range(1, MAX_INTAKE_ATTEMPTS))
    dead_letters = [e for e in events if e["event_type"] == "pulse.intake_dead_letter"]
    assert len(dead_letters) == 1
    assert dead_letters[0]["attempts"] == MAX_INTAKE_ATTEMPTS
    assert dead_letters[0]["reason"] == "max_attempts"

    # And now it's actually over: no further redrain, no further failure --
    # the watermark advanced past the dead-lettered message just like it
    # would past a normally processed one.
    report_after = await pulse.tick_once()
    assert report_after.drained == 0


async def test_failure_counter_cleared_once_message_succeeds() -> None:
    # A message that fails a couple of times and then goes through must not
    # carry any leftover counter -- a later, unrelated re-emission of the
    # same message_id (however unlikely) must not inherit stale attempts.
    clock = FakeClock()
    store = Store()
    session, events = _capturing_session()
    message_id = "telegram:bot:flaky"
    calls = {"n": 0}

    class FlakyThenOkPolicy(PulsePolicy):
        def classify(self, inbound: IM) -> Identity:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("still warming up")
            return Identity(sender=inbound.sender_raw, trust=TrustLevel.SYSTEM)

    adapter = WatermarkAdapter([_msg(message_id)])
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["unused"]),
        store=store,
        session=session,
        clock=clock,
        policy=FlakyThenOkPolicy(),
        adapters=[adapter],
    )

    await pulse.tick_once()  # fails, count -> 1
    await pulse.tick_once()  # fails, count -> 2
    assert store.read(store_keys.intake_failures_key(message_id)) == {
        "count": 2,
        "last_error": "RuntimeError: still warming up",
    }

    await pulse.tick_once()  # succeeds, well below the cap
    assert store.read(store_keys.intake_failures_key(message_id)) is None
    assert store.read(store_keys.event_key(message_id)) is not None  # recorded normally (not dead_letter)
    assert store.read(store_keys.event_key(message_id)) != {"dead_letter": True}
    assert not any(e["event_type"] == "pulse.intake_dead_letter" for e in events)

    # Message is now genuinely processed -- the adapter's own watermark
    # logic won't hand it back either.
    report_final = await pulse.tick_once()
    assert report_final.drained == 0
