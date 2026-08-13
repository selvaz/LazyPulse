"""The packaged always-on launcher: ``lazypulse serve``.

Runs a Telegram-facing PulseAgent configured entirely from environment
variables, so a deployment changes behaviour from a control panel rather than
from a code edit. Requires the ``telegram`` extra (and ``cron`` if you point it
at a calendar).

    export BOT_TOKEN=... OWNER_ID=... DEEPSEEK_API_KEY=...
    lazypulse serve

What it wires up: the Telegram inbox and its owner-only trust policy, the
human-in-the-loop reviewer (``/approve`` · ``/reject`` in the chat), crash
recovery, Store retention, and — when ``CALENDAR_FILE`` is set — a
:class:`~lazypulse.Calendar` of recurring work, optionally with
:class:`~lazypulse.CalendarTools` so the agent can manage its own timetable.

**Environment**

===========================  =======================================================
``BOT_TOKEN``                *required* — token from @BotFather
``OWNER_ID``                 *required* — your Telegram user id
``MODEL``                    LazyBridge model id (default ``deepseek-v4-flash``);
                             its provider key must be set (e.g. ``DEEPSEEK_API_KEY``)
``SYSTEM_PROMPT``            system instructions for the assistant
``STORE_DB``                 Store path (default ``pulse.db``; put it on a mounted
                             volume for a container)
``BOT_ID``                   Telegram watermark identity (default ``lazypulse``).
                             Changing it resets the update offset — an existing
                             deployment must keep the value it already uses.
``AGENT_NAME``               agent + adapter name (default ``lazypulse``)
``TICK_SECONDS``             loop interval (default 3)
``MAX_CONCURRENT``           cap on tasks running at once (default 4)
``REPLY_MIN_INTERVAL``       per-chat auto-reply throttle (default 2)
``RETENTION_SECONDS``        prune terminal records older than this (default 7d)
``STALE_AFTER``              recover ``running`` records older than this (default 600)
``REVIEW_KEYWORDS``          comma-separated words that send a message to review;
                             empty string disables the heuristic
``OWNER_CHAT_ID``            chat for unprompted messages (default: ``OWNER_ID``)
``NOTIFY_TOOL``              ``0`` to withhold the ``notify_owner`` tool. It is on
                             by default because scheduled work has no conversation
                             to reply into, so without it a calendar delivers nothing
``CALENDAR_FILE``            TOML calendar to run (see :meth:`Calendar.from_toml`)
``CALENDAR_TOOLS``           ``1`` to let the agent manage its own schedules
``CALENDAR_MIN_INTERVAL``    floor on agent-created cadences, seconds (default 300)
``CALENDAR_MAX_SCHEDULES``   cap on agent-created schedules (default 20)
===========================  =======================================================

To extend it — extra tools, a different engine — import :func:`serve` and pass
them in rather than copying this file; see ``deploy/tg-bot/bot.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lazypulse.pulse_agent import PulseAgent

#: Words that mark an inbound message as risky, re-labelling it EXTERNAL_SEND so
#: a verified owner still has to confirm it (→ ``awaiting_review`` → HITL ping).
#: A keyword MVP: it re-labels *intent*, it does not confine the worker's tools.
DEFAULT_REVIEW_KEYWORDS = (
    "invia,inviare,manda,mandare,spedisci,email,mail,cancella,elimina,rimuovi,"
    "paga,pagamento,bonifico,trasferisci,send,delete,remove,pay,transfer,wire"
)

DEFAULT_SYSTEM_PROMPT = (
    "Sei un assistente personale conciso. Rispondi direttamente all'utente. Quando serve, usa i tool a disposizione."
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _env_flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0") == "1"


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class LauncherConfig:
    """Everything :func:`serve` reads from the environment, resolved once."""

    bot_token: str
    owner_id: int
    model: str = "deepseek-v4-flash"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    store_db: str = "pulse.db"
    bot_id: str = "lazypulse"
    agent_name: str = "lazypulse"
    tick_seconds: float = 3.0
    max_concurrent: int = 4
    reply_min_interval: float = 2.0
    retention_seconds: float = 7 * 24 * 3600
    stale_after: float = 600.0
    review_keywords: tuple[str, ...] = ()
    owner_chat_id: int | None = None
    notify_tool: bool = True
    calendar_file: str | None = None
    calendar_tools: bool = False
    calendar_min_interval: float = 300.0
    calendar_max_schedules: int = 20

    @classmethod
    def from_env(cls) -> LauncherConfig:
        owner_raw = _require("OWNER_ID")
        try:
            owner_id = int(owner_raw)
        except ValueError:
            raise SystemExit(f"OWNER_ID must be a Telegram numeric user id, got {owner_raw!r}") from None
        raw_keywords = os.environ.get("REVIEW_KEYWORDS", DEFAULT_REVIEW_KEYWORDS)
        return cls(
            bot_token=_require("BOT_TOKEN"),
            owner_id=owner_id,
            model=os.environ.get("MODEL", "deepseek-v4-flash"),
            system_prompt=os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            store_db=os.environ.get("STORE_DB", "pulse.db"),
            bot_id=os.environ.get("BOT_ID", "lazypulse"),
            agent_name=os.environ.get("AGENT_NAME", "lazypulse"),
            tick_seconds=_env_float("TICK_SECONDS", 3.0),
            max_concurrent=_env_int("MAX_CONCURRENT", 4),
            reply_min_interval=_env_float("REPLY_MIN_INTERVAL", 2.0),
            retention_seconds=_env_float("RETENTION_SECONDS", 7 * 24 * 3600),
            stale_after=_env_float("STALE_AFTER", 600.0),
            review_keywords=tuple(w.strip().lower() for w in raw_keywords.split(",") if w.strip()),
            owner_chat_id=_env_int("OWNER_CHAT_ID", owner_id),
            notify_tool=_env_flag("NOTIFY_TOOL", default=True),
            calendar_file=os.environ.get("CALENDAR_FILE") or None,
            calendar_tools=_env_flag("CALENDAR_TOOLS"),
            calendar_min_interval=_env_float("CALENDAR_MIN_INTERVAL", 300.0),
            calendar_max_schedules=_env_int("CALENDAR_MAX_SCHEDULES", 20),
        )


def build_action_classifier(keywords: Sequence[str]) -> Any:
    """Map messages asking for risky actions to EXTERNAL_SEND (→ review).

    ``None`` when the keyword list is empty, which turns the heuristic off."""
    from lazypulse.models import ActionClass, InboundMessage

    if not keywords:
        return None
    lowered = tuple(keywords)

    def classify(msg: InboundMessage) -> ActionClass:
        text = (msg.text or "").lower()
        return ActionClass.EXTERNAL_SEND if any(kw in text for kw in lowered) else ActionClass.READ_PUBLIC

    return classify


def _build_notify_tool(client: Any, chat_id: int) -> Any:
    """A tool the agent can call to message the owner unprompted.

    Scheduled work needs this. The adapter's reply path only answers an
    *inbound* message — it keys on the task's originating chat — and a task
    born from the calendar has no origin, so without a tool like this a
    scheduled digest would run, produce its text, and deliver it nowhere.
    """
    from lazybridge import Tool

    def notify_owner(text: str) -> str:
        """Send a message to the owner on Telegram.

        Use this to deliver the result of scheduled work, which has no
        conversation to reply into.

        Args:
            text: the message to send.
        """
        client.send_message(chat_id=chat_id, text=text)
        return "sent"

    return Tool.wrap(notify_owner, name="notify_owner")


def build(
    config: LauncherConfig,
    *,
    tools: Sequence[Any] = (),
    engine: Any | None = None,
) -> tuple[PulseAgent, Any]:
    """Assemble the agent and its HITL reviewer. Returns ``(pulse, reviewer)``.

    Separated from :func:`serve` so a caller can inspect or extend the agent
    before the loop starts — and so tests can build one without running it."""
    from lazybridge import LLMEngine, Store

    from lazypulse.pulse_agent import PulseAgent

    try:
        from lazytools.connectors.telegram import TelegramClient
    except ImportError as exc:  # pragma: no cover — depends on the install
        raise SystemExit("The launcher needs the 'telegram' extra: pip install 'lazypulse[telegram]'") from exc
    from lazypulse.adapters.telegram import (
        TelegramInbox,
        TelegramInboxConfig,
        TelegramPolicy,
        TelegramReviewer,
    )

    calendar = None
    if config.calendar_file:
        from lazypulse.schedules import Calendar

        calendar = Calendar.from_toml(config.calendar_file)

    client = TelegramClient.from_token(config.bot_token)
    store = Store(db=config.store_db)
    reviewer = TelegramReviewer(client, store, owner_id=config.owner_id, bot_id=config.bot_id)

    all_tools = list(tools)
    if config.notify_tool:
        all_tools.append(_build_notify_tool(client, config.owner_chat_id or config.owner_id))
    if config.calendar_tools:
        from lazypulse.calendar_tools import CalendarTools

        all_tools += CalendarTools(
            store,
            min_interval_seconds=config.calendar_min_interval,
            max_agent_schedules=config.calendar_max_schedules,
        ).tools()

    pulse = PulseAgent(
        name=config.agent_name,
        engine=engine if engine is not None else LLMEngine(config.model, system=config.system_prompt),
        store=store,
        policy=TelegramPolicy(owner_ids=[config.owner_id]),
        adapters=[
            TelegramInbox(
                client,
                TelegramInboxConfig(
                    bot_id=config.bot_id,
                    reply_with_output=True,
                    reply_min_interval_seconds=config.reply_min_interval,
                ),
                name=config.agent_name,
            )
        ],
        calendar=calendar,
        tools=all_tools,
        tick_seconds=config.tick_seconds,
        max_concurrent_inbound=config.max_concurrent,
        terminal_retention=config.retention_seconds,
        stale_after=config.stale_after,
        command_filter=reviewer.handle_command,
        action_classifier=build_action_classifier(config.review_keywords),
    )
    return pulse, reviewer


def serve(
    *,
    config: LauncherConfig | None = None,
    tools: Sequence[Any] = (),
    engine: Any | None = None,
) -> None:
    """Build the agent and run it until interrupted.

    The tick loop runs in its own thread; this coroutine loop only announces
    tasks parked for approval to the owner. They share nothing but the Store."""
    config = config or LauncherConfig.from_env()
    pulse, reviewer = build(config, tools=tools, engine=engine)

    schedules = pulse.list_schedules()
    print(
        f"[{config.agent_name}] start | model={config.model} db={config.store_db} "
        f"tick={config.tick_seconds}s owner={config.owner_id} "
        f"tools={len(tools)}+{'notify' if config.notify_tool else '-'}"
        f"/{'calendar' if config.calendar_tools else '-'} "
        f"schedules={len(schedules)} hitl={'on' if config.review_keywords else 'off'}",
        flush=True,
    )
    for record in schedules:
        print(f"[{config.agent_name}]   schedule {record.name} -> {record.next_fire_at or 'event-driven'}", flush=True)

    async def _notifier() -> None:
        while pulse.is_running():
            try:
                await reviewer.notify_pending()
            except Exception as exc:  # best-effort: a notifier failure must not end the bot
                print(f"[{config.agent_name}] notifier error: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(config.tick_seconds)

    with pulse.running():
        try:
            asyncio.run(_notifier())
        except KeyboardInterrupt:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """``lazypulse`` console entry point."""
    parser = argparse.ArgumentParser(prog="lazypulse", description="Run an always-on LazyPulse agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_cmd = sub.add_parser("serve", help="run the Telegram agent until interrupted")
    serve_cmd.add_argument(
        "--calendar",
        metavar="FILE",
        help="TOML calendar of recurring work (overrides CALENDAR_FILE)",
    )
    check = sub.add_parser("check-calendar", help="validate a TOML calendar and print what it declares")
    check.add_argument("file", help="path to the TOML calendar")

    args = parser.parse_args(argv)

    if args.command == "check-calendar":
        from lazypulse.schedules import Calendar

        try:
            calendar = Calendar.from_toml(args.file)
        except (OSError, ValueError) as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 1
        print(f"{args.file}: {len(calendar)} schedule(s)")
        for entry in calendar:
            when = getattr(entry, "expr", None)
            when = f"cron {when} ({entry.tz})" if when else f"after {entry.after}"  # type: ignore[union-attr]
            print(f"  {entry.name:<24} {when:<34} action={entry.action.value}")
        return 0

    if args.calendar:
        os.environ["CALENDAR_FILE"] = args.calendar
    serve()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
