"""Regression tests for the audit fixes:

* C2 — crash recovery must not reset a task this process is actively running.
* P4 — cron enumeration must fall back cleanly on a store whose ``items()``
  predates the ``prefix=`` keyword (or has no ``items()`` at all).
* Rate-bucket pruning — closed per-sender rate windows are reclaimed.
* Intake hooks — ``action_classifier`` (re-label intent) and ``command_filter``
  (consume operator commands) behave and default to off.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lazybridge import Store

from lazypulse import InboundMessage, PulseAgent, store_keys
from lazypulse.models import ActionClass, Identity, PulseRecord, TickReport, TrustLevel
from lazypulse.policy import PulsePolicy
from lazypulse.tasks import _iter_records, purge_stale_rate_buckets
from lazypulse.testing import FakeClock, MockAdapter, MockEngine

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _record(store: Store, task_id: str) -> PulseRecord:
    return PulseRecord.model_validate(store.read(store_keys.task_key(task_id)))


# --- C2: recovery skips in-flight tasks -------------------------------- #


def test_recover_stale_skips_inflight_task() -> None:
    clock = FakeClock()
    store = Store()
    pulse = PulseAgent(name="p", engine=MockEngine(["x"]), store=store, clock=clock, stale_after=300)

    rec = PulseRecord(text="slow", status="running", created_at=clock.now, run_at=clock.now, started_at=clock.now)
    key = store_keys.task_key(rec.task_id)
    store.write(key, rec.model_dump(mode="json"))
    pulse._inflight.add(key)  # this process is actively running it

    clock.advance(1000)  # well past stale_after
    report = TickReport(at=clock.now)
    pulse._recover_stale(clock.now, report)

    assert report.recovered == 0
    assert _record(store, rec.task_id).status == "running"  # not reset → no double-run

    # Once no longer in-flight (the process really did crash), it IS recovered.
    pulse._inflight.discard(key)
    pulse._recover_stale(clock.now, report)
    assert report.recovered == 1
    assert _record(store, rec.task_id).status == "scheduled"


# --- P4: cron enumeration fallback ------------------------------------- #


class _ItemsRejectsPrefix:
    """A duck-typed store whose ``items()`` predates the ``prefix=`` keyword."""

    def __init__(self, data: dict) -> None:
        self._d = data

    def items(self, **kwargs):
        if "prefix" in kwargs:
            raise TypeError("items() got an unexpected keyword argument 'prefix'")
        return list(self._d.items())

    def keys(self):
        return list(self._d.keys())

    def read(self, key):
        return self._d.get(key)


def test_iter_records_falls_back_when_items_rejects_prefix() -> None:
    store = _ItemsRejectsPrefix({"pulse:schedule:s1": {"a": 1}, "pulse:task:x": {"b": 2}, "other": {"c": 3}})
    out = _iter_records(store, store_keys.SCHEDULE_PREFIX)
    assert [k for k, _ in out] == ["pulse:schedule:s1"]


async def test_cron_fires_and_does_not_abort_tick() -> None:
    clock = FakeClock()
    store = Store()  # lazybridge Store here has no items() at all → keys() fallback
    pulse = PulseAgent(name="p", engine=MockEngine(["ok"]), store=store, clock=clock)
    pulse.schedule_cron("recurring", "recurring", "* * * * *")  # every minute
    clock.advance(61)
    report = await pulse.tick_once()
    assert report.completed >= 1  # cron fired, task ran — the tick did not abort


# --- Rate-bucket pruning ----------------------------------------------- #


def test_purge_stale_rate_buckets_reclaims_closed_windows() -> None:
    store = Store()
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    cur = int(now.timestamp()) // 60
    store.write(f"pulse:rate:alice:{cur - 5}", {"count": 3})  # closed window
    store.write(f"pulse:rate:alice:{cur}", {"count": 1})  # current window
    store.write("pulse:task:x", {"status": "completed"})  # unrelated key

    deleted = purge_stale_rate_buckets(store, window_seconds=60, now=now)

    assert deleted == 1
    assert store.read(f"pulse:rate:alice:{cur - 5}") is None
    assert store.read(f"pulse:rate:alice:{cur}") is not None  # open window kept
    assert store.read("pulse:task:x") is not None  # non-rate key untouched


async def test_prune_reclaims_rate_buckets_during_tick() -> None:
    from lazypulse.ratelimit import RateLimit

    clock = FakeClock()
    store = Store()
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["x"]),
        store=store,
        clock=clock,
        terminal_retention=3600,
        policy=PulsePolicy(rate_limit=RateLimit(window_seconds=60)),
    )
    cur = int(clock.now.timestamp()) // 60
    store.write(f"pulse:rate:bob:{cur - 10}", {"count": 5})  # stale
    clock.advance(120)  # past a window boundary and the prune throttle
    report = await pulse.tick_once()
    assert report.pruned >= 1
    assert store.read(f"pulse:rate:bob:{cur - 10}") is None


async def test_prune_reclaims_rate_buckets_without_terminal_retention() -> None:
    # Rate-limited agent that never set terminal_retention must still reclaim
    # closed rate buckets — otherwise pulse:rate:* grows forever (the prune was
    # previously gated behind terminal_retention).
    from lazypulse.ratelimit import RateLimit

    clock = FakeClock()
    store = Store()
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["x"]),
        store=store,
        clock=clock,
        policy=PulsePolicy(rate_limit=RateLimit(window_seconds=60)),  # no terminal_retention
    )
    cur = int(clock.now.timestamp()) // 60
    store.write(f"pulse:rate:bob:{cur - 10}", {"count": 5})  # stale
    clock.advance(120)
    report = await pulse.tick_once()
    assert report.pruned >= 1
    assert store.read(f"pulse:rate:bob:{cur - 10}") is None


# --- Intake hooks ------------------------------------------------------ #


class _OwnerPolicy(PulsePolicy):
    def classify(self, inbound: InboundMessage) -> Identity:
        return Identity(sender=inbound.sender_raw, trust=TrustLevel.OWNER_VERIFIED_EMAIL)


def _owner_msg(text: str, mid: str = "1", action: str = "read_public") -> InboundMessage:
    return InboundMessage(
        source="mock",
        message_id=mid,
        received_at=NOW,
        sender_raw="owner",
        text=text,
        requested_action=action,  # type: ignore[arg-type]
        metadata={"user_id": 42},
    )


async def test_action_classifier_upgrades_intent_to_review() -> None:
    clock = FakeClock()
    store = Store()
    engine = MockEngine(["x"])

    def classify(msg: InboundMessage) -> ActionClass:
        return ActionClass.EXTERNAL_SEND if "send" in msg.text else ActionClass.READ_PUBLIC

    pulse = PulseAgent(
        name="p",
        engine=engine,
        store=store,
        clock=clock,
        policy=_OwnerPolicy(),
        action_classifier=classify,
        adapters=[MockAdapter([_owner_msg("please send the report")])],
    )
    report = await pulse.tick_once()
    # READ_PUBLIC would auto-run; the classifier re-labels it EXTERNAL_SEND, so
    # an owner send parks for confirmation instead.
    assert report.queued_for_review == 1
    assert len(engine.calls) == 0


async def test_command_filter_consumes_message_without_creating_task() -> None:
    clock = FakeClock()
    store = Store()
    engine = MockEngine(["x"])
    seen: list[str] = []

    def is_command(msg: InboundMessage) -> bool:
        if msg.text.startswith("/cmd"):
            seen.append(msg.text)
            return True
        return False

    pulse = PulseAgent(
        name="p",
        engine=engine,
        store=store,
        clock=clock,
        unsafe_allow_all=True,
        command_filter=is_command,
        adapters=[MockAdapter([_owner_msg("/cmd approve x", mid="c1"), _owner_msg("do the work", mid="w1")])],
    )
    report = await pulse.tick_once()

    assert seen == ["/cmd approve x"]
    # The command produced no task; only the real work message ran.
    task_texts = [
        PulseRecord.model_validate(store.read(k)).text for k in store if k.startswith(store_keys.TASK_PREFIX)
    ]
    assert task_texts == ["do the work"]
    assert engine.calls == ["do the work"]
    # The command's event marker exists so the at-least-once adapter won't re-emit it.
    assert store.read(store_keys.event_key("c1")) is not None
    assert report.completed == 1
