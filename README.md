# LazyPulse

Always-on agents on top of [lazybridge](https://github.com/selvaz/LazyBridge).

A `PulseAgent` **is** a `lazybridge.Agent` — same `engine=`, `tools=`,
`guard=`, `verify=`, `memory=`, `store=`, `session=`, `output=`, `fallback=`
— plus three additions:

1. a background **tick loop** (`start()` / `stop()` / `running()`),
2. an optional **policy** that authorizes inbound messages before any worker
   runs (trust resolution × action class),
3. a list of **adapters** that feed messages in from the outside world
   (HTTP webhook, Gmail, or your own).

Because it subclasses `Agent` and dispatches through the ordinary
`self.run()` path, every lazybridge engine works unchanged — including
`Plan` for deterministic routing — and every future `Agent` improvement
propagates for free.

## Install

```bash
pip install lazypulse                 # core: tick loop + policy + custom adapters
pip install 'lazypulse[webhook]'      # + HTTP intake (starlette)
pip install 'lazypulse[gmail]'        # + Gmail polling and draft/send tools
pip install 'lazypulse[dev]'          # test + lint toolchain
```

The Gmail and webhook integrations are optional extras — a bare
`pip install lazypulse` never downloads the Google client libraries or
starlette.

## Quickstart

```python
import asyncio
from datetime import datetime, timezone

from lazybridge import Store
from lazypulse import PulseAgent, InboundMessage
from lazypulse.testing import MockEngine, MockAdapter

async def main():
    store = Store()
    pulse = PulseAgent(
        name="pulse",
        engine=MockEngine(["handled"]),   # swap for LLMEngine("claude-opus-4-7")
        store=store,
        adapters=[MockAdapter([
            InboundMessage(source="mock", message_id="1",
                           received_at=datetime.now(timezone.utc), text="hello"),
        ])],
        tick_seconds=0.05,
    )
    async with pulse.running():
        await asyncio.sleep(0.3)

asyncio.run(main())
```

## Concepts

| Concept | What it is |
|---|---|
| `PulseAgent(Agent)` | Subclass adding the tick loop, policy and adapters. |
| `PulsePolicy` | Pre-execution authorization: `classify()` → `Identity`, then `authorize()` → `PolicyDecision`. **Not** a `Guard`. |
| `Adapter` | An object with `.drain()` that yields `InboundMessage`s. **Not** a `ToolProvider`. |
| `PulseRecord` | One ledger entry per task; its `status` lives in the `Store`. |
| `StoreReviewerUI` | A `HumanEngine` UI that routes review through the `Store` instead of a terminal. |

## Trust × action matrix

The default policy is conservative — unknown senders get nothing, verified
owners get local reads/writes, and sensitive actions (external send,
destructive) require explicit confirmation. See
[`docs/security.md`](docs/security.md) for the threat model.

## License

Apache-2.0
