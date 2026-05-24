"""GmailTools: draft is free, send is guarded."""

from __future__ import annotations

from typing import Any

import pytest

from lazypulse.adapters.gmail.tools import GmailSendBlocked, GmailTools


class FakeService:
    def __init__(self) -> None:
        self.drafts: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.drafts.append({"to": to, "subject": subject, "body": body})
        return {"id": "draft-1"}

    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"id": "sent-1"}


def _tools(svc: FakeService, **kw: Any) -> tuple[Any, Any]:
    provider = GmailTools(svc, **kw)
    by_name = {t.name: t for t in provider.as_tools()}
    return provider, by_name


def test_as_tools_exposes_both() -> None:
    svc = FakeService()
    _, by_name = _tools(svc)
    assert set(by_name) == {"gmail_create_draft", "gmail_send"}


def test_provider_is_tool_provider() -> None:
    assert GmailTools(FakeService())._is_lazy_tool_provider is True


def test_create_draft_is_not_blocked() -> None:
    svc = FakeService()
    provider, _ = _tools(svc)
    out = provider._create_draft(to="a@x.com", subject="hi", body="b")
    assert "draft created" in out
    assert len(svc.drafts) == 1


async def test_send_without_confirmation_blocked() -> None:
    svc = FakeService()
    provider, _ = _tools(svc)
    with pytest.raises(GmailSendBlocked, match="no outstanding confirmation"):
        await provider._send(to="a@x.com", subject="hi", body="b")
    assert svc.sent == []


async def test_confirm_once_authorizes_exactly_one_send() -> None:
    svc = FakeService()
    provider, _ = _tools(svc)
    provider.confirm_once()
    await provider._send(to="a@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1
    # The single grant is now spent — a second send is blocked.
    with pytest.raises(GmailSendBlocked):
        await provider._send(to="a@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1


async def test_confirm_send_is_bound_to_recipient() -> None:
    svc = FakeService()
    provider, _ = _tools(svc)
    provider.confirm_send(to="alice@x.com")
    # A grant for alice does not authorize a send to bob.
    with pytest.raises(GmailSendBlocked):
        await provider._send(to="bob@x.com", subject="hi", body="b")
    assert svc.sent == []
    # ...but it does authorize exactly one send to alice.
    await provider._send(to="alice@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1
    with pytest.raises(GmailSendBlocked):
        await provider._send(to="alice@x.com", subject="hi", body="b")


async def test_send_respects_recipient_allowlist() -> None:
    svc = FakeService()
    provider, _ = _tools(svc, allowed_recipients=["ok@x.com"])
    provider.confirm_once()
    with pytest.raises(GmailSendBlocked, match="allow-list"):
        await provider._send(to="evil@y.com", subject="hi", body="b")
    assert svc.sent == []


async def test_send_to_allowed_recipient_succeeds() -> None:
    svc = FakeService()
    provider, _ = _tools(svc, allowed_recipients=["ok@x.com"])
    provider.confirm_send(to="ok@x.com")
    await provider._send(to="ok@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1


async def test_require_confirmation_false_allows_send() -> None:
    svc = FakeService()
    provider, _ = _tools(svc, require_confirmation=False)
    await provider._send(to="a@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1


# --- Task-bound grants ------------------------------------------------- #


async def test_task_bound_grant_only_consumed_by_that_task() -> None:
    from lazypulse._context import active_task_id

    svc = FakeService()
    provider, _ = _tools(svc)
    provider.confirm_send(to="a@x.com", task_id="TASK-A")

    # A different task running concurrently cannot spend task A's grant.
    token = active_task_id.set("TASK-B")
    try:
        with pytest.raises(GmailSendBlocked):
            await provider._send(to="a@x.com", subject="hi", body="b")
    finally:
        active_task_id.reset(token)
    assert svc.sent == []

    # The matching task consumes it exactly once.
    token = active_task_id.set("TASK-A")
    try:
        await provider._send(to="a@x.com", subject="hi", body="b")
        with pytest.raises(GmailSendBlocked):
            await provider._send(to="a@x.com", subject="hi", body="b")
    finally:
        active_task_id.reset(token)
    assert len(svc.sent) == 1


async def test_task_bound_grant_not_consumed_outside_a_task() -> None:
    # With no active task context, a task-bound grant must not be spendable.
    svc = FakeService()
    provider, _ = _tools(svc)
    provider.confirm_once(task_id="TASK-A")
    with pytest.raises(GmailSendBlocked):
        await provider._send(to="a@x.com", subject="hi", body="b")
    assert svc.sent == []


async def test_unbound_grant_still_works_for_any_task() -> None:
    from lazypulse._context import active_task_id

    svc = FakeService()
    provider, _ = _tools(svc)
    provider.confirm_once()  # no task_id → backward-compatible, any task
    token = active_task_id.set("TASK-X")
    try:
        await provider._send(to="a@x.com", subject="hi", body="b")
    finally:
        active_task_id.reset(token)
    assert len(svc.sent) == 1


class _SendingEngine:
    """Minimal engine that invokes gmail_send once — exercises the real
    lazybridge tool-execution path so the task-context propagation is tested
    end-to-end (PulseAgent → Agent.run → engine → Tool.run → async _send)."""

    def __init__(self, to: str) -> None:
        self._to = to
        self._agent_name: str | None = None

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
    from lazybridge import Store

    from lazypulse import PulseAgent

    svc = FakeService()
    tools = GmailTools(svc, allowed_recipients=["a@x.com"])
    pulse = PulseAgent(name="p", engine=_SendingEngine("a@x.com"), store=Store(), tools=[tools])
    task_id = pulse.schedule("send it")
    tools.confirm_send(to="a@x.com", task_id=task_id)  # bound to this very task
    await pulse.tick_once()
    assert len(svc.sent) == 1  # the worker's task id reached the tool


async def test_task_bound_grant_for_other_task_blocks_send_e2e() -> None:
    from lazybridge import Store

    from lazypulse import PulseAgent, PulseRecord, store_keys

    svc = FakeService()
    tools = GmailTools(svc, allowed_recipients=["a@x.com"])
    pulse = PulseAgent(name="p", engine=_SendingEngine("a@x.com"), store=Store(), tools=[tools])
    task_id = pulse.schedule("send it")
    tools.confirm_send(to="a@x.com", task_id="some-other-task")  # not this run's id
    await pulse.tick_once()
    assert svc.sent == []  # grant for a different task is not spendable here
    rec = PulseRecord.model_validate(pulse.store.read(store_keys.task_key(task_id)))
    assert rec.status == "failed"  # the send raised GmailSendBlocked
