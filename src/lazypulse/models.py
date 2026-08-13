"""Core data types for LazyPulse.

Everything here is a plain pydantic model or ``StrEnum`` with no behaviour
beyond validation, so the types round-trip cleanly through
:class:`lazybridge.Store` (which serialises via ``model_dump(mode="json")``).
The authorization *logic* that consumes these lives in :mod:`lazypulse.policy`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TrustLevel(StrEnum):
    """How much the system trusts the sender of an inbound message.

    Ordered loosely from least to most trusted. The policy maps each level
    to the set of actions it may perform (see ``DEFAULT_ACTION_RULES``).
    """

    UNKNOWN = "unknown"
    EXTERNAL_VERIFIED = "external_verified"
    OWNER_CLAIM_UNVERIFIED = "owner_claim_unverified"
    OWNER_VERIFIED_EMAIL = "owner_verified_email"
    #: Alias of :attr:`OWNER_VERIFIED_EMAIL`. The canonical name carries
    #: "EMAIL" for historical reasons (the Gmail origin); the tier itself is
    #: channel-agnostic — e.g. ``TelegramPolicy`` grants it to ``owner_ids``.
    #: Prefer this alias in non-email policies. Serialises identically.
    OWNER_VERIFIED = "owner_verified_email"
    APPROVED_SESSION = "approved_session"
    SYSTEM = "system"


class ActionClass(StrEnum):
    """The kind of action an inbound message is asking the agent to take."""

    READ_PUBLIC = "read_public"
    READ_PRIVATE = "read_private"
    WRITE_LOCAL = "write_local"
    EXTERNAL_SEND = "external_send"
    DESTRUCTIVE = "destructive"
    CODE_OR_COMPUTER = "code_or_computer"


class PolicyDecision(StrEnum):
    """The outcome of evaluating an ``(identity, action)`` pair."""

    ALLOW = "allow"
    QUEUE_FOR_REVIEW = "queue_for_review"
    REJECT = "reject"
    REQUIRE_OWNER_CONFIRMATION = "require_owner_confirmation"


class Identity(BaseModel):
    """Resolved sender identity attached to an inbound message.

    Produced by :meth:`lazypulse.PulsePolicy.classify`. ``auth_signals``
    carries adapter-specific evidence (DKIM/SPF/DMARC for Gmail, HMAC
    verification for webhooks) so a reviewer can audit *why* a trust level
    was assigned.
    """

    sender: str | None = None
    trust: TrustLevel = TrustLevel.UNKNOWN
    auth_signals: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class InboundMessage(BaseModel):
    """A message produced by an :class:`~lazypulse.Adapter`.

    ``message_id`` is the idempotency key: the same id is never turned into
    two tasks, even across adapter re-drains or multiple PulseAgents sharing
    a Store.
    """

    source: str
    message_id: str
    received_at: datetime
    sender_raw: str | None = None
    text: str
    requested_action: ActionClass = ActionClass.READ_PUBLIC
    metadata: dict[str, Any] = Field(default_factory=dict)


PulseStatus = Literal[
    "scheduled",
    "running",
    "awaiting_review",
    "completed",
    "rejected",
    "failed",
]


class PulseRecord(BaseModel):
    """The single source of truth for one task's lifecycle.

    Lives in the Store under ``store_keys.TASK``. The PulseAgent advances
    ``status`` through the lifecycle via compare-and-swap so concurrent
    ticks (and multiple PulseAgents on one Store) never double-run a task.
    """

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    text: str
    status: PulseStatus = "scheduled"
    created_at: datetime
    run_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    source_event_id: str | None = None
    # Originating adapter name + its inbound metadata, carried so a completed
    # task can be routed back to where it came from (see ``Responder``). A
    # programmatically scheduled task has no source.
    source: str | None = None
    inbound_metadata: dict[str, Any] = Field(default_factory=dict)
    identity: Identity | None = None
    action_class: ActionClass = ActionClass.READ_PUBLIC
    decision: PolicyDecision | None = None
    # Crash-recovery bookkeeping: incremented each time a stale ``running``
    # record is reset back to ``scheduled``. Capped by the agent.
    restart_count: int = 0
    # Populated when the worker finishes.
    worker_text: str | None = None
    cost_usd: float = 0.0
    error: str | None = None
    # v0.2 fields — all default so v0.1 JSON deserialises cleanly.
    attempt: int = 0
    next_retry_at: datetime | None = None
    rate_limited: bool = False
    error_type: str | None = None


class TickReport(BaseModel):
    """Summary of what a single :meth:`PulseAgent.tick_once` did.

    Returned for deterministic testing and emitted on the Session as the
    ``pulse.tick`` event.
    """

    at: datetime
    drained: int = 0
    duplicates: int = 0
    #: Recurring schedules that fired this tick, and occurrences deliberately
    #: passed over (misfire grace exceeded, non-business day, overlap).
    fired: int = 0
    missed: int = 0
    scheduled: int = 0
    queued_for_review: int = 0
    rejected: int = 0
    recovered: int = 0
    pruned: int = 0
    due: int = 0
    completed: int = 0
    failed: int = 0
