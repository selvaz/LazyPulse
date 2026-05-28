from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff retry policy for PulseAgent workers.

    Parameters
    ----------
    max_attempts:
        Total number of attempts allowed (1 = no retries).
    backoff_base:
        Base for exponential backoff: delay = min(base ** attempt, backoff_max).
    backoff_max:
        Upper cap on the backoff delay in seconds.
    retry_on:
        Exception types that trigger a retry. Defaults to ``(Exception,)``.
    """

    max_attempts: int = 1
    backoff_base: float = 2.0
    backoff_max: float = 300.0
    retry_on: tuple[type[BaseException], ...] = field(default_factory=lambda: (Exception,))

    def next_delay(self, attempt: int) -> float:
        """Delay in seconds before attempt number ``attempt``."""
        return min(self.backoff_base**attempt, self.backoff_max)

    def should_retry(self, attempt: int, exc: BaseException) -> bool:
        """True when the attempt count and exception type both permit a retry."""
        return self.should_retry_by_count(attempt) and isinstance(exc, self.retry_on)

    def should_retry_by_count(self, attempt: int) -> bool:
        """True when there are still attempts remaining."""
        return attempt < self.max_attempts - 1
