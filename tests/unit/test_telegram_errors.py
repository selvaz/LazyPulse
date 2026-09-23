"""``telegram_errors``: classifying Telegram delivery failures raised during
intake, and the ``PulseAgent`` behaviour that reacts to that classification.

The bug this backs: an incident on 2026-09-20 (see
``docs/piano-affidabilita-2026-09-23.md`` Passo 11 in LazyCEO) showed the
same ``telegram:<bot>:<update_id>`` redrained and re-failing on ten
consecutive ticks, each a fresh ``sendMessage`` 400. The exception (raised
synchronously inside ``PulseAgent._intake`` — in that incident, by a
``command_filter``'s own reply) happened *before* the event marker
``_intake`` would otherwise write, so ``TelegramInbox``'s at-least-once
watermark never advanced past the update and the adapter kept handing it
back every tick, forever, with no backoff. A 4xx other than 429 will never
succeed by retrying; a 429/5xx/timeout might, so those must keep being
retried exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from lazybridge import Store
from pydantic import PrivateAttr

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.adapters.telegram_errors import is_permanent_telegram_error, telegram_error_status
from lazypulse.models import Identity
from lazypulse.models import InboundMessage as IM
from lazypulse.policy import PulsePolicy
from lazypulse.testing import FakeClock, MockEngine


def _telegram_client_error(method: str, status: int, reason: str = "Bad Request") -> RuntimeError:
    """The exact shape ``lazytools`` ``TelegramClient._call`` raises: it
    wraps httpx's own ``HTTPStatusError`` string, which looks like
    ``"Client error '400 Bad Request' for url 'https://api.telegram.org/...'"``."""
    kind = "Client" if status < 500 else "Server"
    return RuntimeError(
        f"Telegram API call {method!r} failed: {kind} error '{status} {reason}' "
        f"for url 'https://api.telegram.org/bot<bot-token>/{method}'"
    )


# --------------------------------------------------------------------- #
# telegram_error_status / is_permanent_telegram_error
# --------------------------------------------------------------------- #


def test_status_extracted_from_client_error() -> None:
    exc = _telegram_client_error("sendMessage", 400)
    assert telegram_error_status(exc) == 400


def test_status_extracted_from_server_error() -> None:
    exc = _telegram_client_error("sendMessage", 502, "Bad Gateway")
    assert telegram_error_status(exc) == 502


def test_status_none_for_unshaped_message() -> None:
    # Telegram's own `ok: false` error, a timeout, a connection error -- none
    # of these carry an HTTP-status-shaped fragment.
    assert telegram_error_status(RuntimeError("Telegram API error on sendMessage: chat not found")) is None
    assert telegram_error_status(RuntimeError("Telegram API call 'sendMessage' failed: timed out")) is None


def test_400_403_404_are_permanent() -> None:
    for status in (400, 403, 404):
        assert is_permanent_telegram_error(_telegram_client_error("sendMessage", status)) is True


def test_429_is_not_permanent() -> None:
    assert is_permanent_telegram_error(_telegram_client_error("sendMessage", 429, "Too Many Requests")) is False


def test_5xx_is_not_permanent() -> None:
    assert is_permanent_telegram_error(_telegram_client_error("sendMessage", 500, "Internal Server Error")) is False
    assert is_permanent_telegram_error(_telegram_client_error("sendMessage", 503, "Service Unavailable")) is False


def test_unshaped_message_is_not_permanent() -> None:
    # Unclassifiable => retried, never silently dropped.
    assert is_permanent_telegram_error(RuntimeError("Telegram API call 'sendMessage' failed: timed out")) is False


# --------------------------------------------------------------------- #
# PulseAgent.tick_once: dead-lettering a permanent intake failure
# --------------------------------------------------------------------- #


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


class RaisingPolicy(PulsePolicy):
    """Stands in for the real live failure: some intake step (there, a
    ``command_filter``'s synchronous ``notify()``) raises the Telegram
    client's own exception shape.

    ``PulsePolicy`` is a pydantic model, so the exception factory is a
    ``PrivateAttr`` rather than a constructor argument grafted onto
    ``BaseModel.__init__``."""

    _exc_factory: Callable[[], BaseException] = PrivateAttr()

    def __init__(self, exc_factory: Callable[[], BaseException], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._exc_factory = exc_factory

    def classify(self, inbound: IM) -> Identity:
        raise self._exc_factory()

    def authorize(self, identity: Identity, action: object) -> object:  # pragma: no cover - unreached
        raise AssertionError("classify raises before authorize is ever called")


async def test_permanent_error_dead_letters_and_stops_reprocessing() -> None:
    clock = FakeClock()
    store = Store()
    adapter = WatermarkAdapter([_msg("telegram:bot:1")])
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["unused"]),
        store=store,
        clock=clock,
        policy=RaisingPolicy(lambda: _telegram_client_error("sendMessage", 400)),
        adapters=[adapter],
    )

    report1 = await pulse.tick_once()
    assert report1.drained == 1
    # The event marker now exists, dead-lettered rather than "processed".
    marker = store.read(store_keys.event_key("telegram:bot:1"))
    assert marker == {"dead_letter": True}

    # Next tick: the adapter's own watermark logic (like the real
    # TelegramInbox) no longer hands the message back, so intake never runs
    # for it again -- no repeat exception, no repeat sendMessage attempt.
    report2 = await pulse.tick_once()
    assert report2.drained == 0


async def test_429_keeps_retrying_like_today() -> None:
    clock = FakeClock()
    store = Store()
    adapter = WatermarkAdapter([_msg("telegram:bot:2")])
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["unused"]),
        store=store,
        clock=clock,
        policy=RaisingPolicy(lambda: _telegram_client_error("sendMessage", 429, "Too Many Requests")),
        adapters=[adapter],
    )

    await pulse.tick_once()
    assert store.read(store_keys.event_key("telegram:bot:2")) is None  # not dead-lettered

    # Unchanged from today: still redrained and still raises next tick.
    report2 = await pulse.tick_once()
    assert report2.drained == 1


async def test_5xx_keeps_retrying_like_today() -> None:
    clock = FakeClock()
    store = Store()
    adapter = WatermarkAdapter([_msg("telegram:bot:3")])
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["unused"]),
        store=store,
        clock=clock,
        policy=RaisingPolicy(lambda: _telegram_client_error("sendMessage", 503, "Service Unavailable")),
        adapters=[adapter],
    )

    await pulse.tick_once()
    assert store.read(store_keys.event_key("telegram:bot:3")) is None

    report2 = await pulse.tick_once()
    assert report2.drained == 1


async def test_timeout_keeps_retrying_like_today() -> None:
    clock = FakeClock()
    store = Store()
    adapter = WatermarkAdapter([_msg("telegram:bot:4")])
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["unused"]),
        store=store,
        clock=clock,
        policy=RaisingPolicy(lambda: RuntimeError("Telegram API call 'sendMessage' failed: timed out")),
        adapters=[adapter],
    )

    await pulse.tick_once()
    assert store.read(store_keys.event_key("telegram:bot:4")) is None

    report2 = await pulse.tick_once()
    assert report2.drained == 1

