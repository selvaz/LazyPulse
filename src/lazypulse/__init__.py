"""LazyPulse — always-on agents on top of lazybridge.

A :class:`PulseAgent` is an ordinary :class:`lazybridge.Agent` with a
background tick loop. Each tick drains its :class:`Adapter` list for new
messages, runs each through a :class:`PulsePolicy` (trust + authorization),
and dispatches the authorized ones to the agent's normal ``run`` path. Task
lifecycle lives in the lazybridge ``Store``.

Quickstart::

    import asyncio
    from lazybridge import Store
    from lazypulse import PulseAgent, InboundMessage
    from lazypulse.testing import MockEngine, MockAdapter
    from datetime import datetime, timezone

    async def main():
        store = Store()
        pulse = PulseAgent(
            name="pulse",
            engine=MockEngine(["handled"]),
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

The Gmail adapter (``GmailInbox``, ``GmailPolicy``, ``GmailTools``) and the
HTTP ``WebhookAdapter`` are imported lazily so ``import lazypulse`` never
pulls their optional dependencies (the Google client libraries / starlette).
Install with ``pip install 'lazypulse[gmail]'`` or ``'lazypulse[webhook]'``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lazypulse import store_keys
from lazypulse.adapters.base import Adapter
from lazypulse.models import (
    ActionClass,
    Identity,
    InboundMessage,
    PolicyDecision,
    PulseRecord,
    TickReport,
    TrustLevel,
)
from lazypulse.policy import DEFAULT_ACTION_RULES, PulsePolicy
from lazypulse.pulse_agent import PulseAgent
from lazypulse.review import StoreReviewerUI, pending_reviews, respond

__version__ = "0.1.0"

if TYPE_CHECKING:
    from lazypulse.adapters.gmail import GmailInbox, GmailInboxConfig, GmailPolicy, GmailTools
    from lazypulse.adapters.webhook import WebhookAdapter

# Symbols served lazily by ``__getattr__`` so importing them does not force
# the optional extra to be installed unless the user actually reaches for it.
_LAZY: dict[str, tuple[str, str]] = {
    "WebhookAdapter": ("lazypulse.adapters.webhook", "webhook"),
    "GmailInbox": ("lazypulse.adapters.gmail", "gmail"),
    "GmailInboxConfig": ("lazypulse.adapters.gmail", "gmail"),
    "GmailPolicy": ("lazypulse.adapters.gmail", "gmail"),
    "GmailTools": ("lazypulse.adapters.gmail", "gmail"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        module_path, extra = _LAZY[name]
        try:
            import importlib

            module = importlib.import_module(module_path)
        except ImportError as exc:  # missing optional dependency
            raise ImportError(
                f"{name} requires the '{extra}' extra. Install it with: pip install 'lazypulse[{extra}]'"
            ) from exc
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Agent
    "PulseAgent",
    # Policy
    "PulsePolicy",
    "Identity",
    "TrustLevel",
    "ActionClass",
    "PolicyDecision",
    "DEFAULT_ACTION_RULES",
    # Models
    "InboundMessage",
    "PulseRecord",
    "TickReport",
    # Adapters
    "Adapter",
    "WebhookAdapter",
    "GmailInbox",
    "GmailInboxConfig",
    "GmailPolicy",
    "GmailTools",
    # Review
    "StoreReviewerUI",
    "pending_reviews",
    "respond",
    # Keys
    "store_keys",
]
