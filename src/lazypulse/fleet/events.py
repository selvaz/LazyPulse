"""Durable process-event outbox and short-lived planned-stop markers.

Promoted from LazyCEO because watchdog exit auditing and suppression of
operator-planned restarts are generic process-supervision infrastructure.
Neutral prefixes are defaults; compatibility callers may pass their old
Store namespaces explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from lazybridge import Store

DEFAULT_PLANNED_STOP_PREFIX = "fleet:planned-stop:"
DEFAULT_PROCESS_EVENT_PREFIX = "fleet:process-event:"
DEFAULT_PLANNED_STOP_TTL_SECONDS = 120.0


class _StoreItemsReader(Protocol):
    def items(self, *, prefix: str | None = None) -> list[tuple[str, Any]]: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: Any) -> datetime | None:
    """Tolerantly parse stored ISO datetimes, treating naive values as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def write_planned_stop_marker(
    store: Store,
    agent_name: str,
    reason: str,
    *,
    ttl_seconds: float = DEFAULT_PLANNED_STOP_TTL_SECONDS,
    now: datetime | None = None,
    prefix: str = DEFAULT_PLANNED_STOP_PREFIX,
) -> dict[str, str]:
    """Record an operator-planned stop/restart for alert suppression."""
    if not agent_name.strip():
        raise ValueError("agent_name must be a non-empty string")
    if not reason.strip():
        raise ValueError("reason must be a non-empty string")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    requested_at = (now or _utc_now()).astimezone(UTC)
    marker = {
        "requested_at": requested_at.isoformat(),
        "expires_at": (requested_at + timedelta(seconds=ttl_seconds)).isoformat(),
        "reason": reason.strip(),
    }
    store.write(f"{prefix}{agent_name}", marker)
    return marker


def planned_stop_covering(
    store: Store,
    agent_name: str,
    event_created_at: datetime,
    *,
    now: datetime | None = None,
    prefix: str = DEFAULT_PLANNED_STOP_PREFIX,
) -> dict[str, Any] | None:
    """Return the current marker when it is unexpired and covers an event."""
    marker = store.read(f"{prefix}{agent_name}")
    if not isinstance(marker, dict):
        return None
    requested_at = _parse_datetime(marker.get("requested_at"))
    expires_at = _parse_datetime(marker.get("expires_at"))
    checked_at = (now or _utc_now()).astimezone(UTC)
    event_created_at = event_created_at.astimezone(UTC)
    if requested_at is None or expires_at is None:
        return None
    if requested_at <= event_created_at <= expires_at and checked_at <= expires_at:
        return marker
    return None


def read_process_events(
    store: _StoreItemsReader,
    *,
    prefix: str = DEFAULT_PROCESS_EVENT_PREFIX,
) -> list[dict[str, Any]]:
    """Return process events newest first, tolerating malformed timestamps."""
    process_events = [raw for _key, raw in store.items(prefix=prefix) if isinstance(raw, dict)]
    process_events.sort(
        key=lambda event: _parse_datetime(event.get("created_at")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return process_events


__all__ = [
    "DEFAULT_PLANNED_STOP_PREFIX",
    "DEFAULT_PLANNED_STOP_TTL_SECONDS",
    "DEFAULT_PROCESS_EVENT_PREFIX",
    "planned_stop_covering",
    "read_process_events",
    "write_planned_stop_marker",
]
