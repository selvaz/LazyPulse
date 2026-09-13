"""Structured, UI-agnostic readers for supervised-fleet telemetry.

Promoted from LazyCEO because correlating durable registry intent, Pulse task
state, session activity, process events, and live OS discovery is generic
always-on infrastructure.  The snapshots keep declared, process, and
operational state separate so missing telemetry and lifecycle mismatches stay
visible to any UI rather than being flattened into one misleading boolean.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from lazypulse.fleet.events import read_process_events
from lazypulse.fleet.registry import DEFAULT_AGENT_PREFIX, AgentRecord
from lazypulse.tasks import list_tasks

DEFAULT_AGENT_STATE_DIR = Path("C:/ProgramData/lazypulse/fleet")
PROCESS_QUERY_CACHE_TTL_SECONDS = 7.5

#: Ceiling on how far back `_activity_for` will widen its search past
#: skipped status-poll events -- see that function for why it widens at all.
_MAX_ACTIVITY_LOOKBACK = 2000


class StoreReader(Protocol):
    """Read-only Store surface required by fleet inspection."""

    def read(self, key: str, default: Any = None) -> Any: ...

    def items(self, *, prefix: str | None = None) -> list[tuple[str, Any]]: ...


class ReadOnlySQLiteStore:
    """LazyBridge Store-compatible reads over a strictly read-only SQLite URI."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.uri = self.path.as_uri() + "?mode=ro"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def read(self, key: str, default: Any = None) -> Any:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM store WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def items(self, *, prefix: str | None = None) -> list[tuple[str, Any]]:
        with self._connect() as connection:
            if prefix:
                rows = connection.execute(
                    "SELECT key, value FROM store WHERE substr(key, 1, ?) = ?",
                    (len(prefix), prefix),
                ).fetchall()
            else:
                rows = connection.execute("SELECT key, value FROM store").fetchall()
        return [(str(row["key"]), json.loads(row["value"])) for row in rows]


@dataclass(frozen=True)
class ActivitySnapshot:
    event_id: int
    event_type: str
    tool_name: str | None
    timestamp: float


@dataclass(frozen=True)
class AgentSnapshot:
    """One fleet member; ``role`` uses the caller's own vocabulary."""

    agent_id: str
    #: Typically the caller's own naming, e.g. a privileged always-on agent
    #: versus an ordinary one; the fleet layer does not prescribe an enum.
    role: str
    declared_status: str
    process_state: Literal["running", "not_running", "unknown"]
    operational_state: Literal["working", "idle", "down", "stopped", "telemetry_missing"]
    current_task: str | None
    last_activity: ActivitySnapshot | None
    latest_process_event: dict[str, Any] | None
    telemetry_errors: list[str]


@dataclass(frozen=True)
class FleetSnapshot:
    observed_at: datetime
    agents: list[AgentSnapshot]


