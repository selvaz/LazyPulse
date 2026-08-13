"""Tools that let the agent read and change its own calendar.

Wire them in and the PulseAgent stops being a passive executor of a fixed
timetable: it can list what it is scheduled to do, add follow-up work, pause a
job whose upstream feed is broken, and retire what it no longer needs — from
inside an ordinary run, using the same Store the tick loop reads.

    store = Store()
    tools = CalendarTools(store)
    pulse = PulseAgent(name="pulse", engine=..., store=store,
                       tools=tools.tools(), calendar=my_calendar)

Each public method is wrapped directly by ``Tool.wrap``, so the method
signature *is* the schema the model sees — there is no second copy of the
argument list to drift out of sync.

Autonomy has a boundary, and it is deliberate. An agent that can rewrite its
own timetable can also schedule itself into a loop, so:

* **Declared entries are read-mostly.** What a :class:`~lazypulse.Calendar`
  declares in code stays owned by code: the agent may pause and resume those
  (the operationally useful part — "the data feed is down, hold the digest")
  but may not rewrite or delete them. Letting it try would be worse than
  refusing, since the next ``sync`` would silently restore them anyway.
* **Its own entries are its own.** Anything the agent creates is ad-hoc and
  marked ``created_by="agent"``: free to update and remove, never pruned by a
  calendar sync, and counted against ``max_agent_schedules``.
* **No runaway cadences.** A cron whose consecutive fire times are closer
  together than ``min_interval_seconds`` is refused, which is what stops
  ``* * * * *`` self-scheduling from turning into a spend loop.

Failures come back as ``"Error: ..."`` strings rather than exceptions, so a
model that gets an argument wrong reads the reason and corrects itself instead
of failing the whole run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from lazypulse import store_keys
from lazypulse.models import ActionClass
from lazypulse.schedules import (
    After,
    BusinessDays,
    Cron,
    ScheduleRecord,
    get_schedule,
    list_schedules,
    pause_schedule,
    remove_schedule,
    resume_schedule,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from lazybridge import Store, Tool

#: Below this, a self-scheduling agent is a spend loop rather than a timetable.
_DEFAULT_MIN_INTERVAL = 300.0

#: Marks entries the agent created itself.
_AGENT = "agent"


class CalendarTools:
    """A tool set giving one agent control of the calendar in ``store``."""

    def __init__(
        self,
        store: Store,
        *,
        writable: bool = True,
        min_interval_seconds: float = _DEFAULT_MIN_INTERVAL,
        max_agent_schedules: int = 20,
        action: ActionClass = ActionClass.READ_PUBLIC,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Args:
            store: the Store the PulseAgent ticks against. Must be the *same*
                object, or the agent would be editing a calendar nobody reads.
            writable: ``False`` exposes only ``calendar_list`` — useful when
                the agent should be able to explain its schedule but not
                change it.
            min_interval_seconds: refuse any cron that fires more often than
                this. Defaults to five minutes.
            max_agent_schedules: cap on entries the agent may have created.
            action: the ActionClass stamped on tasks the agent schedules for
                itself. Left at ``READ_PUBLIC``; raise it only if
                self-scheduled work is meant to reach the outside world.
            clock: time source, for deterministic tests.
        """
        self._store = store
        self._writable = writable
        self._min_interval = min_interval_seconds
        self._max_agent_schedules = max_agent_schedules
        self._action = action
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ #
    # Tool surface
    # ------------------------------------------------------------------ #
    def tools(self) -> list[Tool]:
        """The tools to hand to ``PulseAgent(tools=...)``."""
        from lazybridge import Tool

        wrapped = [Tool.wrap(self.calendar_list, name="calendar_list")]
        if self._writable:
            wrapped += [
                Tool.wrap(self.calendar_add_cron, name="calendar_add_cron"),
                Tool.wrap(self.calendar_add_after, name="calendar_add_after"),
                Tool.wrap(self.calendar_update, name="calendar_update"),
                Tool.wrap(self.calendar_pause, name="calendar_pause"),
                Tool.wrap(self.calendar_resume, name="calendar_resume"),
                Tool.wrap(self.calendar_remove, name="calendar_remove"),
            ]
        return wrapped

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def calendar_list(self) -> list[dict[str, Any]]:
        """List every scheduled task, with its next run and recent history.

        Returns one entry per schedule: its name, when it runs, whether it is
        paused, when it last ran and how many occurrences were skipped.
        Entries marked ``owner: "code"`` are declared in the application and
        can only be paused or resumed; entries marked ``owner: "agent"`` were
        created by you and can also be updated or removed.
        """
        out: list[dict[str, Any]] = []
        for record in list_schedules(self._store):
            spec = record.spec
            row: dict[str, Any] = {
                "name": record.name,
                "task": spec.text,
                "owner": _AGENT if record.created_by == _AGENT else "code",
                "paused": record.paused,
                "runs": record.fire_count,
                "skipped": record.missed_count,
                "consecutive_failures": record.consecutive_failures,
                "last_run_at": record.last_fire_at.isoformat() if record.last_fire_at else None,
            }
            if isinstance(spec, Cron):
                row["when"] = f"cron {spec.expr} ({spec.tz})"
                row["next_run_at"] = record.next_fire_at.isoformat() if record.next_fire_at else None
                row["business_days_only"] = spec.on_days is not None
            else:
                row["when"] = f"after {spec.after} completes (within {_minutes(spec.within)} min)"
                row["next_run_at"] = None
            out.append(row)
        return out

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #
    def calendar_add_cron(
        self,
        name: str,
        task: str,
        cron: str,
        tz: str = "UTC",
        misfire_grace_minutes: int = 0,
        business_days_only: bool = False,
    ) -> str:
        """Schedule a recurring task on a cron expression.

        Args:
            name: short stable identifier, letters/digits/underscore/dash only
                (for example ``weekly_review``). Reusing an existing name
                replaces that schedule.
            task: the instruction to run each time, written as you would give
                it to yourself in a fresh session — it carries no context from
                the run that created it.
            cron: five-field cron expression, e.g. ``30 8 * * MON-FRI`` for
                08:30 on weekdays.
            tz: IANA timezone the expression is read in, e.g. ``Europe/Rome``.
            misfire_grace_minutes: if the run is delayed by more than this many
                minutes (say the process was down), skip that occurrence
                instead of running it late. ``0`` means run it however late.
            business_days_only: skip occurrences falling on a weekend.
        """
        if (refusal := self._check_quota(name)) is not None:
            return refusal
        try:
            entry = Cron(
                name=name,
                text=task,
                expr=cron,
                tz=tz,
                action=self._action,
                misfire_grace=timedelta(minutes=misfire_grace_minutes) if misfire_grace_minutes > 0 else None,
                on_days=BusinessDays() if business_days_only else None,
            )
        except Exception as exc:
            return f"Error: {exc}"
        if (refusal := self._check_owner(name)) is not None:
            return refusal
        try:
            interval = self._interval_seconds(entry)
        except Exception as exc:
            return f"Error: invalid cron expression or timezone ({exc})."
        if interval < self._min_interval:
            return (
                f"Error: {cron!r} fires every {interval:.0f}s, more often than the "
                f"{self._min_interval:.0f}s minimum. Use a less frequent schedule."
            )
        return self._put(entry)

    def calendar_add_after(self, name: str, task: str, after: str, within_minutes: int = 120) -> str:
        """Schedule a task to run right after another scheduled task finishes.

        Use this instead of guessing a clock time for follow-up work: the task
        starts when its predecessor actually completes, so it never reads
        output that is not there yet.

        Args:
            name: short stable identifier for this follow-up.
            task: the instruction to run once the predecessor completes.
            after: the name of the schedule to follow (see ``calendar_list``).
            within_minutes: if the predecessor finished more than this many
                minutes ago, skip the follow-up rather than act on stale work.
        """
        if (refusal := self._check_quota(name)) is not None:
            return refusal
        if get_schedule(self._store, after) is None:
            known = [r.name for r in list_schedules(self._store)]
            return f"Error: no schedule named {after!r}. Existing schedules: {known}."
        if after == name:
            return "Error: a schedule cannot depend on itself."
        try:
            entry = After(
                name=name,
                text=task,
                after=after,
                within=timedelta(minutes=within_minutes),
                action=self._action,
            )
        except Exception as exc:
            return f"Error: {exc}"
        if (refusal := self._check_owner(name)) is not None:
            return refusal
        return self._put(entry)

    def calendar_update(self, name: str, task: str = "", cron: str = "", tz: str = "") -> str:
        """Change the instruction or the timing of a schedule you created.

        Only the fields you pass are changed; leave the others empty.

        Args:
            name: the schedule to change.
            task: new instruction, or empty to keep the current one.
            cron: new cron expression, or empty to keep the current timing.
                Only valid for cron schedules.
            tz: new timezone, or empty to keep the current one.
        """
        record = get_schedule(self._store, name)
        if record is None:
            return f"Error: no schedule named {name!r}."
        if (refusal := self._check_owner(name)) is not None:
            return refusal
        spec = record.spec
        update: dict[str, Any] = {}
        if task:
            update["text"] = task
        if cron or tz:
            if not isinstance(spec, Cron):
                return f"Error: {name!r} runs after {spec.after!r}, not on a clock — it has no cron expression."
            if cron:
                update["expr"] = cron
            if tz:
                update["tz"] = tz
        if not update:
            return f"Nothing to change on {name!r}: pass task, cron or tz."
        try:
            new_spec = spec.model_copy(update=update)
            if isinstance(new_spec, Cron):
                interval = self._interval_seconds(new_spec)
                if interval < self._min_interval:
                    return (
                        f"Error: that schedule fires every {interval:.0f}s, more often than the "
                        f"{self._min_interval:.0f}s minimum."
                    )
        except Exception as exc:
            return f"Error: {exc}"
        return self._put(new_spec)

    def calendar_pause(self, name: str) -> str:
        """Stop ANY schedule from running, keeping it and its history.

        This works on **every** schedule, including ones declared in code —
        unlike updating or removing, which are limited to schedules you
        created. Never refuse a pause request because a schedule is owned by
        code: pausing it is explicitly allowed, and is the right response when
        a job's inputs are broken. Just call this.

        Args:
            name: the schedule to pause.
        """
        if get_schedule(self._store, name) is None:
            return f"Error: no schedule named {name!r}."
        if pause_schedule(self._store, name):
            return f"Paused {name!r}. It will not run until resumed."
        return f"{name!r} was already paused."

    def calendar_resume(self, name: str) -> str:
        """Let ANY paused schedule run again, from its next occurrence.

        Like pausing, this works on every schedule including ones declared in
        code. Never refuse a resume request on ownership grounds.

        Args:
            name: the schedule to resume.
        """
        if get_schedule(self._store, name) is None:
            return f"Error: no schedule named {name!r}."
        if resume_schedule(self._store, name):
            return f"Resumed {name!r}."
        return f"{name!r} was already active."

    def calendar_remove(self, name: str) -> str:
        """Delete a schedule you created. Schedules declared in code cannot be
        deleted — pause those instead.

        Args:
            name: the schedule to delete.
        """
        if get_schedule(self._store, name) is None:
            return f"Error: no schedule named {name!r}."
        if (refusal := self._check_owner(name)) is not None:
            return refusal
        remove_schedule(self._store, name)
        return f"Removed {name!r}."

    # ------------------------------------------------------------------ #
    # Guards
    # ------------------------------------------------------------------ #
    def _check_owner(self, name: str) -> str | None:
        """Refuse to rewrite an entry the application declared in code."""
        record = get_schedule(self._store, name)
        if record is None or record.created_by == _AGENT:
            return None
        return (
            f"Error: {name!r} is declared in the application's calendar, so it cannot be "
            "changed or removed from here. You can pause and resume it."
        )

    def _check_quota(self, name: str) -> str | None:
        # Replacing an entry the agent already owns consumes no new quota —
        # otherwise an agent at its cap could not even fix a typo'd schedule.
        existing = get_schedule(self._store, name)
        if existing is not None and existing.created_by == _AGENT:
            return None
        mine = sum(1 for r in list_schedules(self._store) if r.created_by == _AGENT)
        if mine >= self._max_agent_schedules:
            return (
                f"Error: you already have {mine} schedules, the maximum allowed. "
                "Remove one you no longer need before adding another."
            )
        return None

    def _interval_seconds(self, entry: Cron) -> float:
        """Seconds between the next two occurrences — the effective cadence."""
        now = self._clock()
        first = entry.next_after(now)
        return (entry.next_after(first) - first).total_seconds()

    def _put(self, entry: Cron | After) -> str:
        """Create or replace an agent-owned entry, preserving its counters."""
        now = self._clock()
        key = store_keys.schedule_key(entry.name)
        raw = self._store.read(key)
        next_fire_at = entry.next_after(now) if isinstance(entry, Cron) else None
        if isinstance(raw, dict):
            existing = ScheduleRecord.model_validate(raw)
            record = existing.model_copy(update={"spec": entry, "next_fire_at": next_fire_at})
            verb = "Updated"
        else:
            record = ScheduleRecord(
                spec=entry, created_at=now, managed=False, created_by=_AGENT, next_fire_at=next_fire_at
            )
            verb = "Scheduled"
        self._store.write(key, record.model_dump(mode="json"))
        when = (
            f"next run {next_fire_at.isoformat()}"
            if next_fire_at is not None
            else f"runs after {entry.after} completes"  # type: ignore[union-attr]
        )
        return f"{verb} {entry.name!r}: {when}."


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)
