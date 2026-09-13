"""Regression tests for UI-agnostic fleet telemetry readers."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lazybridge import Store

import lazypulse.fleet.telemetry as telemetry_module
from lazypulse.fleet.events import DEFAULT_PROCESS_EVENT_PREFIX
from lazypulse.fleet.registry import AgentRecord, AgentRegistry
from lazypulse.fleet.telemetry import ReadOnlySQLiteStore, read_fleet_snapshot, read_session_events


def _create_session_db(path: Path, *events: tuple[str, dict[str, object] | str, float]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, payload TEXT, ts REAL)"
        )
        for event_type, payload, timestamp in events:
            encoded = json.dumps(payload) if isinstance(payload, dict) else payload
            connection.execute(
                "INSERT INTO events (event_type, payload, ts) VALUES (?, ?, ?)",
                (event_type, encoded, timestamp),
            )


def _write_record(store: Store, record: AgentRecord, *, prefix: str = "fleet:agent:") -> None:
    store.write(f"{prefix}{record.name}", record.model_dump(mode="json"))


def test_read_only_sqlite_store_reads_normal_store_and_cannot_write(tmp_path: Path) -> None:
    db = tmp_path / "fleet.sqlite"
    store = Store(db=str(db))
    store.write("some:key", {"a": 1})
    store.write("other:key", {"b": 2})
    reader = ReadOnlySQLiteStore(db)

    assert reader.read("some:key") == {"a": 1}
    assert reader.read("missing", default="fallback") == "fallback"
    assert dict(reader.items(prefix="some:")) == {"some:key": {"a": 1}}

    with reader._connect() as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO store (key, value) VALUES ('x', '1')")


def test_read_session_events_is_newest_first_extracts_tools_and_tolerates_payload(tmp_path: Path) -> None:
    session_db = tmp_path / "session.sqlite"
    _create_session_db(
        session_db,
        ("message", {"text": "older"}, 1.0),
        ("tool_call", {"tool_name": "search"}, 2.0),
        ("broken", "not-json", 3.0),
    )

    events = read_session_events(session_db, limit=3)

    assert [event.event_type for event in events] == ["broken", "tool_call", "message"]
    assert events[0].tool_name is None
    assert events[1].tool_name == "search"
    with pytest.raises(ValueError, match="limit must be positive"):
        read_session_events(session_db, limit=0)


def test_query_running_specialist_cmdlines_treats_nonzero_empty_exit_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed PowerShell/CIM query cannot prove that zero agents run."""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="access denied")

    monkeypatch.setattr(telemetry_module.subprocess, "run", fake_run)
    telemetry_module.clear_process_query_cache()

    assert telemetry_module.query_running_specialist_cmdlines() is None


def test_process_query_cache_and_explicit_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"query-{calls}", stderr="")

    monkeypatch.setattr(telemetry_module.subprocess, "run", fake_run)
    telemetry_module.clear_process_query_cache()
    assert telemetry_module.query_running_specialist_cmdlines() == "query-1"
    assert telemetry_module.query_running_specialist_cmdlines() == "query-1"
    assert calls == 1
    telemetry_module.clear_process_query_cache()
    assert telemetry_module.query_running_specialist_cmdlines() == "query-2"