def read_session_events(session_db: str | Path, *, limit: int = 3) -> list[ActivitySnapshot]:
    """Read the newest persisted events without creating a Session or DB."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    path = Path(session_db).resolve()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT id, event_type, payload, ts FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    session_events: list[ActivitySnapshot] = []
    for event_id, event_type, payload_text, timestamp in rows:
        try:
            payload = json.loads(payload_text)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
        session_events.append(
            ActivitySnapshot(
                event_id=int(event_id),
                event_type=str(event_type),
                tool_name=str(tool_name) if tool_name is not None else None,
                timestamp=float(timestamp),
            )
        )
    return session_events


def _run_specialist_process_query() -> str | None:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                "| Select-Object -ExpandProperty CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        # `check=False` never raises on a nonzero exit -- WMI unavailable,
        # access denied, etc. can all exit nonzero with EMPTY stdout, which
        # would otherwise be indistinguishable from "the query genuinely
        # found zero running python.exe processes" and make every
        # specialist read as a confirmed not_running (a false fleet-wide
        # outage), instead of the honest "process query unavailable" this
        # function's None return already means for every OTHER failure
        # mode. Found by Codex review before this ever shipped.
        if result.returncode != 0:
            return None
        return result.stdout
    except Exception:
        return None


_process_query_lock = threading.Lock()
_process_query_cached_at = 0.0
_process_query_cached_value: str | None = None
_process_query_has_value = False
_process_query_runner_id: int | None = None


def clear_process_query_cache() -> None:
    global _process_query_cached_at, _process_query_cached_value, _process_query_has_value, _process_query_runner_id
    with _process_query_lock:
        _process_query_cached_at = 0.0
        _process_query_cached_value = None
        _process_query_has_value = False
        _process_query_runner_id = None


def query_running_specialist_cmdlines() -> str | None:
    """Return a cached live query of running Python process command lines."""
    global _process_query_cached_at, _process_query_cached_value, _process_query_has_value, _process_query_runner_id
    now = time.monotonic()
    runner_id = id(subprocess.run)
    with _process_query_lock:
        if (
            _process_query_has_value
            and _process_query_runner_id == runner_id
            and now - _process_query_cached_at < PROCESS_QUERY_CACHE_TTL_SECONDS
        ):
            return _process_query_cached_value
        value = _run_specialist_process_query()
        _process_query_cached_at = time.monotonic()
        _process_query_cached_value = value
        _process_query_has_value = True
        _process_query_runner_id = runner_id
        return value


def _agent_store_path(state_dir: Path, name: str) -> Path:
    return state_dir / f"{name}.sqlite"


def _agent_session_path(state_dir: Path, name: str) -> Path:
    store_path = _agent_store_path(state_dir, name)
    return store_path.with_name(f"{store_path.stem}.session.sqlite")


def _cmdline_mentions(store_path: Path, running_cmdlines: str | object) -> bool:
    """Whether an agent's own store path appears in a raw process-cmdline dump.

    Both sides go through :func:`os.path.normcase` first. A ``Path``-built
    string always carries native separators and whatever case its state
    directory happens to be spelled in, while the real command line carries
    whatever the launcher actually typed -- so a plain substring check
    reports a false "not running" for a live process pointing at the very
    same file, purely because one side used ``/`` or a different case. Found
    live in the project this was promoted from (its ``list_specialists``/
    ``fleet_status`` reported a permanent false MISMATCH for any specialist
    launched by hand); ``normcase`` is the one call that fixes both slash
    style and case on Windows, and is a harmless no-op on POSIX.
    """
    needle = os.path.normcase(str(store_path))
    haystack = os.path.normcase(str(running_cmdlines))
    return needle in haystack


def _activity_for(
    session_db: str | Path | None,
    errors: list[str],
    *,
    skip_fleet_status: bool = False,
) -> ActivitySnapshot | None:
    if session_db is None:
        errors.append("session telemetry missing")
        return None
    # Any FIXED window, combined with skipping fleet_status calls, can hide
    # real prior activity entirely: a self-supervising agent inspected N
    # times in a row pushes its last genuine event past the end of an
    # N-sized window, and it reads as "no activity" when the session
    # actually has plenty. So when filtering is active the window WIDENS
    # until either a non-skipped event is found or a full read came back
    # with fewer rows than asked for (i.e. the session is exhausted, so
    # there is genuinely nothing else to find). Bounded by _MAX_LOOKBACK
    # rather than unbounded, since a session db is read live and a
    # pathological one should not stall a status poll. Found by Codex
    # review before this ever shipped (twice: first the fixed 3, then the
    # fixed 20 that replaced it).
    windows = (20, 200, _MAX_ACTIVITY_LOOKBACK) if skip_fleet_status else (3,)
    for limit in windows:
        try:
            session_events = read_session_events(session_db, limit=limit)
        except (OSError, sqlite3.Error, ValueError) as exc:
            errors.append(f"session telemetry missing ({type(exc).__name__})")
            return None
        for event in session_events:
            if skip_fleet_status and event.event_type == "tool_call" and event.tool_name == "fleet_status":
                continue
            return event
        if len(session_events) < limit:
            break  # the whole session was read and every event was skipped
    return None


def _running_task(store: StoreReader, errors: list[str]) -> str | None:
    try:
        tasks = list_tasks(store, status="running")  # type: ignore[arg-type]
    except Exception:
        errors.append("Pulse task telemetry unavailable")
        return None
    return tasks[0].text if tasks else None


def _latest_events_by_agent(store: StoreReader) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in read_process_events(store):
        agent_name = event.get("agent_name")
        if isinstance(agent_name, str) and agent_name not in latest:
            latest[agent_name] = event
    return latest


_PROCESS_CMDLINES_UNSET = object()


def read_fleet_snapshot(
    store: StoreReader,
    registry_prefix: str = DEFAULT_AGENT_PREFIX,
    *,
    self_agents: list[tuple[str, str, str | Path]] | None = None,
    specialist_state_dir: str | Path = DEFAULT_AGENT_STATE_DIR,
    process_cmdlines: str | object | None = _PROCESS_CMDLINES_UNSET,
    observed_at: datetime | None = None,
) -> FleetSnapshot:
    """Read one coherent snapshot of self-supervising and registered agents.

    ``self_agents`` contains ``(agent_id, role, session_db)`` rows whose
    process state is unconditionally ``running`` because their caller is the
    live process taking the snapshot.  Registered agents still use session,
    task, and OS-process telemetry independently.
    """
    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    state_dir = Path(specialist_state_dir)
    resolved_cmdlines = (
        query_running_specialist_cmdlines() if process_cmdlines is _PROCESS_CMDLINES_UNSET else process_cmdlines
    )
    try:
        process_events = _latest_events_by_agent(store)
    except Exception:
        process_events = {}

    agents: list[AgentSnapshot] = []
    for agent_id, role, session_db in self_agents or []:
        errors: list[str] = []
        task = _running_task(store, errors)
        activity = _activity_for(session_db, errors, skip_fleet_status=True)
        # `errors` is populated ONLY on a genuine read failure (a task
        # store or session db that can't be read), never on a merely
        # empty result (no running task is a normal, error-free state)
        # -- so any entry at all means some telemetry is missing, not
        # just session telemetry specifically. Checking only for the
        # "session telemetry missing" substring let a task-lookup
        # failure alone (session read still succeeding) fall through to
        # "idle" instead of "telemetry_missing", silently misreporting
        # a state this snapshot genuinely doesn't know. Found by Codex
        # review before this ever shipped.
        state: Literal["working", "idle", "telemetry_missing"] = (
            "telemetry_missing" if errors else ("working" if task else "idle")
        )
        agents.append(
            AgentSnapshot(
                agent_id=agent_id,
                role=role,
                declared_status="self-supervising",
                process_state="running",
                operational_state=state,
                current_task=task,
                last_activity=activity,
                latest_process_event=process_events.get(agent_id),
                telemetry_errors=errors,
            )
        )

    records = [
        AgentRecord.model_validate(raw) for _key, raw in store.items(prefix=registry_prefix) if isinstance(raw, dict)
    ]
    records.sort(key=lambda record: record.created_at)
    for record in records:
        errors = []
        store_path = _agent_store_path(state_dir, record.name)
        task = _running_task(ReadOnlySQLiteStore(store_path), errors)
        activity = _activity_for(_agent_session_path(state_dir, record.name), errors)
        if resolved_cmdlines is None:
            process_state: Literal["running", "not_running", "unknown"] = "unknown"
        elif _cmdline_mentions(store_path, resolved_cmdlines):
            process_state = "running"
        else:
            process_state = "not_running"
        operational_state: Literal["working", "idle", "down", "stopped", "telemetry_missing"]
        # Same reasoning as the self-agent branch above: any telemetry
        # error at all (task or session) means genuinely missing
        # visibility, not just a session-specific one.
        if errors:
            operational_state = "telemetry_missing"
        elif record.status == "stopped":
            operational_state = "stopped"
        elif process_state == "running" and task:
            operational_state = "working"
        elif process_state == "running":
            operational_state = "idle"
        else:
            operational_state = "down"
        agents.append(
            AgentSnapshot(
                agent_id=record.name,
                role="agent",
                declared_status=record.status,
                process_state=process_state,
                operational_state=operational_state,
                current_task=task,
                last_activity=activity,
                latest_process_event=process_events.get(record.name),
                telemetry_errors=errors,
            )
        )
    return FleetSnapshot(observed_at=observed, agents=agents)


__all__ = [
    "DEFAULT_AGENT_STATE_DIR",
    "PROCESS_QUERY_CACHE_TTL_SECONDS",
    "ActivitySnapshot",
    "AgentSnapshot",
    "FleetSnapshot",
    "ReadOnlySQLiteStore",
    "StoreReader",
    "clear_process_query_cache",
    "query_running_specialist_cmdlines",
    "read_fleet_snapshot",
    "read_session_events",
]
