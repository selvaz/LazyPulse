"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from lazybridge import Store

from lazypulse import InboundMessage
from lazypulse.testing import FakeClock


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))


def build_msg(
    text: str = "hello",
    *,
    message_id: str = "m1",
    source: str = "mock",
    sender: str | None = None,
    action: str = "read_public",
) -> InboundMessage:
    return InboundMessage(
        source=source,
        message_id=message_id,
        received_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        sender_raw=sender,
        text=text,
        requested_action=action,  # type: ignore[arg-type]
    )


@pytest.fixture
def make_msg():
    """Factory fixture for building InboundMessages in tests."""
    return build_msg
