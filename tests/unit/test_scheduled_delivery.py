"""Opt-in delivery of completed recurring schedule output."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lazybridge import Store

from lazypulse import ActionClass, Calendar, Cron, InboundMessage, PulseAgent, store_keys
from lazypulse.models import PulseRecord
from lazypulse.testing import FakeClock, MockEngine

_START = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)


def _task(store: Store | None, task_id: str | None) -> PulseRecord:
    assert store is not None
    assert task_id is not None
    return PulseRecord.model_validate(store.read(store_keys.task_key(task_id)))


def test_existing_serialized_task_defaults_to_no_scheduled_delivery() -> None:
    task = PulseRecord.model_validate(
        {
            "task_id": "old-task",
            "text": "work",
            "status": "scheduled",
            "created_at": _START.isoformat(),
            "run_at": _START.isoformat(),
        }
    )

    assert task.notify is False
    assert task.schedule_name is None


def test_toml_calendar_accepts_notify_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "calendar.toml"
    path.write_text(
        """
[schedules.digest]
task = "Build digest"
cron = "0 9 * * *"
action = "external_send"
notify = true
""".strip(),
        encoding="utf-8",
    )

    entry = next(iter(Calendar.from_toml(path)))

    assert entry.notify is True
    assert entry.action == ActionClass.EXTERNAL_SEND


async def test_notify_false_keeps_cron_silent_with_responder_configured() -> None:
    calls: list[tuple[str, str, str]] = []
    clock = FakeClock(start=_START)
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["finished"]),
        store=Store(),
        clock=clock,
        calendar=Calendar([Cron("silent", "work", "0 * * * *")]),
        scheduled_responder=lambda *args: calls.append(args),
    )

    clock.advance(3600)
    report = await pulse.tick_once()

    assert report.completed == 1
    assert calls == []
    schedule = pulse.get_schedule("silent")
    assert schedule is not None
    task = _task(pulse.store, schedule.last_task_id)
    assert task.notify is False
    assert task.schedule_name == "silent"


async def test_notify_true_calls_sync_responder_once_with_completed_task_context() -> None:
    calls: list[tuple[str, str, str]] = []
    clock = FakeClock(start=_START)
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["daily result"]),
        store=Store(),
        clock=clock,
        calendar=Calendar(
            [
                Cron(
                    "digest",
                    "work",
                    "0 * * * *",
                    notify=True,
                    action=ActionClass.EXTERNAL_SEND,
                )
            ]
        ),
        scheduled_responder=lambda *args: calls.append(args),
    )

    clock.advance(3600)
    report = await pulse.tick_once()

    assert report.completed == 1
    schedule = pulse.get_schedule("digest")
    assert schedule is not None
    assert calls == [("daily result", schedule.last_task_id, "digest")]
    task = _task(pulse.store, schedule.last_task_id)
    assert task.status == "completed"
    assert task.worker_text == "daily result"
    assert task.notify is True
    assert task.schedule_name == "digest"
    assert task.action_class == ActionClass.EXTERNAL_SEND


async def test_notify_true_without_responder_is_a_noop() -> None:
    clock = FakeClock(start=_START)
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["finished"]),
        store=Store(),
        clock=clock,
        calendar=Calendar([Cron("unwired", "work", "0 * * * *", notify=True)]),
    )

    clock.advance(3600)
    report = await pulse.tick_once()

    assert report.completed == 1
    schedule = pulse.get_schedule("unwired")
    assert schedule is not None
    assert _task(pulse.store, schedule.last_task_id).worker_text == "finished"


async def test_failing_async_scheduled_responder_does_not_break_completion(
    caplog: Any,
) -> None:
    async def broken_responder(worker_text: str, task_id: str, schedule_name: str) -> None:
        raise RuntimeError("delivery unavailable")

    clock = FakeClock(start=_START)
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["stored first"]),
        store=Store(),
        clock=clock,
        calendar=Calendar([Cron("failing_delivery", "work", "0 * * * *", notify=True)]),
        scheduled_responder=broken_responder,
    )

    clock.advance(3600)
    with caplog.at_level(logging.ERROR, logger="lazypulse.pulse_agent"):
        report = await pulse.tick_once()

    assert report.completed == 1
    schedule = pulse.get_schedule("failing_delivery")
    assert schedule is not None
    task = _task(pulse.store, schedule.last_task_id)
    assert task.status == "completed"
    assert task.worker_text == "stored first"
    assert "Scheduled responder failed" in caplog.text
    assert "delivery unavailable" in caplog.text


async def test_inbound_responder_path_does_not_call_scheduled_responder() -> None:
    class TelegramStyleAdapter:
        name = "telegram"

        def __init__(self) -> None:
            self.drained = False
            self.replies: list[tuple[str, str]] = []

        async def drain(self, *, store: Store, session: Any | None) -> list[InboundMessage]:
            if self.drained:
                return []
            self.drained = True
            return [
                InboundMessage(
                    source=self.name,
                    message_id="message-1",
                    received_at=_START,
                    text="ping",
                )
            ]

        async def reply(
            self,
            record: PulseRecord,
            text: str,
            *,
            store: Store,
            session: Any | None,
        ) -> None:
            self.replies.append((record.task_id, text))

    scheduled_calls: list[tuple[str, str, str]] = []
    adapter = TelegramStyleAdapter()
    pulse = PulseAgent(
        name="p",
        engine=MockEngine(["pong"]),
        store=Store(),
        clock=FakeClock(start=_START),
        adapters=[adapter],
        unsafe_allow_all=True,
        scheduled_responder=lambda *args: scheduled_calls.append(args),
    )

    report = await pulse.tick_once()

    assert report.completed == 1
    assert len(adapter.replies) == 1
    assert adapter.replies[0][1] == "pong"
    assert scheduled_calls == []
