"""Ambient task context for the running worker.

When a :class:`~lazypulse.PulseAgent` runs a task it sets ``active_task_id``
for the duration of ``Agent.run``. Tools executed by that run can read it to
bind a one-shot send confirmation to the *specific* task it was granted for,
so a grant approved for task A can never be consumed by a concurrent task B.

The value propagates into **async** tools (lazybridge awaits them in the same
context) but not into sync tools (lazybridge runs those in a thread pool with
a fresh context) — which is why the gated send tools are async.
"""

from __future__ import annotations

import contextvars

#: The id of the task currently being executed by a PulseAgent, or ``None``
#: outside a tracked run (e.g. a direct tool call in a test).
active_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("lazypulse_active_task_id", default=None)


def current_task_id() -> str | None:
    """Return the id of the task the worker is currently running, if any."""
    return active_task_id.get()
