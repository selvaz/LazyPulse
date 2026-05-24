"""A thin reviewer client: drain pending review requests from the Store and
answer them.

Pair this with a worker whose verify channel is a HumanEngine driven by a
StoreReviewerUI on the *same* Store (db file). The worker parks requests; this
CLI lists and answers them. Run it on a phone over SSH, a cron job, a Slack
bot — anything that can reach the Store.

    python examples/04_store_review_thin_client.py pulse.db
"""

from __future__ import annotations

import sys

from lazybridge import Store

from lazypulse.review import pending_reviews, respond


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else "pulse.db"
    store = Store(db=db)

    reqs = pending_reviews(store)
    if not reqs:
        print("No pending reviews.")
        return

    for req in reqs:
        print("=" * 60)
        print(f"review_id: {req['review_id']}")
        print(f"requested: {req.get('requested_at')}")
        print(f"task:\n{req['task']}")
        answer = input("Your response (blank to skip): ").strip()
        if answer:
            respond(store, req["review_id"], answer)
            print("  -> recorded")


if __name__ == "__main__":
    main()
