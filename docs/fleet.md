# Fleet supervision (`lazypulse.fleet`)

`lazypulse.fleet` is generic infrastructure for running and watching a group
of always-on agents — one `PulseAgent` per process, each in its own OS
process. It was promoted out of LazyCEO after live use: process supervision,
durable exit recording, agent-definition bookkeeping, and fleet inspection
turned out to have nothing LazyCEO-specific about them. Framework-specific
prompts, tools, privileged roles, and approval storage stay caller policy —
this module tracks *that* an agent exists, *whether* its process is alive,
and *what* it last did, and (via the supervisor) actively launches,
restarts, and kills the processes it watches — it isn't purely passive
observation.

It ships in the same package as the tick-loop/`PulseAgent` core but is a
separate concern: `AgentRegistry` and the process-event helpers are generic
and don't require `PulseAgent` at all. The supervisor's stall detection and
`read_fleet_snapshot()`'s activity telemetry do read LazyPulse task records
(`lazypulse.tasks.list_tasks`), so those two pieces assume a Store shaped
like a Pulse task Store even though nothing in `PulseAgent` requires
`lazypulse.fleet` back. Import it explicitly:

```python
from lazypulse.fleet import AgentRegistry, read_fleet_snapshot, run_forever
```

## The four pieces

| Module | What it's for |
|---|---|
| `lazypulse.fleet.registry` | `AgentRegistry` — a Store-backed record of each agent's declared identity, schedule, and lifecycle status. |
| `lazypulse.fleet.events` | A durable, best-effort process-exit outbox plus short-lived "planned stop" markers that mark events suppressed for an operator-initiated restart. |
| `lazypulse.fleet.supervisor` | An external process supervisor: restarts a crashed or stalled agent, with backoff. Runnable as `python -m lazypulse.fleet.supervisor`. |
| `lazypulse.fleet.telemetry` | `read_fleet_snapshot()` — a read-only, UI-agnostic view correlating registry, Store task state, session activity, and OS process discovery into one `FleetSnapshot`. |

### `AgentRegistry` — durable agent identity

```python
from lazybridge import Store
from lazypulse.fleet import AgentRegistry

store = Store(db="fleet.db")
registry = AgentRegistry(store)   # default Store prefix: "fleet:agent:"
registry.register(name="research", function="research assistant", tick_cron="0 * * * *")
# register() is an unconditional write: calling it again for the same name
# replaces the existing record (created_at, status, config all reset), it
# does not error on a name that's already registered.

registry.list()                   # every agent, oldest first, including stopped ones
registry.get("research")          # -> AgentRecord | None
registry.update("research", tick_cron="*/30 * * * *")  # partial field edit, CAS-protected
registry.mark_stopped("research") # status="stopped", stopped_at=now
registry.mark_active("research")  # call only after a replacement process is confirmed started
```

`AgentRecord.config` is an opaque `dict` — prompts, tool wiring, and other
framework-specific construction details are the caller's business; the
registry only preserves them. The registry deliberately never infers live
process state from a record: durable intent (`status`) and momentary OS
liveness are different facts, and `read_fleet_snapshot()` is what correlates
them, on purpose keeping the mismatch visible rather than hiding it.

