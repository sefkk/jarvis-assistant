"""CLI entry point and subcommands."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import threading
import time
from datetime import datetime, timedelta

from rich.console import Console

from jarvis.config import Config, Paths, load_config, load_dotenv, resolve_home
from jarvis.exceptions import ConfigError, JarvisError
from jarvis.i18n import t
from jarvis.observability import health, logging_setup
from jarvis.utils import secrets
from jarvis.utils.timeutil import format_local, now_local

log = logging.getLogger("jarvis.main")


class VersionAction(argparse.Action):
    """`jarvis --version`: version from pyproject.toml, git commit and branch, home directory.
    Computed only when asked, so parser construction stays free."""

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, help="show the version, git commit and home directory")

    def __call__(self, parser, namespace, values, option_string=None):
        from jarvis.version import version_string

        try:
            home = resolve_home()
        except ConfigError:
            home = None
        print(version_string(home))
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis personal assistant")
    parser.add_argument("--debug", action="store_true", help="show thinking, API payloads (in the log) and timings")
    parser.add_argument("--version", action=VersionAction)
    sub = parser.add_subparsers(dest="command")
    chat = sub.add_parser("chat", help="interactive chat (default)")
    chat.add_argument("--debug", action="store_true", help="same as the top-level --debug")
    setup = sub.add_parser("setup", help="first-run wizard: language, files, profile, API key, optional features (safe to run again)")
    setup.add_argument("--language", action="store_true", help="only choose the main language (English or Turkish)")
    setup.add_argument(
        "--answers",
        metavar="FILE",
        help="run without questions from a TOML answers file (language, name, address_as, home_location, daily_budget_usd)",
    )
    setup.add_argument("--no-doctor", action="store_true", help="do not run `jarvis doctor` at the end")
    sub.add_parser("uninstall", help="remove the background services and, after asking, the Keychain entries and the local data")
    sub.add_parser("doctor", help="full health check")
    stats = sub.add_parser("stats", help="turn and tool statistics")
    stats.add_argument("--days", type=int, default=7, help="how many days back to count (default 7)")
    usage = sub.add_parser("usage", help="tokens and cost")
    usage.add_argument("--days", type=int, default=7, help="how many days back to count (default 7)")
    export = sub.add_parser("export", help="markdown transcript of a session")
    export.add_argument("--session", required=True, help="session id (see `jarvis sessions`, or the chat with --debug)")
    export.add_argument("--out", help="write to this file instead of stdout")
    sessions = sub.add_parser("sessions", help="list recent sessions")
    sessions.add_argument("--limit", type=int, default=20, help="how many sessions to list (default 20)")
    memory = sub.add_parser("memory", help="long-term memory (data/memory.md): list the notes with their dates, or forget one")
    memory_sub = memory.add_subparsers(dest="memory_command")
    memory_forget = memory_sub.add_parser("forget", help="remove every note containing this text")
    memory_forget.add_argument("text", nargs="+", help="the text to match (case-insensitive)")
    google = sub.add_parser("google", help="Google OAuth (Calendar events, Gmail read and drafts only)")
    google_sub = google.add_subparsers(dest="google_command", required=True)
    google_login = google_sub.add_parser("login", help="run the OAuth flow in the browser and store the token in the Keychain")
    google_login.add_argument("--client-secret", help="path to the OAuth client JSON downloaded from Google Cloud Console")
    google_sub.add_parser("logout", help="remove the stored Google token")
    health_p = sub.add_parser("health", help="Apple Health import (from the iCloud inbox or a full export)")
    health_sub = health_p.add_subparsers(dest="health_command", required=True)
    health_sub.add_parser("sync", help="import new files from the Health inbox folder")
    health_import = health_sub.add_parser("import", help="backfill from the Health app's 'Export All Health Data' zip")
    health_import.add_argument("zip", help="path to export.zip")
    sched = sub.add_parser("scheduler", help="background timer scheduler (launchd user agent)")
    sched_sub = sched.add_subparsers(dest="scheduler_command", required=True)
    sched_run = sched_sub.add_parser("run", help="run the scheduler loop in the foreground")
    sched_run.add_argument("--once", action="store_true", help="fire due timers once and exit")
    sched_sub.add_parser("install", help="install the launchd agent so timers fire even when the CLI is closed")
    sched_sub.add_parser("uninstall", help="remove the launchd agent")
    sched_sub.add_parser("status", help="is the agent installed and running")
    alarms = sub.add_parser("alarms", help="alarms (different from timers): list, cancel, stop or snooze the one ringing, ring a test")
    alarms_sub = alarms.add_subparsers(dest="alarms_command")
    alarms_sub.add_parser("list", help="pending alarms and whether one is ringing (the default)")
    alarms_cancel = alarms_sub.add_parser("cancel", help="cancel an alarm by id or label ('all' cancels every alarm)")
    alarms_cancel.add_argument("target", help="alarm id, label, or 'all'")
    alarms_sub.add_parser("stop", help="stop the alarm that is ringing now")
    alarms_snooze = alarms_sub.add_parser("snooze", help="snooze the ringing alarm (default alarm.snooze_min)")
    alarms_snooze.add_argument("minutes", nargs="?", type=int, default=None, help="snooze this many minutes (default alarm.snooze_min)")
    alarms_test = alarms_sub.add_parser("test", help="ring the alarm sound now, with the dialog and the volume, to try it out")
    alarms_test.add_argument("--seconds", type=int, default=20, help="stop by itself after this many seconds (default 20)")
    brief = sub.add_parser("brief", help="the daily brief: agenda, weather, reminders, football, alarms")
    brief.add_argument("--date", default="today", help="the day: today, tomorrow or YYYY-MM-DD (default today)")
    brief.add_argument("--send", choices=["telegram", "voice"], help="send the brief to this channel now (the automatic brief's versions)")
    brief.add_argument("--raw", action="store_true", help="print the assembled data instead of the model's summary")
    brief_sub = brief.add_subparsers(dest="brief_command")
    brief_schedule = brief_sub.add_parser(
        "schedule", help="the automatic morning brief: show the schedule and the next run, or turn it on/off"
    )
    brief_schedule.add_argument("state", nargs="?", choices=["on", "off"], help="write enabled = true/false to config.local.toml")
    tg = sub.add_parser("telegram", help="Telegram bot interface (long polling, allowlisted user only)")
    tg_sub = tg.add_subparsers(dest="telegram_command", required=True)
    tg_sub.add_parser("run", help="run the bot in the foreground")
    tg_sub.add_parser("install", help="install the launchd agent that runs the bot in the background")
    tg_sub.add_parser("uninstall", help="remove the launchd agent")
    tg_sub.add_parser("status", help="agent status, kill switch state")
    tg_sub.add_parser("set-token", help="store the bot token in the Keychain")
    tg_sub.add_parser("whoami", help="setup mode: print the user id and chat id of the first message received")
    tg_sub.add_parser("enable", help="re-enable the bot after /stop (requires /start from the phone first)")
    voice = sub.add_parser("voice", help="hands-free voice service: wake word, hotkey, spoken replies (needs use_voice = true)")
    voice.add_argument(
        "voice_command",
        nargs="?",
        default="status",
        choices=[
            "run",
            "install",
            "uninstall",
            "status",
            "pause",
            "resume",
            "calibrate",
            "debug-record",
            "test-stt",
            "list-voices",
            "set-elevenlabs-key",
            "say",
        ],
        help="status (default) shows the agent and whether it listens; run starts the service in this terminal; install/uninstall the launchd agent; pause/resume stop and restart wake-word"
        " listening without touching the agent; calibrate shows live wake-word scores;"
        " debug-record records one utterance like the service, saves the WAV the STT engine gets and prints its levels and transcript;"
        " test-stt records 15 sentences once and scores every speech-to-text option on them (word and name accuracy, latency, cost);"
        " list-voices prints the ElevenLabs voices on your account (name + id); set-elevenlabs-key stores the API key in the Keychain;"
        " say speaks a text through the configured engine after the spoken-form normalization (numbers, times, dates as words)",
    )
    voice.add_argument("text", nargs="*", help="say: the text to speak (quote it)")
    voice.add_argument("--dry-run", action="store_true", help="say: only print the normalized text, speak nothing")
    voice.add_argument(
        "--language", choices=["tr", "en"], default=None, help="say: force the language instead of detecting it per sentence"
    )
    voice.add_argument("--foreground", action="store_true", help="print every state change, transcript, reply and timing")
    voice.add_argument("--seconds", type=float, default=30.0, help="calibrate: how long to listen")
    voice.add_argument("--out", default=None, metavar="DIR", help="debug-record: folder for the WAV (default: a new temp folder)")
    voice.add_argument(
        "--compare", default=None, metavar="DIR", help="test-stt: score an earlier session (data/stt_test/<stamp>) instead of recording"
    )
    voice.add_argument(
        "--engines",
        default=None,
        metavar="LIST",
        help="test-stt: comma-separated option keys (current, current+hints, large-v3, turkish, apple, elevenlabs, hybrid ...)",
    )
    voice.add_argument(
        "--cloud", action="store_true", help="test-stt: also send the recordings to ElevenLabs Scribe (paid, audio leaves the Mac)"
    )
    voice.add_argument(
        "--download",
        action="store_true",
        help="test-stt: download the Whisper models that are not cached yet (large-v3 and the Turkish model are about 3 GB each)",
    )
    voice.add_argument("--verbose", action="store_true", help="test-stt: print every transcript under the table")
    voice.add_argument(
        "--for",
        dest="pause_for",
        default=None,
        metavar="DURATION",
        help="pause: listen again automatically after this long (60m, 1h, 1h30m, 90)",
    )
    services = sub.add_parser("services", help="the launchd agents (scheduler, Telegram, voice) as one group")
    services_sub = services.add_subparsers(dest="services_command", required=True)
    services_sub.add_parser("reinstall", help="rewrite and reload every installed agent so it runs the current `jarvis` binary")
    services_sub.add_parser("status", help="which binary the agents run, and whether each one is installed and running")
    dashboard = sub.add_parser(
        "dashboard", help="the local monitoring and settings page in the browser (127.0.0.1 only; runs in this terminal until Ctrl-C)"
    )
    dashboard.add_argument("--no-open", action="store_true", help="print the one-time link instead of opening the browser")
    dashboard.add_argument(
        "--port", type=int, default=None, help="listen on this port instead of dashboard.port (a busy port falls back to a free one)"
    )
    shortcuts = sub.add_parser("shortcuts", help="Apple Shortcuts: which ones exist on this Mac and which are allowlisted for Jarvis")
    shortcuts_sub = shortcuts.add_subparsers(dest="shortcuts_command", required=True)
    shortcuts_sub.add_parser("list", help="list every Shortcut on this Mac and mark the allowlisted ones (copy names into config.toml)")
    network = sub.add_parser("network", help="internet test and history: what the internet_test tool does, from the terminal")
    network_sub = network.add_subparsers(dest="network_command", required=True)
    network_test = network_sub.add_parser(
        "test", help="diagnostics plus the networkQuality speed test (about 20 s); --quick skips the speed test"
    )
    network_test.add_argument("--quick", action="store_true", help="diagnostics only: interface, router, DNS, public hosts, Wi-Fi signal")
    network_history = network_sub.add_parser("history", help="averages, worst times and failures from the stored tests")
    network_history.add_argument("--range", choices=["day", "week", "month"], default="week", help="the period to summarize (default week)")
    presence = sub.add_parser("presence", help='"am I home" / "who is home": setup, status, one scan')
    presence_sub = presence.add_subparsers(dest="presence_command", required=True)
    presence_sub.add_parser(
        "setup", help="interactive, at home: save the router's MAC as the home network and pick your phone and other devices"
    )
    presence_sub.add_parser("status", help="home network, people and their states, last scan")
    presence_sub.add_parser("scan", help="run one presence scan now (normally the scheduler agent does this every scan_interval_s)")
    calendar = sub.add_parser("calendar", help="calendar settings: the [[calendar.filters]] and what they hide")
    calendar_sub = calendar.add_subparsers(dest="calendar_command", required=True)
    calendar_filters = calendar_sub.add_parser(
        "filters", help="list the active calendar filters and the events they hide in the coming days"
    )
    calendar_filters.add_argument("--days", type=int, default=7, help="how many days ahead to count (default 7)")
    location = sub.add_parser(
        "location", help="the Current Location Jarvis uses (where you are now, not your home Location): show it, or refresh it"
    )
    location.add_argument(
        "location_command",
        nargs="?",
        default="show",
        choices=["show", "refresh"],
        help="show (default): the stored location, its source and age; refresh: look it up now (Shortcut, then CoreLocationCLI, then IP)",
    )
    completion = sub.add_parser("completion", help="print a shell completion script for the jarvis subcommands")
    completion.add_argument("shell", choices=["zsh", "bash"], help="the shell to print the script for")
    key = sub.add_parser("key", help="manage secrets in the macOS Keychain")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    key_set = key_sub.add_parser("set", help="store a secret (prompts for the value)")
    key_set.add_argument("name", help=f"secret name, e.g. {secrets.ANTHROPIC_API_KEY}")
    key_del = key_sub.add_parser("delete", help="remove a secret")
    key_del.add_argument("name", help="secret name, e.g. anthropic_api_key")
    return parser


def bootstrap(*, console_logs: bool = False) -> tuple[Config, Paths]:
    """Load config, .env and logging. Raises ConfigError on an invalid config.

    Log records stay in the files; user-facing messages go through the sink and the
    health check, so the stderr handler is off by default to avoid printing errors twice.
    """
    from jarvis import i18n

    paths = Paths(resolve_home())
    load_dotenv(paths.env_file)
    config = load_config(paths.config_file)
    i18n.set_language(config.general.language)
    paths.ensure_dirs()
    logging_setup.setup_logging(
        paths.logs_dir,
        level=config.general.log_level,
        app_retention_days=config.logging.app_log_retention_days,
        audit_retention_days=config.logging.audit_log_retention_days,
        console=console_logs,
    )
    return config, paths


def parse_args(parser: argparse.ArgumentParser, argv: list[str] | None = None) -> argparse.Namespace:
    """`parse_args`, except that `jarvis voice say --dry-run "text"` works: argparse binds the empty `text`
    list before it sees the option, so the text would be "unrecognized"; the leftovers are the text."""
    args, extra = parser.parse_known_args(argv)
    if extra and args.command == "voice" and getattr(args, "voice_command", None) == "say":
        args.text = list(args.text or []) + extra
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    return args


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parse_args(parser, argv)
    console = Console()
    try:
        if args.command in ("key", "google"):
            _use_configured_language()
        if args.command == "key":
            return cmd_key(args, console)
        if args.command == "google":
            return cmd_google(args, console)
        if args.command == "completion":
            print(completion_script(parser, args.shell))
            return 0
        if args.command == "setup":
            return cmd_setup(args, console)
        if args.command in (None, "chat") and not _ready_for_chat(console):
            return 1
        config, paths = bootstrap()
        if args.command in (None, "chat"):
            return cmd_chat(config, paths, console, debug=bool(args.debug))
        if args.command == "doctor":
            return cmd_doctor(config, paths, console)
        if args.command == "uninstall":
            from rich.prompt import Confirm

            from jarvis.uninstall import run_uninstall

            return run_uninstall(paths, console, lambda question: bool(Confirm.ask(question, default=False, console=console)))
        if args.command in ("stats", "usage", "export", "sessions"):
            return cmd_report(args, paths, console, config)
        if args.command == "memory":
            return cmd_memory(args, paths, console)
        if args.command == "health":
            return cmd_health(args, config, paths, console)
        if args.command == "brief":
            return cmd_brief(args, config, paths, console)
        if args.command == "telegram":
            return cmd_telegram(args, config, paths, console)
        if args.command == "scheduler":
            return cmd_scheduler(args, config, paths, console)
        if args.command == "voice":
            return cmd_voice(args, config, paths, console)
        if args.command == "services":
            return cmd_services(args, paths, console)
        if args.command == "dashboard":
            return cmd_dashboard(args, config, paths, console)
        if args.command == "shortcuts":
            return cmd_shortcuts(args, config, console)
        if args.command == "network":
            return cmd_network(args, config, paths, console)
        if args.command == "presence":
            return cmd_presence(args, config, paths, console)
        if args.command == "location":
            return cmd_location(args, config, paths, console)
        if args.command == "calendar":
            return cmd_calendar(args, config, console)
        if args.command == "alarms":
            return cmd_alarms(args, config, paths, console)
    except ConfigError as exc:
        console.print(f"[bold red]{t('cli.common.config_error')}[/] {exc}")
        return 1
    except JarvisError as exc:
        console.print(f"[bold red]{t('cli.common.error')}[/] {exc}")
        return 1
    parser.print_help()
    return 0


def _use_configured_language() -> None:
    """`jarvis key` and `jarvis google` run without `bootstrap`; they still answer in the main language when
    config.toml exists (English otherwise)."""
    from jarvis import i18n

    try:
        i18n.set_language(load_config(Paths(resolve_home()).config_file).general.language)
    except Exception:  # no or a broken config: the default language
        pass


def _yes_no(value: bool) -> str:
    return t("cli.common.yes" if value else "cli.common.no")


def cmd_setup(args: argparse.Namespace, console: Console) -> int:
    """`jarvis setup`: runs before the config is loaded, because it is what creates config.toml."""
    from pathlib import Path

    from jarvis.setup_wizard import read_answers, run_setup

    answers = read_answers(Path(args.answers)) if getattr(args, "answers", None) else None

    def doctor() -> int:
        config, paths = bootstrap()
        return cmd_doctor(config, paths, console)

    return run_setup(
        resolve_home(),
        console,
        language_only=bool(getattr(args, "language", False)),
        answers=answers,
        run_doctor=None if getattr(args, "no_doctor", False) else doctor,
    )


def _ready_for_chat(console: Console) -> bool:
    """Before the chat: without config.toml Jarvis says that `jarvis setup` creates it and, in a terminal,
    offers to start the wizard right away, instead of failing on the missing file."""
    from rich.prompt import Confirm

    if Paths(resolve_home()).config_file.is_file():
        return True
    console.print(t("cli.first_run.missing", what="config.toml") + " " + t("cli.first_run.hint"))
    if not sys.stdin.isatty() or not Confirm.ask(t("cli.first_run.ask"), default=True, console=console):
        return False
    return cmd_setup(argparse.Namespace(language=False, answers=None, no_doctor=True), console) == 0


def cmd_key(args: argparse.Namespace, console: Console) -> int:
    if args.key_command == "set":
        value = getpass.getpass(t("cli.key.prompt", name=args.name)).strip()
        if not value:
            console.print(f"[red]{t('cli.key.empty')}[/]")
            return 1
        secrets.set_secret(args.name, value)
        console.print(t("cli.key.stored", name=f"[bold]{args.name}[/]", service=secrets.KEYCHAIN_SERVICE))
        return 0
    secrets.delete_secret(args.name)
    console.print(t("cli.key.removed", name=f"[bold]{args.name}[/]"))
    return 0


def cmd_google(args: argparse.Namespace, console: Console) -> int:
    from pathlib import Path

    from jarvis.utils import google_auth

    if args.google_command == "login":
        secret = Path(args.client_secret).expanduser() if args.client_secret else None
        granted = set(google_auth.login(secret))
        console.print(t("cli.google.login_done"))
        missing = [s.rsplit("/", 1)[-1] for s in google_auth.SCOPES if s not in granted]
        if missing:
            console.print(f"[yellow]{t('cli.common.warning').capitalize()}[/] " + t("cli.google.missing_scopes", scopes=", ".join(missing)))
            return 1
        console.print(t("cli.google.scopes"))
        return 0
    google_auth.logout()
    console.print(t("cli.google.logout"))
    return 0


def cmd_report(args: argparse.Namespace, paths: Paths, console: Console, config: Config | None = None) -> int:
    from pathlib import Path

    from jarvis.observability import reports
    from jarvis.observability.db import Database

    db = Database(paths.db_file)
    try:
        if args.command == "stats":
            text = reports.stats_report(db, args.days)
        elif args.command == "usage":
            text = reports.usage_report(db, args.days, config)
        elif args.command == "sessions":
            rows = db.conn.execute(
                "SELECT s.id, s.started_at, s.model_main, COUNT(t.id) AS turns FROM sessions s"
                " LEFT JOIN turns t ON t.session_id = s.id GROUP BY s.id ORDER BY s.started_at DESC LIMIT ?",
                (args.limit,),
            ).fetchall()
            text = "\n".join(
                t("cli.report.session", id=r["id"], started=r["started_at"], model=r["model_main"], turns=r["turns"]) for r in rows
            ) or t("cli.report.no_sessions")
        else:
            try:
                text = reports.export_session(db, args.session)
            except ValueError as exc:
                console.print(f"[red]{exc}[/]")
                return 1
            if args.out:
                Path(args.out).expanduser().write_text(text, encoding="utf-8")
                console.print(t("cli.report.wrote", path=args.out), markup=False)
                return 0
    finally:
        db.close()
    console.print(text, markup=False, highlight=False, soft_wrap=True)
    return 0


def cmd_memory(args: argparse.Namespace, paths: Paths, console: Console) -> int:
    """`jarvis memory` lists data/memory.md with dates; `jarvis memory forget <text>` removes matching notes."""
    from jarvis.exceptions import ToolValidationError
    from jarvis.memory import MAX_BYTES, MemoryStore

    store = MemoryStore(paths.memory_file)
    if getattr(args, "memory_command", None) == "forget":
        try:
            removed = store.forget(" ".join(args.text))
        except ToolValidationError as exc:
            console.print(f"[red]{exc}[/]")
            return 1
        console.print(t("cli.memory.removed", count=removed) if removed else t("cli.memory.no_match"), markup=False)
        return 0
    entries = store.entries()
    if not entries:
        console.print(t("cli.memory.empty", path=paths.memory_file), markup=False, highlight=False)
        return 0
    for entry in entries:
        console.print(f"{entry.date or '          '}  {entry.text}", markup=False, highlight=False, soft_wrap=True)
    console.print(
        "\n" + t("cli.memory.summary", count=len(entries), size=store.size_bytes(), max=MAX_BYTES, path=paths.memory_file),
        style="dim",
        markup=False,
        highlight=False,
    )
    return 0


def cmd_health(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    from pathlib import Path

    from jarvis.observability.db import Database
    from jarvis.training import health_import

    db = Database(paths.db_file)
    try:
        if args.health_command == "sync":
            result = health_import.sync_inbox(config, paths, db)
            console.print(t("cli.health.sync", result=result.one_line()), markup=False)
            for err in result.errors[:5]:
                console.print(f"[yellow]{err}[/]", markup=False)
            return 0
        zip_path = Path(args.zip).expanduser()
        if not zip_path.is_file():
            console.print(t("cli.health.not_found", path=zip_path), style="red", markup=False)
            return 1
        console.print(t("cli.health.importing", path=zip_path), markup=False)
        result = health_import.import_export_zip(zip_path, db)
        console.print(result.one_line(), markup=False)
        for err in result.errors[:5]:
            console.print(f"[yellow]{err}[/]", markup=False)
        return 0
    finally:
        db.close()


def cmd_scheduler(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    from jarvis.scheduler import launchd, service

    if args.scheduler_command == "run":
        if args.once:
            fired = service.run_once(config, paths)
            console.print(t("cli.scheduler.fired", count=fired))
            return 0
        console.print(t("cli.scheduler.running"))
        try:
            service.run_forever(config, paths)
        except KeyboardInterrupt:
            pass
        return 0
    if args.scheduler_command == "install":
        launchd.install(paths)
        console.print(t("cli.scheduler.installed", path=launchd.plist_path()), markup=False)
        return 0
    if args.scheduler_command == "uninstall":
        launchd.uninstall()
        console.print(t("cli.scheduler.removed"))
        return 0
    status = launchd.status()
    console.print(
        t("cli.common.scheduler_status", installed=_yes_no(status.installed), running=_yes_no(status.running))
        + (f" (pid {status.pid})" if status.pid else "")
        + (f"; {status.detail}" if getattr(status, "detail", "") else ""),
        markup=False,
    )
    return 0 if status.installed and status.running else 1


def startup_health_sync(config: Config, paths: Paths) -> str | None:
    """Quick import of new Health inbox files at startup. Runs in the warm-up thread
    and returns a one-line notice when there is something to say."""
    if not config.training.sync_on_startup:
        return None
    try:
        from jarvis.observability.db import Database
        from jarvis.training import health_import

        db = Database(paths.db_file)
        try:
            result = health_import.sync_inbox(config, paths, db)
        finally:
            db.close()
        if getattr(result, "new", 0):
            return t("cli.health.sync", result=result.one_line())
        return None
    except Exception as exc:  # a broken inbox must not stop Jarvis
        return t("cli.health.sync_skipped", error=f"{type(exc).__name__}: {exc}")


def start_location_refresh(config: Config, paths: Paths) -> None:
    """Kick off the Current Location lookup in its own daemon thread at the start of a CLI session, so
    the prompt never waits for a Shortcut or the network (the Assistant's `new_session` does the same
    for the voice service; a refresh already in flight or done a minute ago is skipped)."""
    try:
        from jarvis.location import service as location_service

        location_service.refresh_in_background(config, paths)
    except Exception:  # never let the lookup touch the startup
        pass


class Warmup(threading.Thread):
    """Loads the slow parts while the prompt is already on screen: the Anthropic SDK,
    the brain and the tool modules (about 0.8 s together), then the Health inbox sync. The first
    turn joins the thread, so nothing is used before it is ready."""

    def __init__(self, config: Config, paths: Paths) -> None:
        super().__init__(name="jarvis-warmup", daemon=True)
        self.config = config
        self.paths = paths
        self.notices: list[str] = []
        self.import_ms = 0.0

    def run(self) -> None:
        started = time.perf_counter()
        try:
            import importlib
            import pkgutil

            import jarvis.assistant  # noqa: F401  (anthropic SDK, brain, registry)
            import jarvis.tools

            for info in pkgutil.iter_modules(jarvis.tools.__path__):
                if not info.name.startswith("_"):
                    try:
                        importlib.import_module(f"jarvis.tools.{info.name}")
                    except Exception:  # the registry reports broken tool modules itself
                        pass
        except Exception as exc:  # the first turn imports again and reports properly
            self.notices.append(t("cli.chat.warmup_failed", error=f"{type(exc).__name__}: {exc}"))
        self.import_ms = (time.perf_counter() - started) * 1000
        notice = startup_health_sync(self.config, self.paths)
        if notice:
            self.notices.append(notice)


def cmd_brief(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    """`jarvis brief`: the model summarizes the assembled brief; --raw prints the data; --send posts it;
    `schedule [on|off]` shows or toggles the automatic morning brief."""
    from jarvis.assistant import Assistant
    from jarvis.io.text import TerminalOutput
    from jarvis.tools import brief as brief_tool

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    if getattr(args, "brief_command", None) == "schedule":
        return cmd_brief_schedule(args, config, paths, out)
    if args.send == "voice":
        from jarvis.scheduler import brief as schedule

        blocked = schedule.voice_blocked(schedule.Probes.for_paths(paths))
        if blocked:
            out(t("cli.brief.not_spoken", reason=blocked))
            return 1
        schedule.request_voice(config, paths)
        out(t("cli.brief.voice_requested"))
        return 0
    if args.send == "telegram":
        from jarvis.scheduler import brief as schedule

        try:
            text = schedule.send_telegram(config, paths)
        except JarvisError as exc:
            console.print(f"[yellow]{exc}[/]")
            return 1
        out(text)
        console.print(t("cli.brief.sent_telegram"), style="dim")
        return 0
    if args.raw:
        from jarvis.observability.db import Database
        from jarvis.tools.base import Runtime, ToolContext
        from jarvis.tools.registry import ToolRegistry

        db = Database(paths.db_file)
        try:
            ctx = ToolContext.build(config, paths)
            registry = ToolRegistry.discover(ctx)
            runtime = Runtime(client=None, db=db, usage_tracker=None, registry=registry, budget=None)
            for tool in registry:
                if tool.ctx is not None:
                    tool.ctx.runtime = runtime
            data = brief_tool.build_brief(config, paths, runtime, args.date)
            text = data.as_text()
        finally:
            db.close()
    else:
        assistant = Assistant(config, paths)
        try:
            prompt = f"Give me the daily brief for {args.date} (call daily_brief)."
            result = assistant.handle(prompt, TerminalOutput(console))
            text = result.reply
        finally:
            assistant.close()
    if args.raw:
        console.print(text, markup=False, highlight=False)
    return 0


def cmd_brief_schedule(args: argparse.Namespace, config: Config, paths: Paths, out) -> int:
    """`jarvis brief schedule [on|off]`: the schedule, the next run and the last outcome; on/off writes the overlay."""
    from jarvis.config import update_local_config
    from jarvis.observability.db import Database
    from jarvis.scheduler import brief as schedule
    from jarvis.scheduler import launchd

    if args.state:
        update_local_config(paths.config_file, "brief.schedule", {"enabled": args.state == "on"})
        config = load_config(paths.config_file)
        out(t("cli.brief.schedule_written", value=args.state == "on", file=paths.local_config_file.name))
    db = Database(paths.db_file)
    try:
        state = schedule.load_state(db)
    finally:
        db.close()
    out(t("cli.brief.schedule", text=schedule.describe(config.brief.schedule, now_local(), state)))
    status = launchd.status()
    if config.brief.schedule.enabled and not status.running:
        out(t("cli.brief.scheduler_down", detail=status.detail))
        return 1
    return 0


def cmd_telegram(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    from jarvis.interfaces import telegram_bot, telegram_launchd

    cmd = args.telegram_command
    if cmd == "run":
        telegram_bot.run_bot(config, paths)
        return 0
    if cmd == "install":
        telegram_launchd.install(paths)
        console.print(t("cli.telegram.installed", path=telegram_launchd.plist_path()), markup=False)
        return 0
    if cmd == "uninstall":
        telegram_launchd.uninstall()
        console.print(t("cli.telegram.removed"))
        return 0
    if cmd == "set-token":
        telegram_bot.set_token()
        console.print(t("cli.telegram.token_stored"))
        return 0
    if cmd == "whoami":
        telegram_bot.whoami(config, paths)
        return 0
    if cmd == "enable":
        console.print(telegram_bot.enable_from_mac(paths), markup=False)
        return 0
    status = telegram_launchd.status()
    console.print(
        t("cli.common.agent_status", installed=_yes_no(status.installed), running=_yes_no(status.running))
        + (f" (pid {status.pid})" if status.pid else "")
        + (f"; {status.detail}" if getattr(status, "detail", "") else ""),
        markup=False,
    )
    from jarvis.interfaces.telegram_state import load_state

    state = load_state(paths.telegram_state_file)
    if state.stopped:
        console.print(
            t("cli.telegram.kill_stopped")
            + t("cli.telegram.kill_reenable_requested" if state.reenable_requested else "cli.telegram.kill_send_start"),
            markup=False,
        )
    else:
        console.print(t("cli.telegram.kill_active"), markup=False)
    return 0


def cmd_voice(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    """`jarvis voice [status|run|install|uninstall|pause|resume|calibrate|debug-record|test-stt|say]`. Bare `jarvis voice` is
    `status`: `run` would start a second service next to the launchd agent, so it has to be asked for."""
    from jarvis.voice import launchd as voice_launchd

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    cmd = args.voice_command
    if cmd == "list-voices":
        from jarvis.voice.elevenlabs import list_voices_cli

        return list_voices_cli(out)
    if cmd == "set-elevenlabs-key":
        from jarvis.voice.elevenlabs import set_key_cli

        return set_key_cli(out=out)
    if cmd == "say":
        from jarvis.voice.say import say_cli

        return say_cli(config, paths, " ".join(args.text), language=args.language, dry_run=bool(args.dry_run), out=out)
    if cmd == "run":
        from jarvis.voice.service import run_service

        return run_service(config, paths, foreground=bool(args.foreground), out=out)
    if cmd == "calibrate":
        from jarvis.voice.service import run_calibration

        return run_calibration(config, paths, seconds=args.seconds, out=out)
    if cmd == "debug-record":
        from jarvis.voice.debug import run_debug_record

        return run_debug_record(config, paths, out_dir=args.out, out=out)
    if cmd == "test-stt":
        from jarvis.voice.stt_test import run_test_stt

        return run_test_stt(
            config,
            paths,
            compare_dir=args.compare,
            engines=args.engines,
            cloud=bool(args.cloud),
            download=bool(args.download),
            verbose=bool(args.verbose),
            out=out,
        )
    if cmd == "install":
        if not config.general.use_voice:
            out(t("cli.voice.needs_use_voice"))
            return 1
        voice_launchd.install(paths)
        out(t("cli.voice.installed", path=voice_launchd.plist_path()))
        out(t("cli.voice.permissions", python=voice_launchd.python_executable()))
        return 0
    if cmd == "uninstall":
        voice_launchd.uninstall()
        out(t("cli.voice.removed"))
        return 0
    from jarvis.voice import pause as voice_pause

    if cmd == "pause":
        minutes = None
        if getattr(args, "pause_for", None):
            minutes = voice_pause.parse_duration(args.pause_for)
            if minutes is None:
                out(t("cli.voice.bad_duration", value=repr(args.pause_for)))
                return 1
        try:
            state = voice_pause.pause(paths.voice_pause_file, by="cli", minutes=minutes)
        except ValueError as exc:
            out(str(exc))
            return 1
        paused = (
            t("cli.voice.paused_for", duration=voice_pause.duration_label(minutes), until=voice_pause._local(state.until))
            if minutes
            else t("cli.voice.paused")
        )
        out(paused + " " + t("cli.voice.paused_detail"))
        if not voice_launchd.status().running:
            out(t("cli.voice.not_running"))
        return 0
    if cmd == "resume":
        out(t("cli.voice.resumed") if voice_pause.resume(paths.voice_pause_file) else t("cli.voice.not_paused"))
        return 0
    status = voice_launchd.status()
    out(
        t("cli.common.agent_status", installed=_yes_no(status.installed), running=_yes_no(status.running))
        + (f" (pid {status.pid})" if status.pid else "")
        + (f"; {status.detail}" if status.detail else "")
    )
    out(t("cli.voice.listening", state=voice_pause.describe(voice_pause.load_state(paths.voice_pause_file))))
    return 0 if status.installed and status.running else 1


def cmd_doctor(config: Config, paths: Paths, console: Console) -> int:
    from jarvis.utils import platform_check

    reason = platform_check.unsupported_reason()
    if reason:
        console.print(platform_check.unsupported_message(reason), markup=False, highlight=False)
        return 1
    checks = health.run_checks(config, paths, full=True)
    console.print(health.format_checklist(checks), markup=False, highlight=False)
    return 0 if all(c.ok for c in checks) else 1


def cmd_chat(
    config: Config,
    paths: Paths,
    console: Console,
    *,
    debug: bool,
    source=None,
    assistant_factory=None,
) -> int:
    """The chat. Fast start: quick checks, one greeting line, the prompt; the heavy
    imports run in a warm-up thread and the Assistant is built on the first message."""
    started = time.perf_counter()
    from jarvis.io.text import TerminalInput, TerminalOutput

    checks = health.run_checks(config, paths)
    failed = [c for c in checks if not c.ok]
    for check in failed:
        console.print(f"[yellow]{t('cli.common.warning')}[/] {check.name}: {check.detail}")
    if any(c.fatal for c in failed):
        console.print(f"[bold red]{t('cli.chat.no_api_key')}[/]")
        return 1
    if not paths.profile_file.is_file():
        console.print(f"[yellow]{t('cli.chat.no_profile')}[/] {t('cli.chat.no_profile_hint')}")

    warmup = Warmup(config, paths)
    warmup.start()
    start_location_refresh(config, paths)  # background, returns at once
    console.print(greeting_line(config, paths), markup=False, highlight=False)
    if debug:
        console.print("[dim]--debug: full API payloads are written (redacted) to logs/jarvis.log at INFO level.[/]")
        if config.general.use_voice:
            console.print("[dim]use_voice = true: the hands-free service runs separately (`jarvis voice`). This is the text chat.[/]")
        console.print(f"[dim]prompt ready in {(time.perf_counter() - started) * 1000:.0f} ms (warm-up continues in the background)[/]")

    source = source or TerminalInput(console, config.general.exit_words)
    sink = TerminalOutput(console, debug=debug, language=config.general.language)
    factory = assistant_factory or (lambda: _build_assistant(config, paths, debug))
    assistant = None
    try:
        while True:
            text = source.read()
            if text is None:
                break
            if not text.strip():
                continue
            cancelled = _cancel_sleep_on_request(text, paths, assistant)
            if cancelled:  # "cancel" / "iptal" while the Mac is about to sleep: no model call
                console.print(cancelled, markup=False, highlight=False)
                continue
            if assistant is None:
                warmup.join()
                for notice in warmup.notices:
                    console.print(notice, style="dim", markup=False, highlight=False)
                assistant = factory()
                # "📝 Noted: …" from the end-of-session pass (at exit) lands in the terminal too
                assistant.memory_notify = lambda line: console.print(line, style="dim", markup=False, highlight=False)
                if debug:
                    sink.debug(
                        f"warm-up imports {warmup.import_ms:.0f} ms · model {config.models.main_model}"
                        f" · session {assistant.session_id} · tools: {', '.join(assistant.registry.names())}"
                    )
            sink.begin_turn()  # "Thinking…" until the first text arrives
            try:
                result = assistant.handle(text, sink)
            except KeyboardInterrupt:
                sink.finish()
                console.print(t("cli.common.interrupted"), style="dim")
                continue
            if debug:
                stages = ", ".join(f"{k} {v:.0f}ms" for k, v in result.timings.items())
                sink.debug(
                    f"turn {result.turn_id}: {result.total_ms:.0f} ms total; {stages}; "
                    f"tokens in {result.usage.input_tokens} / out {result.usage.output_tokens}"
                    f" (cache read {result.usage.cache_read_tokens}, write {result.usage.cache_write_tokens})"
                    + ("; history compacted" if result.compacted else "")
                )
    finally:
        if assistant is not None:
            assistant.close()  # ends the session (ended_at in SQLite) and closes the database
        console.print(goodbye_line(config, paths), markup=False, highlight=False)
    return 0


def _cancel_sleep_on_request(text: str, paths: Paths, assistant) -> str | None:
    """A `sleep_mac` countdown or a scheduled sleep whose notice is out: "cancel" / "iptal" cancels it here."""
    from jarvis.tools import power  # noqa: PLC0415 - only reached for a typed message

    if not power.is_cancel_word(text):
        return None
    try:
        return power.cancel_on_request(text, paths=paths, db=getattr(assistant, "db", None))
    except Exception:
        log.exception("sleep cancel check failed")
        return None


def _build_assistant(config: Config, paths: Paths, debug: bool):
    from jarvis.assistant import Assistant

    return Assistant(config, paths, debug=debug)


def _profile_name(paths: Paths, sep: str = ", ") -> str:
    from jarvis.profile import load_profile

    try:
        profile = load_profile(paths.profile_file)
    except Exception:  # a broken profile must not spoil the greeting or the exit
        return ""
    return f"{sep}{profile.name}" if profile is not None and profile.name else ""


def greeting_line(config: Config, paths: Paths, now: datetime | None = None) -> str:
    """One line in the user's language with the name from the profile; no banner."""
    hour = (now or datetime.now()).hour
    slot = "morning" if 5 <= hour < 12 else "day" if 12 <= hour < 18 else "evening"
    return t(f"cli.greeting.{slot}", config.general.language, name=_profile_name(paths, sep=" "))


