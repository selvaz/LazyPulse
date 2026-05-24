"""Contract tests that pin LazyPulse's coupling to lazybridge internals.

LazyPulse imports only lazybridge's public API (enforced by
``test_no_private_imports``), but it still depends on two *behavioural*
contracts that are not expressed as imported symbols and would otherwise
break silently when lazybridge changes within the allowed version range:

1. A blocked ``Guard`` surfaces as an Envelope whose ``error.type`` is the
   literal string ``"GuardBlocked"`` — ``PulseAgent._finalize`` keys on that
   string to record the task as ``rejected`` rather than ``failed``.
2. ``HumanEngine(ui=...)`` drives a custom UI via ``ui.prompt(task, *, tools,
   output_type)`` — the exact shape ``StoreReviewerUI`` reimplements.

If either contract changes upstream, these tests fail loudly in CI instead of
LazyPulse misclassifying guard rejections or the Store review channel going
silently dead.
"""

from __future__ import annotations

from typing import Any

from lazybridge import Agent, ContentGuard, GuardAction, Store
from lazybridge.ext.hil import HumanEngine

from lazypulse import PulseAgent, PulseRecord, store_keys
from lazypulse.testing import MockEngine


def _record_for(store: Store, task_id: str) -> PulseRecord:
    raw = store.read(store_keys.task_key(task_id))
    assert isinstance(raw, dict)
    return PulseRecord.model_validate(raw)


def test_guard_block_surfaces_as_guardblocked_error_type() -> None:
    # Pins the literal "GuardBlocked" that _finalize matches on. A guard that
    # blocks output must produce env.error.type == "GuardBlocked".
    import asyncio

    guard = ContentGuard(output_fn=lambda _text: GuardAction.block("blocked by test"))
    agent = Agent(name="guarded", engine=MockEngine(["hello"]), guard=guard)
    env = asyncio.run(agent.run("do something"))
    assert not env.ok
    assert env.error is not None
    assert env.error.type == "GuardBlocked"


def test_pulse_agent_records_guard_block_as_rejected() -> None:
    # The end-to-end consequence of the contract above: a guard-blocked task
    # lands as ``rejected`` (a policy outcome), not ``failed`` (a crash).
    store = Store()
    guard = ContentGuard(output_fn=lambda _text: GuardAction.block("nope"))
    pulse = PulseAgent(name="t", engine=MockEngine(["hello"]), store=store, guard=guard)
    task_id = pulse.schedule("do something")
    pulse.tick()
    rec = _record_for(store, task_id)
    assert rec.status == "rejected"
    assert rec.error is not None and "nope" in rec.error


def test_human_engine_drives_custom_ui_prompt_shape() -> None:
    # Pins HumanEngine(ui=...).prompt(task, *, tools, output_type) — the shape
    # StoreReviewerUI implements. A signature change upstream breaks here.
    import asyncio

    seen: dict[str, Any] = {}

    class SpyUI:
        async def prompt(self, task: str, *, tools: list[Any], output_type: type) -> str:
            seen["task"] = task
            seen["tools"] = tools
            seen["output_type"] = output_type
            return "approved"

    worker = Agent(name="reviewer", engine=HumanEngine(ui=SpyUI()))
    env = asyncio.run(worker.run("Approve sending the email?"))
    assert env.ok
    assert env.text() == "approved"
    # The UI was invoked positionally on the task, with tools/output_type as
    # keyword arguments — exactly StoreReviewerUI's signature.
    assert seen["task"] == "Approve sending the email?"
    assert seen["tools"] == []
    assert seen["output_type"] is str
