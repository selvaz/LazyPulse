"""Plan-as-engine: deterministic routing instead of an LLM orchestrator.

A PulseAgent whose engine is a ``Plan`` first triages each message with a
cheap structured call, then routes — via ``routes_by`` — to the matching
specialist sub-agent. No orchestration tokens spent; the routing is code.

Runs offline with MockAgent sub-agents (no API key needed), fully synchronous.

    python examples/05_plan_routing_deterministico.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from lazybridge import Plan, Step, Store
from lazybridge.testing import MockAgent
from pydantic import BaseModel

from lazypulse import InboundMessage, PulseAgent, PulseRecord, store_keys
from lazypulse.testing import MockAdapter


class Triage(BaseModel):
    # Each Literal value MUST equal a downstream step name so routes_by can
    # jump to it.
    category: Literal["research", "calendar"]
    confidence: float


def main() -> None:
    # In production: triager = Agent(engine=LLMEngine("claude-haiku-4-5"), output=Triage)
    triager = MockAgent(Triage(category="calendar", confidence=0.9), name="triager", output=Triage)
    research = MockAgent("Researched the topic.", name="research")
    calendar = MockAgent("Checked your calendar.", name="calendar")

    store = Store()
    pulse = PulseAgent(
        name="router",
        engine=Plan(
            Step("triager", output=Triage, routes_by="category"),
            Step("research"),
            Step("calendar"),
        ),
        tools=[triager, research, calendar],
        store=store,
        adapters=[
            MockAdapter(
                [
                    InboundMessage(
                        source="mock",
                        message_id="1",
                        received_at=datetime.now(UTC),
                        text="what's on my schedule tomorrow?",
                    )
                ]
            )
        ],
        unsafe_allow_all=True,  # dev only
    )

    pulse.tick()  # one synchronous beat: drain → route → run

    for key in list(store.keys()):
        if key.startswith(store_keys.TASK_PREFIX):
            rec = PulseRecord.model_validate(store.read(key))
            print(f"routed result: {rec.worker_text!r}")


if __name__ == "__main__":
    main()
