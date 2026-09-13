from __future__ import annotations

from datetime import UTC, datetime

import pytest
from lazybridge import Store

from lazypulse.fleet.registry import DEFAULT_AGENT_PREFIX, AgentRecord, AgentRegistry


def test_register_defaults_round_trip_and_custom_prefix() -> None:
    store = Store()
    registry = AgentRegistry(store, prefix="legacy:")

    record = registry.register(name="research", function="research assistant", tick_cron="0 * * * *")

    assert record.status == "active"
    assert record.stopped_at is None
    assert record.config == {}
    assert registry.get("research") == record
    assert store.read("legacy:research") == record.model_dump(mode="json")
    assert store.items(prefix=DEFAULT_AGENT_PREFIX) == []


def test_register_preserves_opaque_config() -> None:
    registry = AgentRegistry(Store())
    config = {"system_prompt": "Investigate carefully", "tools": ["search"], "nested": {"limit": 3}}

    record = registry.register(name="research", function="research assistant", tick_cron="0 * * * *", config=config)

    assert record.config == config
    assert registry.get("research") == record


def test_get_unknown_returns_none_and_list_is_oldest_first() -> None:
    store = Store()
    registry = AgentRegistry(store)
    newer = AgentRecord(
        name="alphabetically-first",
        function="newer",
        tick_cron="0 * * * *",
        status="active",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    older = newer.model_copy(
        update={"name": "alphabetically-last", "function": "older", "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
    )
    store.write(registry._key(newer.name), newer.model_dump(mode="json"))
    store.write(registry._key(older.name), older.model_dump(mode="json"))

    assert registry.get("missing") is None
    assert [record.name for record in registry.list()] == ["alphabetically-last", "alphabetically-first"]


def test_mark_stopped_is_idempotent_and_keeps_record_listed() -> None:
    registry = AgentRegistry(Store())
    registry.register(name="research", function="research assistant", tick_cron="0 * * * *")

    assert registry.mark_stopped("research") is True
    first_stopped_at = registry.get("research").stopped_at
    assert first_stopped_at is not None
    assert registry.mark_stopped("research") is True
    assert registry.get("research").status == "stopped"
    assert registry.get("research").stopped_at >= first_stopped_at
    assert [record.name for record in registry.list()] == ["research"]
    assert registry.mark_stopped("missing") is False


def test_update_changes_only_the_given_fields_and_leaves_lifecycle_alone() -> None:
    """None means "leave as is", never "clear it", and editing config is not
    itself a lifecycle event -- status/created_at/stopped_at must survive an
    update untouched."""
    registry = AgentRegistry(Store())
    original = registry.register(
        name="research", function="research assistant", tick_cron="0 * * * *", config={"prompt": "old"}
    )
    registry.mark_stopped("research")

    updated = registry.update("research", config={"prompt": "new"})

    assert updated is not None
    assert updated.config == {"prompt": "new"}
    assert updated.function == "research assistant"  # untouched
    assert updated.tick_cron == "0 * * * *"  # untouched
    assert updated.status == "stopped"  # lifecycle deliberately left alone
    assert updated.created_at == original.created_at
    assert registry.get("research").config == {"prompt": "new"}


def test_update_with_nothing_to_change_returns_the_record_unchanged() -> None:
    """A pure bounce with no config change is a valid, meaningful call."""
    registry = AgentRegistry(Store())
    registry.register(name="research", function="research assistant", tick_cron="0 * * * *")

    assert registry.update("research") == registry.get("research")
    assert registry.update("missing") is None


def test_update_returns_none_when_a_concurrent_write_wins_the_race() -> None:
    """CAS, not a bare write: a lost race must surface to the caller rather
    than silently discarding one of two field changes landing together."""
    store = Store()
    registry = AgentRegistry(store)
    registry.register(name="research", function="research assistant", tick_cron="0 * * * *")
    real_read = store.read

    def read_then_race(key: str, default: object = None) -> object:
        raw = real_read(key, default)
        registry_direct = AgentRegistry(store)
        store.read = real_read  # type: ignore[method-assign]  # race exactly once
        registry_direct.update("research", function="won the race")
        return raw

    store.read = read_then_race  # type: ignore[method-assign]

    assert registry.update("research", function="lost the race") is None
    assert registry.get("research").function == "won the race"


def test_mark_active_reverses_mark_stopped_after_a_confirmed_respawn() -> None:
    """Without this, restarting a stopped agent leaves the registry declaring
    "stopped" for a process that is genuinely running -- a permanent false
    mismatch fleet telemetry would keep reporting."""
    registry = AgentRegistry(Store())
    registry.register(name="research", function="research assistant", tick_cron="0 * * * *")
    registry.mark_stopped("research")
    assert registry.get("research").status == "stopped"

    assert registry.mark_active("research") is True
    restored = registry.get("research")
    assert restored.status == "active"
    assert restored.stopped_at is None
    assert registry.mark_active("missing") is False


def test_register_and_launch_does_not_orphan_record_when_spawn_raises() -> None:
    """Registration must happen only after process creation succeeds.

    Reverting ``register_and_launch`` to register before ``spawn`` makes this
    test fail because ``fleet:agent:research`` remains active after the exact
    launch failure that prevented any process from existing. Found by Codex
    review during the LazyCEO extraction.
    """
    registry = AgentRegistry(Store())
    failure = RuntimeError("process creation failed")

    def spawn():
        raise failure

    with pytest.raises(RuntimeError) as raised:
        registry.register_and_launch(
            name="research",
            function="research assistant",
            tick_cron="0 * * * *",
            config=None,
            spawn=spawn,
        )

    assert raised.value is failure
    assert registry.get("research") is None
    assert registry.list() == []


def test_register_and_launch_terminates_the_process_when_registration_fails() -> None:
    """The reverse orphan: spawn succeeds but the registry write then fails
    (Store locked, disk full, ...) -- without cleanup, the just-spawned
    process would keep running, live but completely untracked, since the
    caller's exception handler never sees a Popen handle to clean up.
    Found by Codex review before this ever shipped."""
    store = Store()

    def broken_write(key: str, value: object) -> None:
        raise RuntimeError("store write failed")

    store.write = broken_write  # type: ignore[method-assign]
    registry = AgentRegistry(store)

    class FakeProcess:
        def __init__(self) -> None:
            self.terminated = False
            self.waited = False

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            return 0

    process = FakeProcess()

    with pytest.raises(RuntimeError, match="store write failed"):
        registry.register_and_launch(
            name="research",
            function="research assistant",
            tick_cron="0 * * * *",
            config=None,
            spawn=lambda: process,  # type: ignore[arg-type,return-value]
        )

    assert process.terminated is True
    assert process.waited is True


def test_register_and_launch_kills_a_child_that_ignores_terminate() -> None:
    """terminate() alone is not a guarantee -- a child trapping the signal
    outlives it, and swallowing the TimeoutExpired would re-raise leaving
    exactly the live, untracked orphan this rollback exists to prevent.
    Found by Codex review before this ever shipped."""
    import subprocess

    store = Store()

    def broken_write(key: str, value: object) -> None:
        raise RuntimeError("store write failed")

    store.write = broken_write  # type: ignore[method-assign]
    registry = AgentRegistry(store)

    class StubbornProcess:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def terminate(self) -> None:
            self.terminated = True  # deliberately keeps "running"

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            if not self.killed:
                raise subprocess.TimeoutExpired("agent", timeout)
            return 0

    process = StubbornProcess()

    with pytest.raises(RuntimeError, match="store write failed"):
        registry.register_and_launch(
            name="research",
            function="research assistant",
            tick_cron="0 * * * *",
            config=None,
            spawn=lambda: process,  # type: ignore[arg-type,return-value]
        )

    assert process.terminated is True
    assert process.killed is True


def test_register_and_launch_returns_process_and_then_registers() -> None:
    registry = AgentRegistry(Store())
    process = object()

    returned = registry.register_and_launch(
        name="research",
        function="research assistant",
        tick_cron="0 * * * *",
        config={"model": "test"},
        spawn=lambda: process,  # type: ignore[arg-type,return-value]
    )

    assert returned is process
    assert registry.get("research").config == {"model": "test"}