`register_and_launch(name=, function=, tick_cron=, config=, spawn=)` is the
safe combined primitive for "start a process and record it" (`config` has
no default — pass `config=None` explicitly if there's nothing to store). It
calls `spawn()` **first**, so a failed launch never creates an orphan
"active" record, and if the registry write itself then fails, it attempts
best-effort rollback: `terminate()`, waiting up to 10s, escalating to
`kill()` and waiting up to 10s more, then re-raising the original
registration error — *if* the rollback itself doesn't raise. Only the
final `TimeoutExpired` (after `kill()`) is suppressed; an exception from
`terminate()`, the first `wait()`, or `kill()` propagates instead and
masks the original error. That closes the common case, not every case — a process that ignores both
signals, or a Store write that commits and then raises, can still leave a
mismatch; it is best-effort cleanup, not a hard guarantee against orphans.

### Process events and planned stops

```python
from lazypulse.fleet import (
    write_planned_stop_marker, planned_stop_covering, read_process_events,
)

write_planned_stop_marker(store, "research", "operator redeploy")  # TTL 120s default
read_process_events(store)   # newest first; default prefix "fleet:process-event:"
```

When run with `--process-event-store-db`, the supervisor (below) best-effort
records an event for each unexpected exit, stall kill, and launch failure
(a `KeyboardInterrupt` is not recorded — that's an operator stopping the
supervisor itself, not a child dying). `planned_stop_covering` checks
whether an unexpired marker (default prefix `"fleet:planned-stop:"`) covers
a given exit timestamp; if so, the supervisor marks that event
`delivered_at`/`delivery_kind="suppressed"` at write time so an alerting
consumer doesn't have to reason about *when* to suppress it — but the
module itself sends no alerts, so suppression only works if whatever
consumer you build respects `delivered_at`/`delivery_kind`.

### The supervisor — external process-level restart

An in-process agent cannot reliably report its own death after its event
loop, SDK subprocess, or permission stream wedges, so this supervisor runs
*outside* the agent process:

```bash
python -m lazypulse.fleet.supervisor \
    --workspace-root <path> --store-db <path> --agent-script <path> \
    --process-event-store-db <path> --supervised-name research
```

Every exit — including code `0` — triggers a restart; an always-on agent is
never expected to exit on its own. Backoff doubles on a "fast failure" (exit
sooner than `FAST_FAILURE_SECONDS = 30.0`, capped at `MAX_BACKOFF_SECONDS =
300.0`) and resets to `MIN_BACKOFF_SECONDS = 5.0` after a longer run.

Exit-only supervision misses a *live-but-wedged* process (a dead subprocess
under a Python parent that is technically still running). `is_stalled()`
closes that gap by polling — every `--stall-check-seconds` (default
`DEFAULT_STALL_CHECK_SECONDS = 60.0`) — for *any* Pulse task still `running`
past `--stall-after-seconds` (default `DEFAULT_STALL_AFTER_SECONDS =
1800.0`, i.e. 30 minutes). `has_pending_approval` is a single zero-argument
callback, not a per-task check — one unrelated pending approval anywhere
suppresses stall-killing across the board, and a caller supplies it because
approvals often live in a different, shared Store than the supervised
agent's own. Both `is_stalled()` and a Store/callback read failure fail
open (treated as "not stalled"). On a stall kill,
`reclaim_stalled_tasks()` best-effort resets every task that's been running
past the threshold (not just the one that triggered the kill) back to
`scheduled`, clearing `started_at` and bumping `restart_count`; a CAS race
or read/write failure just leaves that task's record as-is rather than
retrying. This runs deliberately faster than `PulseAgent`'s own default
recovery threshold (`max(tick_seconds * 60, 3600)`, at least an hour, and
itself overridable via `stale_after`), so a hard-killed task doesn't get
stall-killed again on every restart before the agent's own threshold would
ever fire.

### `read_fleet_snapshot()` — a correlated read for a dashboard

```python
from lazypulse.fleet import read_fleet_snapshot

snapshot = read_fleet_snapshot(
    store,                              # holds the AgentRegistry records
    self_agents=[("ceo", "orchestrator", ceo_session_db)],  # optional: the caller itself
    specialist_state_dir="C:/ProgramData/lazypulse/fleet",  # per-agent {name}.sqlite / .session.sqlite
)
for agent in snapshot.agents:
    print(agent.agent_id, agent.declared_status, agent.process_state, agent.operational_state)
```

`FleetSnapshot.agents` is a list of `AgentSnapshot`, one row per entry in
`self_agents` plus one per registry record — passing a name that's in both
produces two rows, there's no dedup by agent id. Each row has
`declared_status` (from the registry for a registered agent; hardcoded to
the literal `"self-supervising"` for a `self_agents` row, since those
have no registry record at all), `process_state` (`running` /
`not_running` / `unknown` — `unknown` when no live process listing was
available to check against), and `operational_state` (`working` / `idle` /
`down` / `stopped` / `telemetry_missing`) kept as **separate** fields rather
than one flattened boolean, so a registry/process mismatch stays visible to
whatever UI reads the snapshot instead of being silently guessed away — but
note a telemetry read failure (task or session) always wins first and
reports `telemetry_missing` regardless of everything else; only once
telemetry reads cleanly does `operational_state` fall through to `stopped`
(declared status), then `working`/`idle` when `process_state` is `running`,
then `down` otherwise — so an active agent whose process listing came back
`unknown` (not `not_running`, and with telemetry read successfully) is
still reported `down`; check `process_state` directly to tell "confirmed
not running" from "couldn't check". Likewise a `stopped` record whose
Store/session files can't be read comes back `telemetry_missing`, not
`stopped` — the declared intent survives only in `declared_status`.
`self_agents` rows (the process taking the snapshot,
e.g. a CEO agent reporting on itself) are always `process_state="running"`
by construction; every other row is cross-checked against a live OS process
listing and each agent's own Store/session files under
`specialist_state_dir`. Process discovery is Windows-only today (shells out
to PowerShell, matches processes literally named `python.exe`, and infers
which agent owns a match by searching for `{specialist_state_dir}/{name}.sqlite`
in its raw command line — a `pythonw.exe` process, a renamed interpreter, or
a relative `--store-db` argument won't match), and both successful and
failed lookups are cached for `PROCESS_QUERY_CACHE_TTL_SECONDS = 7.5`s. A
process-event read failure is swallowed to an empty event map rather than
surfaced in `telemetry_errors`, so `latest_process_event=None` can mean
either "no event" or "event telemetry failed" — and reads happen
sequentially per agent, not as one atomic transaction, so this is a
correlated view assembled from several point-in-time reads, not a single
consistent snapshot.

`ReadOnlySQLiteStore` gives read-only access to another agent's
SQLite-backed Store (`?mode=ro`) without opening it for writes — it
implements just the `read()`/`items()` surface `StoreReader` needs, not the
full `lazybridge.Store` API — what `read_fleet_snapshot()` uses internally
to inspect a registered agent's own task store.

## Where this fits in `lazypulse`

`lazypulse.fleet`'s names (`AgentRegistry`, `read_fleet_snapshot`, etc.)
are not re-exported from the top-level `lazypulse` package — `lazypulse`
has no `AgentRegistry` attribute of its own — because it is optional
infrastructure for callers running more than one agent, not part of a
single `PulseAgent`'s tick loop. `from lazypulse import fleet` does work
(that's just Python importing the submodule), but you still need to reach
through it, e.g. `fleet.AgentRegistry`; import names directly from
`lazypulse.fleet` instead, as shown above.
