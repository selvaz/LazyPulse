"""PulseRecord / InboundMessage round-trip through the Store."""

from __future__ import annotations

from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import store_keys
from lazypulse.models import (
    ActionClass,
    Identity,
    InboundMessage,
    PolicyDecision,
    PulseRecord,
    TickReport,
    TrustLevel,
)


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_pulse_record_defaults() -> None:
    rec = PulseRecord(text="do a thing", created_at=_now(), run_at=_now())
    assert rec.status == "scheduled"
    assert rec.action_class == ActionClass.READ_PUBLIC
    assert rec.restart_count == 0
    assert rec.cost_usd == 0.0
    assert rec.task_id  # uuid populated


def test_pulse_record_unique_task_ids() -> None:
    a = PulseRecord(text="x", created_at=_now(), run_at=_now())
    b = PulseRecord(text="x", created_at=_now(), run_at=_now())
    assert a.task_id != b.task_id


def test_pulse_record_store_roundtrip_memory() -> None:
    store = Store()
    rec = PulseRecord(
        text="hello",
        created_at=_now(),
        run_at=_now(),
        identity=Identity(sender="me@x", trust=TrustLevel.OWNER_VERIFIED_EMAIL),
        decision=PolicyDecision.ALLOW,
    )
    key = store_keys.task_key(rec.task_id)
    store.write(key, rec.model_dump(mode="json"))
    loaded = PulseRecord.model_validate(store.read(key))
    assert loaded == rec


def test_pulse_record_store_roundtrip_sqlite(tmp_path) -> None:
    db = str(tmp_path / "s.db")
    store = Store(db=db)
    rec = PulseRecord(text="persist me", created_at=_now(), run_at=_now())
    key = store_keys.task_key(rec.task_id)
    store.write(key, rec.model_dump(mode="json"))
    store.close()

    store2 = Store(db=db)
    loaded = PulseRecord.model_validate(store2.read(key))
    assert loaded.text == "persist me"
    store2.close()


def test_inbound_message_defaults() -> None:
    msg = InboundMessage(source="webhook", message_id="abc", received_at=_now(), text="hi")
    assert msg.requested_action == ActionClass.READ_PUBLIC
    assert msg.metadata == {}
    assert msg.sender_raw is None


def test_tick_report_counters_start_at_zero() -> None:
    report = TickReport(at=_now())
    assert (report.drained, report.completed, report.rejected, report.recovered) == (0, 0, 0, 0)
