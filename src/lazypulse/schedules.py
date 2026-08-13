"""Declarative calendars of recurring work for a :class:`PulseAgent`.

A :class:`Calendar` is a list of named schedule entries declared **in Python**
and reconciled against the Store when the agent starts. The name is the
identity: registering the same calendar twice updates the existing entries
instead of creating duplicates, so a process restart does not multiply a
daily job into three.

Two kinds of entry::

    Calendar([
        Cron("etf_daily_stats", "Run daily ETF stats", "45 15 * * MON-FRI",
             tz="Europe/Rome", action=ActionClass.EXTERNAL_SEND,
             on_days=BusinessDays(holidays=NYSE_2026),
             misfire_grace=timedelta(minutes=45)),
        After("anomaly_check", "Investigate today's anomalies",
              after="etf_daily_stats", within=timedelta(hours=2)),
    ])

:class:`Cron` fires on a cron expression. :class:`After` fires when another
entry's task *completes*, which is what a dependent job actually wants — a
fixed "30 minutes later" is a guess that silently analyses yesterday's data
whenever the predecessor runs long.

Execution semantics follow the durable schedulers rather than the "cron
expression + prompt" model of the agent platforms:

* **misfire grace** — a slot the agent was down for is *skipped* once it is
  later than ``misfire_grace``, instead of firing a market digest at 23:00 on
  the next restart. ``None`` (the default) fires no matter how late.
* **coalesce** — missed slots always collapse into at most one firing. The
  next fire time is computed forward from *now*, never replayed.
* **overlap** — ``"skip"`` (default) does not start a run while the previous
  one is still scheduled/running/awaiting review; ``"allow"`` starts it anyway.
* **day filter** — ``on_days`` skips slots that fall on a non-business day, so
  a daily market job does not wake the agent to analyse a closed session.

The spec lives in code; the Store holds only the *state* (next fire time, last
task, counters, paused flag). ``sync`` merges the two.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator

from lazypulse import store_keys
from lazypulse.models import ActionClass

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from lazybridge import Store

#: A schedule name is a Store key segment, so it must not contain the ``:``
#: separator (which would make ``pulse:schedule:a:b`` ambiguous) or whitespace.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

#: Statuses that mean the previous run of a schedule has not finished yet.
_LIVE_STATUSES = frozenset({"scheduled", "running", "awaiting_review"})


class BusinessDays(BaseModel):
    """Day filter: weekdays only, minus an explicit holiday list.

    ``holidays`` is given as plain dates so the filter carries no market-data
    dependency and round-trips through the Store. Feed it from whatever
    calendar you already trust::

        BusinessDays(holidays=[date(2026, 1, 1), date(2026, 12, 25)])
    """

    kind: Literal["business"] = "business"
    holidays: list[date] = Field(default_factory=list)

    def allows(self, day: date) -> bool:
        """True if ``day`` is a weekday and not a listed holiday."""
        return day.weekday() < 5 and day not in set(self.holidays)


class ScheduleEntry(BaseModel):
    """Fields shared by every kind of schedule entry.

    ``name`` is the stable identity — the Store key, the handle for
    ``pause``/``remove``, and what an :class:`After` entry points at.
    """

    name: str
    text: str
    #: The action class the resulting task is authorized as. Recurring work is
    #: trusted (it comes from your own calendar, not from an inbound message),
    #: but the class is still recorded on the task, so a job that sends
    #: Telegram or email reads as ``EXTERNAL_SEND`` in the ledger rather than
    #: masquerading as a public read.
    action: ActionClass = ActionClass.READ_PUBLIC
    enabled: bool = True
    overlap: Literal["skip", "allow"] = "skip"

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"Invalid schedule name {v!r}: use letters, digits, '_', '.' or '-' "
                "(no ':' or whitespace — the name is a Store key segment)."
            )
        return v


class Cron(ScheduleEntry):
    """An entry that fires on a cron expression.

    Requires the ``cron`` extra (``pip install 'lazypulse[cron]'``). The
    expression is evaluated in ``tz``; everything persisted is UTC.
    """

    kind: Literal["cron"] = "cron"
    expr: str
    tz: str = "UTC"
    #: How late a slot may fire. ``None`` = fire no matter how late (the
    #: pre-calendar behaviour). Set it on anything time-sensitive.
    misfire_grace: timedelta | None = None
    on_days: BusinessDays | None = None

    def __init__(self, name: str = "", text: str = "", expr: str = "", **data: Any) -> None:
        # Positional convenience: Cron("name", "text", "45 15 * * MON-FRI").
        data.update(name=name, text=text, expr=expr)
        super().__init__(**data)

    def trigger(self) -> Any:
        """Build the underlying :class:`~lazypulse.cron.CronTrigger`."""
        from lazypulse.cron import CronTrigger

        return CronTrigger(self.expr, self.tz)

    def next_after(self, after: datetime) -> datetime:
        """Next UTC fire time strictly after ``after``."""
        return self.trigger().next(after)  # type: ignore[no-any-return]

    def local_date(self, moment: datetime) -> date:
        """The calendar date ``moment`` falls on **in this entry's timezone**.

        ``on_days`` is a statement about the local business day, so a 15:45
        Europe/Rome slot must be tested against the Rome date, not the UTC one.
        """
        import zoneinfo

        return moment.astimezone(zoneinfo.ZoneInfo(self.tz)).date()


class After(ScheduleEntry):
    """An entry that fires when another entry's task completes.

    Fires at most once per predecessor run, and only if the predecessor
    completed less than ``within`` ago — past that the occurrence is recorded
    as missed rather than run against stale inputs.
    """

    kind: Literal["after"] = "after"
    after: str
    within: timedelta

    def __init__(self, name: str = "", text: str = "", **data: Any) -> None:
        data.update(name=name, text=text)
        super().__init__(**data)


#: Persisted union of every entry kind, discriminated on ``kind``.
AnyEntry = Cron | After


class ScheduleRecord(BaseModel):
    """One schedule's persisted state: the spec plus its runtime bookkeeping."""

    spec: AnyEntry = Field(discriminator="kind")
    created_at: datetime
    #: True when the entry came from a :class:`Calendar`. Only managed records
    #: are pruned as orphans on sync — an ad-hoc ``schedule_cron`` survives.
    managed: bool = False
    #: ``"agent"`` when the entry was created by the agent through
    #: :class:`~lazypulse.CalendarTools`, ``None`` when it came from code. The
    #: distinction is what the agent's own quota is counted against, and what
    #: makes self-scheduled work auditable in the ledger.
    created_by: str | None = None
    paused: bool = False
    #: Next UTC fire time (cron entries only; ``After`` is event-driven).
    next_fire_at: datetime | None = None
    last_fire_at: datetime | None = None
    #: Task produced by the last firing — the handle for the overlap check.
    last_task_id: str | None = None
    #: ``After`` only: the predecessor task already reacted to, so one
    #: predecessor run can never trigger two dependent runs.
    last_trigger_task_id: str | None = None
    fire_count: int = 0
    missed_count: int = 0
    consecutive_failures: int = 0

    @property
    def name(self) -> str:
        return self.spec.name


