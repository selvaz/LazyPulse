"""End-to-end: PulseAgent's task context reaches a lazytools GmailTools send.

The GmailTools provider moved to lazytoolkit; these tests verify the integration
that stays LazyPulse's responsibility — that a task-bound send grant is matched
against the *running task id* PulseAgent publishes (via lazytools.safety's
ambient scope), exercising PulseAgent -> Agent.run -> engine -> Tool.run.
"""

from __future__ import annotations

from typing import Any

from lazybridge import Store
from lazytools.connectors.gmail import GmailTools
from lazytools.testing import FakeGmailService

from lazypulse import PulseAgent, PulseRecord, store_keys


class _SendingEngine:
    """Minimal engine that invokes gmail_send once through the real lazybridge
    tool-execution path so task-context propagation is tested end-to-end."""

    def __init__(self, to: str) -> None:
        self._to = to

    async def run(self, env: Any, *, tools: list[Any], output_type: type, **_: Any) -> Any:
        from lazybridge import Envelope

        send = next(t for t in tools if t.name == "gmail_send")
        out: Envelope[Any] = Envelope.from_task(env.task or "")
        out.payload = await send.run(to=self._to, subject="s", body="b")
        return out

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
        yield  # pragma: no cover


async def test_task_bound_grant_consumed_e2e_via_pulse_agent() -> None:
    svc = FakeGmailService()
    tools = GmailTools(svc, allowed_recipients=["a@x.com"])
    pulse = PulseAgent(name="p", engine=_SendingEngine("a@x.com"), store=Store(), tools=[tools])
    task_id = pulse.schedule("send it")
    tools.confirm_send(to="a@x.com", task_id=task_id)  # bound to this very task
    await pulse.tick_once()
    assert len(svc.sent) == 1  # the worker's task id reached the tool


async def test_task_bound_grant_for_other_task_blocks_send_e2e() -> None:
    svc = FakeGmailService()
    tools = GmailTools(svc, allowed_recipients=["a@x.com"])
    pulse = PulseAgent(name="p", engine=_SendingEngine("a@x.com"), store=Store(), tools=[tools])
    task_id = pulse.schedule("send it")
    tools.confirm_send(to="a@x.com", task_id="some-other-task")  # not this run's id
    await pulse.tick_once()
    assert svc.sent == []  # grant for a different task is not spendable here
    rec = PulseRecord.model_validate(pulse.store.read(store_keys.task_key(task_id)))
    assert rec.status == "failed"  # the send raised GmailSendBlocked
