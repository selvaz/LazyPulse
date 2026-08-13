"""The packaged launcher: env config, TOML calendars, and the CLI."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from lazypulse import ActionClass, After, Calendar, Cron
from lazypulse.launcher import DEFAULT_REVIEW_KEYWORDS, LauncherConfig, build_action_classifier, main

try:
    import croniter  # noqa: F401

    _HAS_CRONITER = True
except ImportError:
    _HAS_CRONITER = False

_CALENDAR = """
[schedules.etf_daily_stats]
task = "Daily ETF stats and digest"
cron = "45 15 * * MON-FRI"
tz = "Europe/Rome"
action = "external_send"
misfire_grace_minutes = 45
business_days = true
holidays = ["2026-12-25", 2027-01-01]

[schedules.anomaly_check]
task = "Investigate today's anomalies"
after = "etf_daily_stats"
within_minutes = 90
overlap = "allow"
"""


def _write(tmp_path: Path, body: str, name: str = "calendar.toml") -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


# --- Environment configuration ------------------------------------------ #


def test_config_requires_token_and_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_ID", raising=False)
    with pytest.raises(SystemExit, match="OWNER_ID"):
        LauncherConfig.from_env()


def test_config_rejects_a_non_numeric_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_ID", "@myhandle")  # a common mistake: the @name, not the id
    monkeypatch.setenv("BOT_TOKEN", "t")
    with pytest.raises(SystemExit, match="numeric user id"):
        LauncherConfig.from_env()


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MODEL", "STORE_DB", "BOT_ID", "AGENT_NAME", "TICK_SECONDS", "CALENDAR_FILE", "CALENDAR_TOOLS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_ID", "42")
    cfg = LauncherConfig.from_env()

    assert cfg.owner_id == 42
    assert cfg.store_db == "pulse.db"  # not /data — the Dockerfile pins that
    assert cfg.bot_id == "lazypulse"
    assert cfg.tick_seconds == 3.0
    assert cfg.calendar_file is None
    assert cfg.calendar_tools is False
    assert cfg.review_keywords  # the HITL heuristic is on by default


def test_config_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("BOT_ID", "tg-deepseek")
    monkeypatch.setenv("STORE_DB", "/data/pulse.db")
    monkeypatch.setenv("TICK_SECONDS", "0.5")
    monkeypatch.setenv("CALENDAR_TOOLS", "1")
    monkeypatch.setenv("CALENDAR_MIN_INTERVAL", "600")
    cfg = LauncherConfig.from_env()

    assert (cfg.bot_id, cfg.store_db, cfg.tick_seconds) == ("tg-deepseek", "/data/pulse.db", 0.5)
    assert cfg.calendar_tools is True
    assert cfg.calendar_min_interval == 600.0


def test_a_non_numeric_interval_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("TICK_SECONDS", "three")
    with pytest.raises(SystemExit, match="TICK_SECONDS must be a number"):
        LauncherConfig.from_env()


def test_empty_review_keywords_disables_the_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("REVIEW_KEYWORDS", "")
    assert LauncherConfig.from_env().review_keywords == ()
    assert build_action_classifier(()) is None


def test_action_classifier_flags_risky_messages() -> None:
    from datetime import UTC, datetime

    from lazypulse import InboundMessage

    classify = build_action_classifier(DEFAULT_REVIEW_KEYWORDS.split(","))
    assert classify is not None

    def action(text: str) -> ActionClass:
        return classify(InboundMessage(source="tg", message_id="1", received_at=datetime.now(UTC), text=text))

    assert action("manda una mail a Marco") is ActionClass.EXTERNAL_SEND
    assert action("che tempo fa domani?") is ActionClass.READ_PUBLIC


# --- TOML calendars ------------------------------------------------------ #


@pytest.mark.skipif(not _HAS_CRONITER, reason="croniter not installed")
def test_calendar_from_toml_round_trips_every_field(tmp_path: Path) -> None:
    calendar = Calendar.from_toml(_write(tmp_path, _CALENDAR))
    entries = {e.name: e for e in calendar}

    cron = entries["etf_daily_stats"]
    assert isinstance(cron, Cron)
    assert (cron.expr, cron.tz) == ("45 15 * * MON-FRI", "Europe/Rome")
    assert cron.action is ActionClass.EXTERNAL_SEND
    assert cron.misfire_grace == timedelta(minutes=45)
    assert cron.on_days is not None
    assert len(cron.on_days.holidays) == 2  # ISO string and native TOML date both accepted

    dep = entries["anomaly_check"]
    assert isinstance(dep, After)
    assert (dep.after, dep.within, dep.overlap) == ("etf_daily_stats", timedelta(minutes=90), "allow")


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("[schedules.x]\ntask = 't'\n", "exactly one of 'cron' or 'after'"),
        ("[schedules.x]\ntask = 't'\ncron = '* * * * *'\nafter = 'y'\n", "found both"),
        ("[schedules.x]\ncron = '* * * * *'\n", "needs a non-empty 'task'"),
        ("[schedules.x]\ntask = 't'\ncron = '* * * * *'\nbuisness_days = true\n", "unknown key"),
        ("[schedules.x]\ntask = 't'\ncron = '* * * * *'\naction = 'nope'\n", "unknown action"),
        ("[schedules.x]\ntask = 't'\ncron = '* * * * *'\noverlap = 'maybe'\n", 'must be "skip" or "allow"'),
        ("[schedules.x]\ntask = 't'\ncron = '* * * * *'\nholidays = ['not-a-date']\n", "not an ISO date"),
        ("[schedules.x]\ntask = 't'\nafter = 'ghost'\n", "does not declare"),
        # bool("false") is True — a schedule written down as off must not run.
        ("[schedules.x]\ntask = 't'\ncron = '* * * * *'\nenabled = 'false'\n", "must be true or false"),
        ("[schedules.x]\ntask = 't'\ncron = '* * * * *'\nbusiness_days = 'yes'\n", "must be true or false"),
        ("[other]\nx = 1\n", "no [schedules] table"),
        ("this is not toml", "invalid TOML"),
    ],
)
def test_a_malformed_calendar_fails_loudly(tmp_path: Path, body: str, fragment: str) -> None:
    """An operator-edited file must never leave the agent on an empty timetable."""
    with pytest.raises(ValueError, match=__import__("re").escape(fragment)):
        Calendar.from_toml(_write(tmp_path, body))


@pytest.mark.skipif(not _HAS_CRONITER, reason="croniter not installed")
def test_a_utf8_bom_does_not_break_the_calendar(tmp_path: Path) -> None:
    """Notepad and PowerShell write a BOM; tomllib rejects it unhelpfully."""
    path = tmp_path / "bom.toml"
    path.write_text(_CALENDAR, encoding="utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # the file really has one

    assert len(Calendar.from_toml(path)) == 2


def test_a_missing_calendar_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Calendar file not found"):
        Calendar.from_toml(tmp_path / "nope.toml")


@pytest.mark.skipif(not _HAS_CRONITER, reason="croniter not installed")
@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("cron = 'questa non e una cron'", "invalid cron expression or timezone"),
        ("cron = '45 15 * * MONFRI'", "invalid cron expression or timezone"),
        ("cron = '0 * * * *'\ntz = 'Non/Esiste'", "Unknown timezone"),
    ],
)
def test_a_bad_expression_or_timezone_is_caught_while_loading(tmp_path: Path, line: str, fragment: str) -> None:
    """check-calendar is the pre-deployment gate; it has to catch these."""
    with pytest.raises(ValueError, match=fragment):
        Calendar.from_toml(_write(tmp_path, f"[schedules.x]\ntask = 't'\n{line}\n"))


@pytest.mark.skipif(not _HAS_CRONITER, reason="croniter not installed")
def test_an_explicit_zero_grace_is_not_unlimited(tmp_path: Path) -> None:
    """0 means "tolerate no lateness" — the opposite of an omitted key."""
    body = "[schedules.x]\ntask = 't'\ncron = '0 * * * *'\nmisfire_grace_minutes = 0\n"
    assert next(iter(Calendar.from_toml(_write(tmp_path, body)))).misfire_grace == timedelta(0)  # type: ignore[union-attr]

    omitted = "[schedules.x]\ntask = 't'\ncron = '0 * * * *'\n"
    assert next(iter(Calendar.from_toml(_write(tmp_path, omitted, "b.toml")))).misfire_grace is None  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="cannot be negative"):
        Calendar.from_toml(_write(tmp_path, body.replace("= 0", "= -5"), "c.toml"))


def test_a_real_toml_false_still_disables(tmp_path: Path) -> None:
    body = "[schedules.x]\ntask = 't'\ncron = '0 * * * *'\nenabled = false\nbusiness_days = false\n"
    entry = next(iter(Calendar.from_toml(_write(tmp_path, body))))
    assert entry.enabled is False
    assert entry.on_days is None  # type: ignore[union-attr]


# --- Migration hazards --------------------------------------------------- #


def test_adapter_name_does_not_follow_agent_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Records written before this launcher hold source="telegram".

    The adapter name is what ``_maybe_reply`` looks up, so tying it to
    AGENT_NAME would leave tasks that were already in flight completing with
    nowhere to send their answer."""
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("AGENT_NAME", "tg-deepseek")
    monkeypatch.delenv("ADAPTER_NAME", raising=False)
    cfg = LauncherConfig.from_env()

    assert cfg.agent_name == "tg-deepseek"
    assert cfg.adapter_name == "telegram"  # TelegramInbox's own default, unchanged

    monkeypatch.setenv("ADAPTER_NAME", "custom")
    assert LauncherConfig.from_env().adapter_name == "custom"