def goodbye_line(config: Config, paths: Paths) -> str:
    """One short line in the user's language, with the name from the profile when there is one."""
    return t("cli.goodbye.text", config.general.language, name=_profile_name(paths))


def cmd_alarms(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    """`jarvis alarms [list|cancel <id|label|all>|stop|snooze [min]|test [--seconds N]]`."""
    from jarvis.scheduler import alarm as alarm_mod
    from jarvis.tools import alarms as alarm_tools

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    command = args.alarms_command or "list"
    if command == "list":
        out(alarm_tools.cli_list(paths))
        return 0
    if command == "cancel":
        out(alarm_tools.cli_cancel(config, paths, args.target))
        return 0
    if command == "stop":
        if alarm_mod.request_stop(paths, by="cli", wait_s=alarm_mod.ACK_WAIT_S):
            out(alarm_mod.STOPPED_TEXT)
            return 0
        out(alarm_mod.NOT_RINGING_TEXT if alarm_mod.ringing(paths) is None else t("cli.alarms.no_answer"))
        return 1
    if command == "snooze":
        minutes = args.minutes or config.alarm.snooze_min
        if alarm_mod.request_snooze(paths, by="cli", minutes=minutes, wait_s=alarm_mod.ACK_WAIT_S):
            out(alarm_mod.SNOOZED_TEXT.format(minutes=minutes, at=format_local(now_local() + timedelta(minutes=minutes), with_date=False)))
            return 0
        out(alarm_mod.NOT_RINGING_TEXT if alarm_mod.ringing(paths) is None else t("cli.alarms.no_answer"))
        return 1
    # test: ring here, in the foreground, without touching the alarms table
    from jarvis.scheduler.store import TimerRow

    if alarm_mod.ringing(paths) is not None:
        out(t("cli.alarms.already_ringing"))
        return 1
    sound = alarm_mod.resolve_sound(paths, config)
    out(t("cli.alarms.sound", sound=sound.describe(paths.sounds_dir)))
    out(
        t(
            "cli.alarms.test_info",
            volume=config.alarm.volume,
            seconds=args.seconds,
            stop=t("alarm.dialog.stop"),
            snooze=t("alarm.dialog.snooze", minutes=config.alarm.snooze_min),
        )
    )
    now = now_local()
    row = TimerRow(
        id=0,
        label=t("alarm.test_label"),
        fire_at=now,
        kind="alarm",
        pomodoro_state_json=None,
        status="fired",
        created_at=now.isoformat(),
        fired_at=None,
    )
    state = alarm_mod.AlarmState(hour=now.hour, minute=now.minute)
    ringer = alarm_mod.Ringer(paths, config, persist=False)
    try:
        result = ringer.ring(row, state, max_ring_s=max(3, args.seconds))
    except KeyboardInterrupt:
        out(t("cli.common.interrupted"))
        return 1
    out(t("cli.alarms.result", outcome=result.outcome, by=result.by, loops=result.loops, restored=_yes_no(result.volume_restored)))
    return 0


def cmd_services(args: argparse.Namespace, paths: Paths, console: Console) -> int:
    """`jarvis services reinstall|status`."""
    from jarvis import services

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    if args.services_command == "reinstall":
        services.reinstall_all(paths, out)
        return 0
    reports = services.service_reports()
    out(services.format_status(reports))
    return 0 if all(r.running or not r.installed for r in reports) and not any(r.stale for r in reports) else 1


def cmd_dashboard(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    """`jarvis dashboard [--no-open] [--port N]`: the local dashboard in the foreground."""
    from jarvis.dashboard.server import run_dashboard

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    try:
        return run_dashboard(config, paths, open_browser=not args.no_open, port=args.port, out=out)
    except KeyboardInterrupt:
        out(t("cli.dashboard.stopped"))
        return 0


def cmd_shortcuts(args: argparse.Namespace, config: Config, console: Console) -> int:
    """`jarvis shortcuts list`: every Shortcut on the Mac, the allowlisted ones marked."""
    from jarvis.tools import shortcuts as shortcuts_tools

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    for line in shortcuts_tools.overview_lines(config):
        out(line)
    return 0


def cmd_network(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    """`jarvis network test [--quick] | history [--range]`: the tools' output, in the terminal."""
    from jarvis.observability.db import Database
    from jarvis.tools import network as network_tools
    from jarvis.tools.base import Runtime, ToolContext

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    db = Database(paths.db_file)
    try:
        ctx = ToolContext.build(config, paths)
        ctx.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None)
        if args.network_command == "test":
            if not args.quick:
                out(t("cli.network.running"))
            tool = network_tools.InternetTest(ctx)
            result = tool.run(tool.parse_args({"full": not args.quick}))
        else:
            tool = network_tools.InternetHistory(ctx)
            result = tool.run(tool.parse_args({"range": args.range}))
    finally:
        db.close()
    out(result.content)
    return 1 if result.is_error else 0


def cmd_presence(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    """`jarvis presence setup | status | scan`."""
    from jarvis.observability.db import Database

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    if args.presence_command == "setup":
        from jarvis.presence.setup import run_setup

        return run_setup(config, paths, console) or 0
    from jarvis.scheduler.notify import notifier_for
    from jarvis.tools import presence as presence_tools

    db = Database(paths.db_file)
    try:
        if args.presence_command == "scan":
            from jarvis.presence.engine import scan_once

            events = scan_once(config, paths, db, notifier_for(config))
            out(
                t("presence.status.scan_done", count=len(events))
                + ("" if not events else ": " + ", ".join(e.kind + (f"({e.person})" if e.person else "") for e in events))
            )
        for line in presence_tools.status_lines(config, paths, db):
            out(line)
    finally:
        db.close()
    return 0


def cmd_location(args: argparse.Namespace, config: Config, paths: Paths, console: Console) -> int:
    """`jarvis location [show|refresh]`: the Current Location with its
    source and age; `refresh` forces a lookup and reports which source answered."""
    from jarvis.location import service as location_service

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    if args.location_command == "refresh":
        result = location_service.refresh(config, paths, force=True)
        if result.skipped:
            out(t("location.refresh.skipped", reason=result.skipped))
        elif result.refreshed and result.location is not None:
            tried = ", ".join(f"{k} ({v})" for k, v in result.source_errors.items())
            out(t("location.refresh.done", source=result.location.source) + (t("location.refresh.tried", tried=tried) if tried else ""))
            if not result.profile_updated:
                out(t("location.refresh.no_profile"))
        else:
            out(t("location.refresh.failed", error=result.error))
            if result.location is not None:
                out(t("location.refresh.kept", place=location_service.last_known_text(result.location, precision="street")))
    for line in location_service.status_lines(config, paths):
        out(line)
    return 0


def cmd_calendar(args: argparse.Namespace, config: Config, console: Console) -> int:
    """`jarvis calendar filters [--days N]`: every `[[calendar.filters]]` entry with the events it
    shows and hides in the coming days. Reading the calendars may ask for Calendar access."""
    from jarvis.tools.calendar_merged import filter_report
    from jarvis.utils.calendar_filters import describe

    out = lambda text: console.print(text, markup=False, highlight=False, soft_wrap=True)  # noqa: E731
    if args.calendar_command != "filters":
        return 1
    if not config.calendar.filters:
        out(t("cli.calendar.no_filters"))
        return 0
    days = max(1, min(366, args.days))
    report = filter_report(config, days=days, allow_prompt=True)
    for flt in report.filters:
        kept, hidden = report.kept.get(flt.calendar, []), report.hidden.get(flt.calendar, [])
        out(f"{describe(flt)}")
        out(t("cli.calendar.counts", days=days, shown=len(kept), hidden=len(hidden)))
        for event in kept:
            out(t("cli.calendar.shown", event=event.one_line()))
        for event in hidden:
            out(t("cli.calendar.hidden", event=event.one_line()))
    for name, reason in {**report.skipped, **report.failures}.items():
        out(t("cli.calendar.not_counted", name=name, reason=reason))
    return 0


def _subcommand_tree(parser: argparse.ArgumentParser) -> dict[str, list[str]]:
    """{"": top-level commands, "google": ["login", "logout"], ...} read from the parser, so the
    completion script can never drift from the real subcommands."""
    tree: dict[str, list[str]] = {}

    def names(p: argparse.ArgumentParser) -> tuple[list[str], dict[str, argparse.ArgumentParser]]:
        found: list[str] = []
        children: dict[str, argparse.ArgumentParser] = {}
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    found.append(name)
                    children[name] = child
            elif not action.option_strings and action.choices:
                found.extend(str(c) for c in action.choices)
        return found, children

    top, children = names(parser)
    tree[""] = top
    for name, child in children.items():
        sub, _ = names(child)
        if sub:
            tree[name] = sub
    return tree


def completion_script(parser: argparse.ArgumentParser, shell: str) -> str:
    """`jarvis completion zsh|bash`: subcommand completion generated from the parser."""
    tree = _subcommand_tree(parser)
    top = " ".join(tree[""] + ["--debug", "--version", "--help"])
    if shell == "zsh":
        cases = "\n".join(f"    {name}) _values 'subcommand' {' '.join(subs)} ;;" for name, subs in tree.items() if name)
        return (
            "#compdef jarvis\n"
            "# Generated by `jarvis completion zsh`. Install: jarvis completion zsh > ~/.zfunc/_jarvis\n"
            '# (with `fpath+=~/.zfunc` before `compinit` in ~/.zshrc), or eval "$(jarvis completion zsh)".\n'
            "_jarvis() {\n"
            "  local -a top\n"
            f"  top=({top})\n"
            "  if (( CURRENT == 2 )); then\n"
            "    _describe 'command' top\n"
            "    return\n"
            "  fi\n"
            '  case "${words[2]}" in\n'
            f"{cases}\n"
            "    *) _files ;;\n"
            "  esac\n"
            "}\n"
            "compdef _jarvis jarvis\n"
        )
    cases = "\n".join(f'    {name}) COMPREPLY=( $(compgen -W "{" ".join(subs)}" -- "$cur") ) ;;' for name, subs in tree.items() if name)
    return (
        '# Generated by `jarvis completion bash`. Install: eval "$(jarvis completion bash)" in ~/.bashrc\n'
        "_jarvis() {\n"
        '  local cur="${COMP_WORDS[COMP_CWORD]}"\n'
        '  if [ "$COMP_CWORD" -eq 1 ]; then\n'
        f'    COMPREPLY=( $(compgen -W "{top}" -- "$cur") )\n'
        "    return\n"
        "  fi\n"
        '  case "${COMP_WORDS[1]}" in\n'
        f"{cases}\n"
        "    *) COMPREPLY=() ;;\n"
        "  esac\n"
        "}\n"
        "complete -F _jarvis jarvis\n"
    )


if __name__ == "__main__":
    sys.exit(main())
