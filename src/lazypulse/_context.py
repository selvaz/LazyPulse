"""Ambient task context for the running worker.

When a :class:`~lazypulse.PulseAgent` runs a task it sets ``active_task_id``
for the duration of ``Agent.run``. Guarded tools executed by that run (e.g.
``lazytools`` GmailTools / TelegramTools) read it to bind a one-shot send
confirmation to the *specific* task it was granted for, so a grant approved for
task A can never be consumed by a concurrent task B.

The underlying contextvar is :data:`lazytools.safety.active_scope` whenever
``lazytoolkit`` is installed, so PulseAgent and the moved tools share a single
context object. Without ``lazytoolkit`` there are no ``lazytools`` tools to
bind, so a local fallback contextvar is used instead. This keeps
``lazytoolkit`` an optional dependency of LazyPulse.

The value propagates into **async** tools (lazybridge awaits them in the same
context) but not into sync tools (run in a fresh thread context) — which is why
the gated send tools are async.
"""

from __future__ import annotations

try:
    from lazytools.safety import active_scope, current_scope
except ImportError:  # lazytoolkit not installed — no guarded lazytools tools
    import contextvars

    active_scope = contextvars.ContextVar("lazypulse_active_task_id", default=None)

    def current_scope() -> str | None:
        return active_scope.get()


#: Back-compat aliases — PulseAgent sets ``active_task_id``; tools read it.
active_task_id = active_scope


def current_task_id() -> str | None:
    """Return the id of the task the worker is currently running, if any."""
    return current_scope()


__all__ = ["active_task_id", "current_task_id", "active_scope", "current_scope"]