def test_owner_chat_id_defaults_to_the_owner_and_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    assert LauncherConfig.from_env().owner_chat_id == 42

    monkeypatch.setenv("OWNER_CHAT_ID", "-1009999")  # a group chat
    assert LauncherConfig.from_env().owner_chat_id == -1009999


def test_the_deploy_image_installs_the_cron_extra() -> None:
    """CALENDAR_FILE with a cron entry imports croniter during sync, so the
    deployment that documents the calendar flow has to ship the extra."""
    root = Path(__file__).resolve().parents[2]
    requirements = (root / "deploy" / "tg-bot" / "requirements.txt").read_text(encoding="utf-8")
    line = next(line for line in requirements.splitlines() if line.startswith("lazypulse["))
    assert "cron" in line, line


# --- CLI ----------------------------------------------------------------- #


@pytest.mark.skipif(not _HAS_CRONITER, reason="croniter not installed")
def test_check_calendar_prints_what_it_declares(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-calendar", _write(tmp_path, _CALENDAR)]) == 0

    out = capsys.readouterr().out
    assert "2 schedule(s)" in out
    assert "cron 45 15 * * MON-FRI (Europe/Rome)" in out
    assert "after etf_daily_stats" in out
    assert "external_send" in out


def test_check_calendar_reports_invalid_and_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-calendar", _write(tmp_path, "[schedules.x]\ntask = 't'\n")]) == 1
    assert "invalid:" in capsys.readouterr().err


def test_cli_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        main([])
