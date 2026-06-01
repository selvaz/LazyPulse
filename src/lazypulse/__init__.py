"""LazyPulse — always-on agents on top of lazybridge.

A :class:`PulseAgent` is an ordinary :class:`lazybridge.Agent` with a
background tick loop. Each tick drains its :class:`Adapter` list for new
messages, runs each through a :class:`PulsePolicy` (trust + authorization),
and dispatches the authorized ones to the agent's normal ``run`` path. Task
lifecycle lives in the lazybridge ``Store``.

Quickstart (synchronous — no asyncio in your code)::

    import time
    from datetime import datetime, timezone
    from lazybridge import Store
    from lazypulse import PulseAgent, InboundMessage
    from lazypulse.testing import MockEngine, MockAdapter

    store = Store()
    pulse = PulseAgent(
        name="pulse",
        engine=MockEngine(["handled"]),
        store=store,
        adapters=[MockAdapter([
            InboundMessage(source="mock", message_id="1",
                           received_at=datetime.now(timezone.utc), text="hello"),
        ])],
        unsafe_allow_all=True,   # dev only — pass policy=... in production
        tick_seconds=0.05,
    )
    with pulse.running():        # background loop; stops on block exit
        time.sleep(0.3)
    # ...or pulse.serve() to block until Ctrl-C, or pulse.tick() for one beat.

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
from lazypulse.ratelimit import RateLimit
from lazypulse.retry import RetryPolicy
from lazypulse.review import StoreReviewerUI, pending_reviews, respond
from lazypulse.tasks import approve_task, pending_tasks, purge_terminal_tasks, reject_task

__version__ = "0.2.0"

if TYPE_CHECKING:
    from lazypulse.adapters.gmail import GmailInbox, GmailInboxConfig, GmailPolicy, GmailTools
    from lazypulse.adapters.telegram import (
        TelegramInbox,
        TelegramInboxConfig,
        TelegramPolicy,
        TelegramTools,
    )
    from lazypulse.adapters.webhook import WebhookAdapter
    from lazypulse.cron import CronTrigger

# Symbols served lazily by ``__getattr__`` so importing them does not force
# the optional extra to be installed unless the user actually reaches for it.
# ``GmailTools`` / ``TelegramTools`` now live in ``lazytools.connectors.*`` (the
# adapters package only re-exports them via a deprecation shim); resolve them
# straight from their new home so ``from lazypulse import GmailTools`` is a clean
# convenience re-export and does not emit the shim's DeprecationWarning.
_LAZY: dict[str, tuple[str, str]] = {
    "WebhookAdapter": ("lazypulse.adapters.webhook", "webhook"),
    "GmailInbox": ("lazypulse.adapters.gmail", "gmail"),
    "GmailInboxConfig": ("lazypulse.adapters.gmail", "gmail"),
    "GmailPolicy": ("lazypulse.adapters.gmail", "gmail"),
    "GmailTools": ("lazytools.connectors.gmail", "gmail"),
    "TelegramInbox": ("lazypulse.adapters.telegram", "telegram"),
    "TelegramInboxConfig": ("lazypulse.adapters.telegram", "telegram"),
    "TelegramPolicy": ("lazypulse.adapters.telegram", "telegram"),
    "TelegramTools": ("lazytools.connectors.telegram", "telegram"),
    "CronTrigger": ("lazypulse.cron", "cron"),
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
    # Retry / cron / rate-limit
    "RetryPolicy",
    "CronTrigger",
    "RateLimit",
    # Adapters
    "Adapter",
    "WebhookAdapter",
    "GmailInbox",
    "GmailInboxConfig",
    "GmailPolicy",
    "GmailTools",
    "TelegramInbox",
    "TelegramInboxConfig",
    "TelegramPolicy",
    "TelegramTools",
    # Review (HumanEngine channel)
    "StoreReviewerUI",
    "pending_reviews",
    "respond",
    # Task review queue (awaiting_review lifecycle)
    "pending_tasks",
    "approve_task",
    "reject_task",
    "purge_terminal_tasks",
    # Keys
    "store_keys",
]
