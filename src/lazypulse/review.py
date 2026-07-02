"""Store-backed human review channel.

``StoreReviewerUI`` implements the same ``prompt`` shape as lazybridge's
``HumanEngine`` terminal/web UIs, but routes the request through the Store
instead of a terminal. That makes human-in-the-loop work for a background
PulseAgent: the worker (``verify=Agent(engine=HumanEngine(ui=StoreReviewerUI(store)))``)
parks a review request in the Store and blocks; a separate reviewer process
— a CLI, a phone, a web form — drains pending requests and writes responses
back. Neither side needs to share a terminal or even a machine.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lazypulse import store_keys

if TYPE_CHECKING:
    from lazybridge import Store


class StoreReviewerUI:
    """A ``HumanEngine`` UI that exchanges prompts/answers via the Store."""

    def __init__(
        self,
        store: Store,
        *,
        poll_interval: float = 2.0,
        timeout: float = 3600.0,
    ) -> None:
        self.store = store
        self.poll_interval = poll_interval
        self.timeout = timeout

    async def prompt(self, task: str, *, tools: list[Any], output_type: type) -> str:
        review_id = str(uuid.uuid4())
        req_key = store_keys.REVIEW_REQ.format(review_id=review_id)
        self.store.write(
            req_key,
            {
                "review_id": review_id,
                "task": task,
                "tools": [getattr(t, "name", str(t)) for t in tools],
                "output_type": getattr(output_type, "__name__", str(output_type)),
                "requested_at": datetime.now(UTC).isoformat(),
            },
        )
        deadline = time.monotonic() + self.timeout
        resp_key = store_keys.REVIEW_RESP.format(review_id=review_id)
        while time.monotonic() < deadline:
            resp = self.store.read(resp_key)
            if isinstance(resp, dict) and "text" in resp:
                # Settled: drop both records so an always-on agent's Store
                # doesn't accumulate one req/resp pair per review forever.
                self.store.delete(req_key)
                self.store.delete(resp_key)
                return str(resp["text"])
            await asyncio.sleep(self.poll_interval)
        # Timed out: withdraw the request so it stops showing as pending.
        # A reviewer answering after this leaves an orphaned (tiny) response
        # record; the worker it was meant for is already gone either way.
        self.store.delete(req_key)
        raise TimeoutError(f"Review {review_id} timed out after {self.timeout}s")


def pending_reviews(store: Store) -> list[dict[str, Any]]:
    """Return every review request that has no response yet."""
    from lazypulse.tasks import _iter_records

    out: list[dict[str, Any]] = []
    for _key, req in _iter_records(store, store_keys.REVIEW_REQ_PREFIX):
        review_id = req.get("review_id")
        if review_id is None:
            continue
        if store.read(store_keys.REVIEW_RESP.format(review_id=review_id)) is None:
            out.append(req)
    return out


def respond(store: Store, review_id: str, text: str) -> None:
    """Answer a pending review request."""
    store.write(
        store_keys.REVIEW_RESP.format(review_id=review_id),
        {"review_id": review_id, "text": text, "responded_at": datetime.now(UTC).isoformat()},
    )
