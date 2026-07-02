"""v0.1 JSON without new fields deserialises cleanly under the v0.2 model."""

from __future__ import annotations

from lazypulse.models import PulseRecord


def _v01_record() -> dict:
    return {
        "task_id": "abc-123",
        "text": "hello",
        "status": "scheduled",
        "created_at": "2026-01-01T00:00:00+00:00",
        "run_at": "2026-01-01T00:00:00+00:00",
        "action_class": "read_public",
    }


def test_v01_json_deserialises() -> None:
    r = PulseRecord.model_validate(_v01_record())
    assert r.attempt == 0
    assert r.next_retry_at is None
    assert r.rate_limited is False
    assert r.error_type is None


def test_record_with_removed_route_field_deserialises() -> None:
    # ``route`` was declared in 0.2/0.3 but never written by the runtime; it
    # was removed. Persisted records that carry it must still load (pydantic
    # ignores unknown keys by default).
    r = PulseRecord.model_validate({**_v01_record(), "route": "some-route"})
    assert not hasattr(r, "route")


def test_v01_json_roundtrip() -> None:
    r = PulseRecord.model_validate(_v01_record())
    dumped = r.model_dump(mode="json")
    r2 = PulseRecord.model_validate(dumped)
    assert r2.attempt == r.attempt
    assert r2.next_retry_at == r.next_retry_at
    assert r2.rate_limited == r.rate_limited
