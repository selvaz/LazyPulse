from __future__ import annotations

from datetime import UTC, datetime


class CronTrigger:
    """Cron-expression based trigger.

    Requires the ``cron`` extra (``pip install 'lazypulse[cron]'``).
    Timezone support via :mod:`zoneinfo` (stdlib, Python 3.9+).
    """

    def __init__(self, expr: str, tz: str = "UTC") -> None:
        try:
            import croniter as _croniter  # type: ignore[import-untyped]  # noqa: F401
        except ImportError as exc:
            raise ImportError("CronTrigger requires the 'cron' extra: pip install 'lazypulse[cron]'") from exc
        try:
            import zoneinfo

            self._tzinfo = zoneinfo.ZoneInfo(tz)
        except KeyError as exc:
            raise ValueError(f"Unknown timezone: {tz!r}") from exc
        self._expr = expr
        import croniter  # type: ignore[import-untyped]

        croniter.croniter(expr, datetime.now(UTC))  # validate expression

    def next(self, after: datetime) -> datetime:
        """Return the next fire time after ``after``, as a UTC-aware datetime."""
        import croniter  # type: ignore[import-untyped]

        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)
        after_local = after.astimezone(self._tzinfo).replace(tzinfo=None)
        cron = croniter.croniter(self._expr, after_local)
        next_local = cron.get_next(datetime)
        next_aware = next_local.replace(tzinfo=self._tzinfo)
        return next_aware.astimezone(UTC)