def test_read_fleet_snapshot_can_report_registered_agents_only(tmp_path: Path) -> None:
    store = Store()
    AgentRegistry(store).register(name="research", function="research assistant", tick_cron="0 * * * *")

    snapshot = read_fleet_snapshot(
        store,
        self_agents=None,
        specialist_state_dir=tmp_path / "agents",
        process_cmdlines=None,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert snapshot.observed_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert [agent.agent_id for agent in snapshot.agents] == ["research"]
    assert snapshot.agents[0].role == "agent"
    assert snapshot.agents[0].process_state == "unknown"
    assert snapshot.agents[0].operational_state == "telemetry_missing"


def test_self_agent_is_unconditionally_running_but_registered_agent_uses_live_state(tmp_path: Path) -> None:
    fleet_store = Store()
    AgentRegistry(fleet_store).register(name="research", function="research assistant", tick_cron="0 * * * *")
    state_dir = tmp_path / "agents"
    state_dir.mkdir()
    research_store = state_dir / "research.sqlite"
    Store(db=str(research_store)).write("sentinel", True)
    _create_session_db(state_dir / "research.session.sqlite", ("message", {"text": "ready"}, 1.0))
    primary_session = tmp_path / "primary.session.sqlite"
    _create_session_db(
        primary_session,
        ("message", {"text": "ready"}, 1.0),
        ("tool_call", {"tool_name": "fleet_status"}, 2.0),
    )

    snapshot = read_fleet_snapshot(
        fleet_store,
        self_agents=[("primary", "coordinator", primary_session)],
        specialist_state_dir=state_dir,
        process_cmdlines=str(research_store),
    )

    assert [agent.agent_id for agent in snapshot.agents] == ["primary", "research"]
    primary, research = snapshot.agents
    assert primary.role == "coordinator"
    assert primary.declared_status == "self-supervising"
    assert primary.process_state == "running"
    assert primary.operational_state == "idle"
    assert primary.last_activity.event_type == "message"
    assert research.process_state == "running"
    assert research.operational_state == "idle"


def test_telemetry_missing_takes_priority_over_stopped_status(tmp_path: Path) -> None:
    store = Store()
    record = AgentRecord(
        name="archived",
        function="historical assistant",
        tick_cron="0 * * * *",
        status="stopped",
        created_at=datetime.now(UTC),
        stopped_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    _write_record(store, record)

    [archived] = read_fleet_snapshot(
        store,
        specialist_state_dir=tmp_path / "missing",
        process_cmdlines="",
    ).agents

    assert archived.declared_status == "stopped"
    assert archived.process_state == "not_running"
    assert archived.operational_state == "telemetry_missing"
    assert any("session telemetry missing" in error for error in archived.telemetry_errors)


@pytest.mark.skipif(
    os.name != "nt",
    reason="normcase only unifies case and slash style on Windows -- on POSIX '/Foo' and '/foo' "
    "are genuinely different files, so refusing to match them there is the correct behaviour, "
    "not a bug to assert against",
)
def test_process_match_normalizes_separators_and_case_before_comparing(tmp_path: Path) -> None:
    """A Path-built store path always carries native separators and whatever
    case its state dir is spelled in, while a real command line carries
    whatever the launcher actually typed. A plain substring check reports a
    false "not_running" for a live process pointing at the very same file --
    found live in the project this was promoted from, where any specialist
    launched by hand read as a permanent MISMATCH."""
    store = Store()
    AgentRegistry(store).register(name="research", function="research assistant", tick_cron="0 * * * *")
    state_dir = tmp_path / "agents"
    state_dir.mkdir()
    research_store = state_dir / "research.sqlite"
    Store(db=str(research_store)).write("sentinel", True)
    _create_session_db(state_dir / "research.session.sqlite", ("message", {"text": "ready"}, 1.0))

    # Same file, spelled with forward slashes and different case -- exactly
    # what a hand-launched process's command line looks like on Windows.
    cmdline = f"python -m agent --store-db {str(research_store).replace(chr(92), '/').upper()}"

    [agent] = read_fleet_snapshot(
        store,
        specialist_state_dir=state_dir,
        process_cmdlines=cmdline,
    ).agents

    assert agent.process_state == "running"


def test_task_lookup_failure_alone_reports_telemetry_missing_not_idle(tmp_path: Path) -> None:
    """A task-store read failure alone -- with the SESSION db still
    readable -- must also report telemetry_missing, not silently fall
    through to "idle". Checking only for a session-specific error
    message let a task-only failure hide behind a false "idle". Found by
    Codex review before this ever shipped."""
    store = Store()
    AgentRegistry(store).register(name="research", function="research assistant", tick_cron="0 * * * *")
    state_dir = tmp_path / "agents"
    state_dir.mkdir()
    # Deliberately do NOT create research.sqlite -- the task-store read
    # fails -- but DO create a valid, readable session db.
    _create_session_db(state_dir / "research.session.sqlite", ("message", {"text": "ready"}, 1.0))

    [agent] = read_fleet_snapshot(
        store,
        specialist_state_dir=state_dir,
        process_cmdlines=str(state_dir / "research.sqlite"),
    ).agents

    assert agent.operational_state == "telemetry_missing"
    assert any("Pulse task telemetry unavailable" in error for error in agent.telemetry_errors)
    assert not any("session telemetry missing" in error for error in agent.telemetry_errors)


def test_self_agent_activity_survives_more_than_three_repeated_fleet_status_polls(tmp_path: Path) -> None:
    """A fixed limit=3 window, combined with skipping fleet_status calls,
    could hide a real prior event entirely if the newest 3 events all
    happen to be repeated fleet_status polls -- a real risk for a
    self-supervising agent inspected often. Found by Codex review before
    this ever shipped."""
    session_db = tmp_path / "primary.session.sqlite"
    # 40 consecutive status polls: past the old fixed 3-event window AND
    # past the fixed 20 that replaced it, so only a widening search finds
    # the one real event underneath them.
    _create_session_db(
        session_db,
        ("message", {"text": "real activity"}, 1.0),
        *[("tool_call", {"tool_name": "fleet_status"}, float(i)) for i in range(2, 42)],
    )
    store = Store()

    snapshot = read_fleet_snapshot(
        store,
        self_agents=[("primary", "coordinator", session_db)],
        specialist_state_dir=tmp_path / "agents",
        process_cmdlines=None,
    )

    [primary] = snapshot.agents
    assert primary.last_activity is not None
    assert primary.last_activity.event_type == "message"


def test_a_custom_process_event_prefix_is_honoured_not_silently_empty(tmp_path: Path) -> None:
    """Moving the registry namespace almost always means the event namespace
    moved too, and reading the wrong one fails silently: latest_process_event
    is None forever, which looks exactly like an agent that has never crashed.
    Found in LazyCEO, whose events live under "ceo:process-event:"."""
    store = Store()
    record = AgentRecord(
        name="legacy",
        function="migrated assistant",
        tick_cron="0 * * * *",
        status="active",
        created_at=datetime.now(UTC),
    )
    _write_record(store, record, prefix="ceo:specialist:")
    store.write(
        "ceo:process-event:crash",
        {"agent_name": "legacy", "created_at": "2026-01-02T00:00:00+00:00", "exit_code": 3},
    )

    def snapshot(**kwargs):
        [agent] = read_fleet_snapshot(
            store,
            registry_prefix="ceo:specialist:",
            specialist_state_dir=tmp_path / "missing",
            process_cmdlines=None,
            **kwargs,
        ).agents
        return agent

    assert snapshot(process_event_prefix="ceo:process-event:").latest_process_event["exit_code"] == 3
    # The default prefix finds nothing here -- that is the silent failure.
    assert snapshot().latest_process_event is None


def test_registry_prefix_and_latest_process_event_are_correlated(tmp_path: Path) -> None:
    store = Store()
    record = AgentRecord(
        name="legacy",
        function="migrated assistant",
        tick_cron="0 * * * *",
        status="active",
        created_at=datetime.now(UTC),
    )
    _write_record(store, record, prefix="ceo:specialist:")
    store.write(
        f"{DEFAULT_PROCESS_EVENT_PREFIX}old",
        {"agent_name": "legacy", "created_at": "2026-01-01T00:00:00+00:00", "exit_code": 1},
    )
    store.write(
        f"{DEFAULT_PROCESS_EVENT_PREFIX}new",
        {"agent_name": "legacy", "created_at": "2026-01-02T00:00:00+00:00", "exit_code": 2},
    )

    [agent] = read_fleet_snapshot(
        store,
        registry_prefix="ceo:specialist:",
        specialist_state_dir=tmp_path / "missing",
        process_cmdlines=None,
    ).agents

    assert agent.agent_id == "legacy"
    assert agent.latest_process_event["exit_code"] == 2
