"""Always-on fleet supervision, durable registry, events, and telemetry.

Promoted from LazyCEO after live use because process supervision, durable
process-exit recording, agent-definition bookkeeping, and fleet inspection
are useful to any group of LazyPulse agents.  Framework-specific prompts,
tools, privileged roles, and approval storage remain caller policy.

One minimal wiring looks like this::

    import argparse
    from lazybridge import Store
    from lazypulse.fleet import AgentRegistry, read_fleet_snapshot, run_forever

    store = Store(db="fleet.sqlite")
    registry = AgentRegistry(store)
    registry.register(name="research", function="research assistant",
                      tick_cron="0 * * * *")
    snapshot = read_fleet_snapshot(store, process_cmdlines=None)

    args = argparse.Namespace(
        workspace_root=".", store_db="research.sqlite",
        agent_script="research_agent.py", tick_cron="0 * * * *", tz="UTC",
        stall_after_seconds=1800.0, stall_check_seconds=60.0,
        process_event_store_db="fleet.sqlite", supervised_name="research",
    )
    run_forever(args)  # blocks until Ctrl+C
"""

from lazypulse.fleet.events import (
    DEFAULT_PLANNED_STOP_PREFIX,
    DEFAULT_PLANNED_STOP_TTL_SECONDS,
    DEFAULT_PROCESS_EVENT_PREFIX,
    planned_stop_covering,
    read_process_events,
    write_planned_stop_marker,
)
from lazypulse.fleet.registry import DEFAULT_AGENT_PREFIX, AgentRecord, AgentRegistry
from lazypulse.fleet.supervisor import (
    DEFAULT_STALL_AFTER_SECONDS,
    DEFAULT_STALL_CHECK_SECONDS,
    FAST_FAILURE_SECONDS,
    MAX_BACKOFF_SECONDS,
    MIN_BACKOFF_SECONDS,
    is_stalled,
    main,
    next_backoff,
    reclaim_stalled_tasks,
    run_forever,
)
from lazypulse.fleet.telemetry import (
    DEFAULT_AGENT_STATE_DIR,
    PROCESS_QUERY_CACHE_TTL_SECONDS,
    ActivitySnapshot,
    AgentSnapshot,
    FleetSnapshot,
    ReadOnlySQLiteStore,
    StoreReader,
    clear_process_query_cache,
    query_running_specialist_cmdlines,
    read_fleet_snapshot,
    read_session_events,
)

__all__ = [
    # Registry
    "DEFAULT_AGENT_PREFIX",
    "AgentRecord",
    "AgentRegistry",
    # Events
    "DEFAULT_PLANNED_STOP_PREFIX",
    "DEFAULT_PLANNED_STOP_TTL_SECONDS",
    "DEFAULT_PROCESS_EVENT_PREFIX",
    "planned_stop_covering",
    "read_process_events",
    "write_planned_stop_marker",
    # Supervisor
    "DEFAULT_STALL_AFTER_SECONDS",
    "DEFAULT_STALL_CHECK_SECONDS",
    "FAST_FAILURE_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "MIN_BACKOFF_SECONDS",
    "is_stalled",
    "main",
    "next_backoff",
    "reclaim_stalled_tasks",
    "run_forever",
    # Telemetry
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
