# Plan as engine: deterministic routing

A PulseAgent's engine can be a `lazybridge.Plan`. Because the tick loop
dispatches through the ordinary `Agent.run()` path, a Plan engine needs **no
special handling** — every step runs, and routing is honoured.

## Triage-then-route

The canonical pattern: a cheap structured triage step decides a category,
then `routes_by` jumps to the specialist sub-agent whose **step name equals
that category value**.

```python
from typing import Literal
from pydantic import BaseModel
from lazybridge import Agent, LLMEngine, Plan, Step
from lazypulse import PulseAgent
from lazypulse.adapters.gmail import GmailInbox, GmailPolicy

class Triage(BaseModel):
    # Each Literal value must match a downstream Step name.
    category: Literal["research", "calendar", "draft_reply"]
    confidence: float

triager   = Agent(name="triager",   engine=LLMEngine("claude-haiku-4-5"), output=Triage)
research  = Agent(name="research",  engine=LLMEngine("claude-opus-4-7"), tools=[web_search])
calendar  = Agent(name="calendar",  engine=LLMEngine("claude-opus-4-7"), tools=[calendar_tools])
draft_reply = Agent(name="draft_reply", engine=LLMEngine("claude-opus-4-7"))

pulse = PulseAgent(
    name="router",
    engine=Plan(
        Step("triager", output=Triage, routes_by="category"),
        Step("research"),
        Step("calendar"),
        Step("draft_reply"),
    ),
    tools=[triager, research, calendar, draft_reply],
    policy=GmailPolicy(owner_emails=["me@example.com"]),
    adapters=[GmailInbox(...)],
    store=store,
    session=session,
)
```

`routes_by="category"` requires that every `Literal` value of the
`category` field is the name of a declared step. Routing is a **detour**:
the router jumps to the matched step, runs it, then linear progression
resumes from that step's declared position.

For predicate-based routing instead of a model field, use
`Step("triager", routes={"research": when.field("category").equals("research"), ...})`.

## Per-task isolation

Each inbound message gets its own `PulseRecord` (a unique `task_id`), and the
Plan runs fresh per task. That is the isolation that matters for a pulse
loop: task-level lifecycle and crash recovery live in the Store, independent
of any single Plan run.

## A note on checkpoints (vs. the original v3 sketch)

The v3 plan sketched passing `plan_state={"checkpoint_key": ..., "resume": False}`
into the engine per task. That does not match lazybridge's API: `Plan`'s
`checkpoint_key` and `resume` are **constructor** arguments, and the
`plan_state=` run-kwarg is a `PlanState` *dataclass* used for resume — not a
config dict. Moreover, `Agent.run()` deliberately does not thread `store` /
`plan_state` to the engine.

So LazyPulse keeps the simpler, correct contract: **dispatch uniformly
through `self.run()`**. This respects the "never override `run()`" constraint
and means Plan routing works out of the box. If you need mid-Plan,
step-level resume across a crash, construct the Plan with its own
`checkpoint_key` and `store` (use `on_concurrent="fork"` so concurrent tasks
get isolated keys) — that is a lazybridge-level configuration on the engine
you pass in, orthogonal to the pulse loop.
