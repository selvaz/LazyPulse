from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from lazybridge import Store

from lazypulse.fleet.events import (
    DEFAULT_PLANNED_STOP_PREFIX,
    DEFAULT_PROCESS_EVENT_PREFIX,
    planned_stop_covering,
    read_process_events,
    write_planned_stop_marker,
)


def test_planned_stop_marker_creation_and_custom_prefix() -> None:
    store = Store()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    marker = write_planned_stop_marker(
        store, "research", " code reload ", ttl_seconds=30, now=now, prefix="legacy:stop:"
    )

    assert marker == {
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "reason": "code reload",
    }
    assert store.read("legacy:stop:research") == marker
    assert store.read(f"{DEFAULT_PLANNED_STOP_PREFIX}research") is None


@pytest.mark.parametrize(
    ("agent_name", "reason", "ttl", "message"),
    [(" ", "reload", 30, "agent_name"), ("a", " ", 30, "reason"), ("a", "reload", 0, "positive")],
)
def test_planned_stop_marker_validates_inputs(agent_name: str, reason: str, ttl: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        write_planned_stop_marker(Store(), agent_name, reason, ttl_seconds=ttl)


def test_planned_stop_covering_obeys_window_and_expiry() -> None:
    store = Store()
    requested = datetime(2026, 1, 1, 12, tzinfo=UTC)
    write_planned_stop_marker(store, "research", "reload", ttl_seconds=60, now=requested)

    assert (
        planned_stop_covering(
            store, "research", requested + timedelta(seconds=10), now=requested + timedelta(seconds=20)
        )
        is not None
    )
    assert planned_stop_covering(store, "research", requested - timedelta(microseconds=1), now=requested) is None
    assert (
        planned_stop_covering(
            store, "research", requested + timedelta(seconds=10), now=requested + timedelta(seconds=61)
        )
        is None
    )


def test_planned_stop_covering_tolerates_bad_stored_datetimes() -> None:
    store = Store()
    store.write(
        f"{DEFAULT_PLANNED_STOP_PREFIX}research",
        {"requested_at": "not-a-date", "expires_at": 123, "reason": "reload"},
    )
    assert planned_stop_covering(store, "research", datetime.now(UTC)) is None


def test_read_process_events_is_newest_first_and_prefix_configurable() -> None:
    store = Store()
    store.write(
        f"{DEFAULT_PROCESS_EVENT_PREFIX}older",
        {"event_id": "older", "created_at": "2026-01-01T00:00:00+00:00"},
    )
    store.write(
        f"{DEFAULT_PROCESS_EVENT_PREFIX}newer",
        {"event_id": "newer", "created_at": "2026-01-02T00:00:00+00:00"},
    )
    store.write("legacy:event:custom", {"event_id": "custom", "created_at": "2026-01-03T00:00:00"})
    store.write(f"{DEFAULT_PROCESS_EVENT_PREFIX}bad", {"event_id": "bad", "created_at": "invalid"})

    assert [event["event_id"] for event in read_process_events(store)] == ["newer", "older", "bad"]
    assert [event["event_id"] for event in read_process_events(store, prefix="legacy:event:")] == ["custom"]