class CalendarSync(BaseModel):
    """What :meth:`Calendar.sync` changed, by schedule name."""

    added: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)


class Calendar:
    """An ordered set of uniquely-named schedule entries.

    Pass one to ``PulseAgent(calendar=...)`` and it is reconciled against the
    Store at construction: new names are created, known names keep their
    runtime state (next fire time, counters, paused flag) while adopting the
    declared spec, and managed entries that the calendar no longer declares are
    removed. Declaring the calendar in code is what makes it reviewable — the
    file *is* the source of truth for what the agent does unattended.
    """

    def __init__(self, entries: Sequence[ScheduleEntry] = ()) -> None:
        self._entries: dict[str, AnyEntry] = {}
        for entry in entries:
            if not isinstance(entry, Cron | After):
                raise TypeError(f"Calendar entries must be Cron or After, got {type(entry).__name__}")
            if entry.name in self._entries:
                raise ValueError(
                    f"Duplicate schedule name {entry.name!r}. Names are identities: "
                    "two entries sharing one would overwrite each other in the Store."
                )
            self._entries[entry.name] = entry
        # A typo'd dependency would silently never fire, so resolve the graph now.
        for entry in self._entries.values():
            if isinstance(entry, After) and entry.after not in self._entries:
                raise ValueError(
                    f"Schedule {entry.name!r} depends on {entry.after!r}, which this calendar "
                    f"does not declare. Known names: {sorted(self._entries)}"
                )
            if isinstance(entry, After) and entry.after == entry.name:
                raise ValueError(f"Schedule {entry.name!r} depends on itself.")

    def __iter__(self) -> Iterator[AnyEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    @property
    def names(self) -> list[str]:
        return list(self._entries)

    def sync(self, store: Store, *, now: datetime | None = None) -> CalendarSync:
        """Reconcile this calendar against ``store``. Returns what changed.

        Idempotent: syncing an unchanged calendar rewrites nothing, so two
        processes starting at once cannot shift each other's fire times.
        """
        now = now or datetime.now(UTC)
        result = CalendarSync()

        for name, entry in self._entries.items():
            key = store_keys.schedule_key(name)
            raw = store.read(key)
            if not isinstance(raw, dict):
                record = ScheduleRecord(
                    spec=entry,
                    created_at=now,
                    managed=True,
                    next_fire_at=entry.next_after(now) if isinstance(entry, Cron) else None,
                )
                store.write(key, record.model_dump(mode="json"))
                result.added.append(name)
                continue

            existing = ScheduleRecord.model_validate(raw)
            # Keep the running state; adopt the declared spec. The fire time is
            # only recomputed when the timing actually changed, so a restart
            # never moves an unchanged schedule.
            next_fire_at = existing.next_fire_at
            if isinstance(entry, Cron):
                old = existing.spec
                timing_changed = (
                    not isinstance(old, Cron) or old.expr != entry.expr or old.tz != entry.tz or next_fire_at is None
                )
                if timing_changed:
                    next_fire_at = entry.next_after(now)
            else:
                next_fire_at = None
            # ``created_by`` is cleared: declaring a name in code *takes* it.
            # Leaving it as "agent" would keep CalendarTools' ownership check
            # treating a now-code-declared entry as the model's own, letting it
            # rewrite or delete exactly what the boundary exists to protect.
            updated = existing.model_copy(
                update={"spec": entry, "managed": True, "created_by": None, "next_fire_at": next_fire_at}
            )
            new_raw = updated.model_dump(mode="json")
            if new_raw == raw:
                result.unchanged.append(name)
                continue
            store.write(key, new_raw)
            result.updated.append(name)

        # Orphans: managed records this calendar no longer declares. Ad-hoc
        # entries (managed=False) are left alone — they were never ours.
        for key, raw in _iter_schedule_records(store):
            if not isinstance(raw, dict) or not raw.get("managed"):
                continue
            stored_name = str(raw.get("spec", {}).get("name", ""))
            if stored_name and stored_name not in self._entries:
                store.delete(key)
                result.removed.append(stored_name)

        return result


def _iter_schedule_records(store: Store) -> list[tuple[str, dict[str, Any]]]:
    """Every ``pulse:schedule:*`` record in the Store, as ``(key, raw)``."""
    from lazypulse.tasks import _iter_records

    return _iter_records(store, store_keys.SCHEDULE_PREFIX)


def list_schedules(store: Store) -> list[ScheduleRecord]:
    """Every schedule in the Store, sorted by name. Malformed records are skipped."""
    out: list[ScheduleRecord] = []
    for _key, raw in _iter_schedule_records(store):
        try:
            out.append(ScheduleRecord.model_validate(raw))
        except Exception:  # a corrupt record must not hide the healthy ones
            continue
    return sorted(out, key=lambda r: r.name)


def get_schedule(store: Store, name: str) -> ScheduleRecord | None:
    """One schedule by name, or ``None`` if it does not exist."""
    raw = store.read(store_keys.schedule_key(name))
    if not isinstance(raw, dict):
        return None
    return ScheduleRecord.model_validate(raw)


def _set_paused(store: Store, name: str, paused: bool) -> bool:
    key = store_keys.schedule_key(name)
    raw = store.read(key)
    if not isinstance(raw, dict) or bool(raw.get("paused", False)) == paused:
        return False
    return store.compare_and_swap(key, raw, {**raw, "paused": paused})


def pause_schedule(store: Store, name: str) -> bool:
    """Stop a schedule from firing, keeping its record and counters.

    Returns ``False`` if it does not exist or was already paused. A paused cron
    entry keeps having its fire time advanced past slots that go by *while it is
    held*, so resuming starts from the next real occurrence rather than firing a
    stale one, and never replays a backlog.
    """
    return _set_paused(store, name, True)


def resume_schedule(store: Store, name: str) -> bool:
    """Let a paused schedule fire again. ``False`` if unknown or already active."""
    return _set_paused(store, name, False)


def remove_schedule(store: Store, name: str) -> bool:
    """Delete a schedule. Returns ``False`` if it did not exist.

    A managed entry deleted this way comes back on the next ``Calendar.sync``;
    to retire it for good, remove it from the calendar in code.
    """
    key = store_keys.schedule_key(name)
    if not isinstance(store.read(key), dict):
        return False
    store.delete(key)
    return True
