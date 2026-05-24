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

    from lazypulse.models import InboundMessage, PulseRecord


@runtime_checkable
class Adapter(Protocol):
    """Source of inbound messages.

    Implementations must set a ``name`` attribute (used in events and
    error reporting) and implement :meth:`drain`.

    **drain() is at-least-once.** An adapter may re-emit a message until
    the PulseAgent has durably recorded it (the central
    ``store_keys.EVENT`` marker exists for that ``message_id``). This
    makes crashes between drain and record-write safe: the next poll
    re-emits and the message is recorded. The PulseAgent deduplicates on
    ``message_id``, so a message still becomes at most one task. Adapters
    that have their own delivered/acked state should honour it to avoid
    unnecessary work, but correctness never depends on them being
    idempotent.
    """

    name: str

    async def drain(
        self,
        *,
        store: Store,
        session: Session | None,
    ) -> list[InboundMessage]: ...


@runtime_checkable
class Responder(Protocol):
    """An adapter that can send a reply back to a message's origin.

    Optional: implement it on an adapter for a *conversational* channel. When
    a task completes, the PulseAgent routes the worker's ``worker_text`` back
    to the originating conversation (e.g. the Telegram chat the message came
    from) via :meth:`reply`.

    Replying to the sender that was already authorized to reach the worker
    needs **no extra confirmation** — it is a direct response to authorized
    inbound, not an outbound send to a new recipient (which the tool layer
    still gates). The reply is best-effort: a failure never un-completes the
    task.
    """

    name: str

    async def reply(
        self,
        record: PulseRecord,
        text: str,
        *,
        store: Store,
        session: Session | None,
    ) -> None: ...
