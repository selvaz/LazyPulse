"""The Adapter protocol.

An adapter is the bridge between an external message source (an HTTP
endpoint, a Gmail inbox, a queue) and the PulseAgent. Its sole job is to
turn whatever arrived since the last call into a list of
:class:`~lazypulse.models.InboundMessage`.

An adapter is **not** a lazybridge ``ToolProvider``: a tool is a capability
the worker invokes mid-run; an adapter injects work into the loop from the
outside. Distinct roles, distinct types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from lazybridge import Session, Store

    from lazypulse.models import InboundMessage


@runtime_checkable
class Adapter(Protocol):
    """Source of inbound messages.

    Implementations must set a ``name`` attribute (used in events and
    error reporting) and implement :meth:`drain`.

    **drain() must be idempotent.** Calling it twice in quick succession
    must not yield the same message twice — adapters that talk to an
    at-least-once source (Gmail history, an HTTP queue) are responsible for
    recording what they have already emitted (typically in the ``store``)
    so a re-drain returns an empty list. The PulseAgent also dedupes on
    ``message_id`` as a backstop, but adapters should not rely on it.
    """

    name: str

    async def drain(
        self,
        *,
        store: Store,
        session: Session | None,
    ) -> list[InboundMessage]: ...
