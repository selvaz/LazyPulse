"""Store-backed human review channel."""

from __future__ import annotations

import asyncio

import pytest
from lazybridge import Agent, Store
from lazybridge.ext.hil import HumanEngine

from lazypulse.review import StoreReviewerUI, pending_reviews, respond


async def test_prompt_returns_response_within_timeout() -> None:
    store = Store()
    ui = StoreReviewerUI(store, poll_interval=0.01, timeout=2.0)
    task = asyncio.create_task(ui.prompt("approve?", tools=[], output_type=str))
    await asyncio.sleep(0.05)
    reqs = pending_reviews(store)
    assert len(reqs) == 1
    respond(store, reqs[0]["review_id"], "approved")
    assert await task == "approved"


async def test_prompt_times_out_without_response() -> None:
    store = Store()
    ui = StoreReviewerUI(store, poll_interval=0.01, timeout=0.05)
    with pytest.raises(TimeoutError):
        await ui.prompt("approve?", tools=[], output_type=str)


async def test_malformed_response_is_ignored_until_valid() -> None:
    store = Store()
    ui = StoreReviewerUI(store, poll_interval=0.01, timeout=2.0)
    task = asyncio.create_task(ui.prompt("approve?", tools=[], output_type=str))
    await asyncio.sleep(0.05)
    review_id = pending_reviews(store)[0]["review_id"]
    # Malformed: missing the "text" key — must not be treated as an answer.
    store.write(f"pulse:review:resp:{review_id}", {"review_id": review_id})
    await asyncio.sleep(0.05)
    assert not task.done()
    # Now answer properly.
    respond(store, review_id, "ok")
    assert await task == "ok"


async def test_pending_reviews_excludes_answered() -> None:
    store = Store()
    ui = StoreReviewerUI(store, poll_interval=0.01, timeout=2.0)
    task = asyncio.create_task(ui.prompt("approve?", tools=[], output_type=str))
    await asyncio.sleep(0.05)
    review_id = pending_reviews(store)[0]["review_id"]
    respond(store, review_id, "done")
    await task
    assert pending_reviews(store) == []


async def test_concurrent_reviews_get_distinct_ids() -> None:
    store = Store()
    ui = StoreReviewerUI(store, poll_interval=0.01, timeout=2.0)
    t1 = asyncio.create_task(ui.prompt("first", tools=[], output_type=str))
    t2 = asyncio.create_task(ui.prompt("second", tools=[], output_type=str))
    await asyncio.sleep(0.05)
    reqs = pending_reviews(store)
    ids = {r["review_id"] for r in reqs}
    assert len(ids) == 2
    for rid, ans in zip(list(ids), ["a", "b"], strict=False):
        respond(store, rid, ans)
    results = await asyncio.gather(t1, t2)
    assert set(results) == {"a", "b"}


async def test_human_engine_with_store_reviewer_ui() -> None:
    # Acceptance: a worker whose verify channel is a HumanEngine driven by
    # the Store, answered by a separate "reviewer" coroutine.
    store = Store()
    ui = StoreReviewerUI(store, poll_interval=0.01, timeout=2.0)
    worker = Agent(name="reviewer", engine=HumanEngine(ui=ui))
    task = asyncio.create_task(worker.run("Approve sending the email?"))
    await asyncio.sleep(0.1)
    review_id = pending_reviews(store)[0]["review_id"]
    respond(store, review_id, "approved")
    env = await task
    assert env.ok
    assert env.text() == "approved"
