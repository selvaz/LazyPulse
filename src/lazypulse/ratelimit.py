from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RateLimit:
    """Per-sender rate limit for inbound message intake.

    Parameters
    ----------
    max_per_sender:
        Maximum messages allowed per sender per ``window_seconds``.
    window_seconds:
        Rolling window size in seconds.
    on_exceeded:
        Action when the limit is exceeded: ``"reject"`` drops the message,
        ``"queue"`` routes it to human review.
    """

    max_per_sender: int = 10
    window_seconds: int = 3600
    on_exceeded: Literal["reject", "queue"] = "reject"
