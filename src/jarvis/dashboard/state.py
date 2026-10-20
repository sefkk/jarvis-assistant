"""What the Overview shows and the immediate actions it runs. One `Dashboard` object lives
for the server's lifetime: it caches the health checks (quick ones every refresh, the slow integration
checks on demand in a thread), remembers which agent is restarting and the outcome of the last quick
actions. Everything here reuses the doctor, the services module, the voice pause file and the brief
scheduler; nothing is reimplemented."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from jarvis import services
from jarvis.dashboard import queries
from jarvis.dashboard.ui_text import UI
from jarvis.exceptions import JarvisError
from jarvis.observability import health
from jarvis.observability.usage import start_of_local_day_utc
from jarvis.utils.timeutil import now_iso, now_local
from jarvis.voice import pause as voice_pause

if TYPE_CHECKING:
    from jarvis.config import Config, Paths

log = logging.getLogger("jarvis.dashboard")

SERVICE_LABELS = {"scheduler": UI.SERVICE_SCHEDULER, "telegram": UI.SERVICE_TELEGRAM, "voice": UI.SERVICE_VOICE}
SOURCE_LABELS = {
    "shortcut": UI.LOCATION_SOURCE_SHORTCUT,
    "corelocation": UI.LOCATION_SOURCE_CORELOCATION,
    "ip": UI.LOCATION_SOURCE_IP,
    "telegram": UI.LOCATION_SOURCE_TELEGRAM,
}


def _maps_url(app: str, coordinates: str) -> str:
    """The Apple Maps / Google Maps link for a `lat,lon` pair (the same builder the maps tool uses)."""
    from jarvis.tools.maps import build_maps_url  # noqa: PLC0415

    return build_maps_url(app, query=coordinates)


SERVICE_NAMES = tuple(SERVICE_LABELS)
PAUSE_PRESETS = {"15": 15, "60": 60}
SERVICE_CACHE_S = 2.0  # the strip and the Services card poll at the same moment: one launchctl round for both


# --- views ------------------------------------------------------------------------------------------------


@dataclass
class HealthItem:
    name: str
    state: str  # UI.HEALTH_OK | HEALTH_WARNING | HEALTH_FAILED | HEALTH_CHECKING
    detail: str


@dataclass
class HealthView:
    items: list[HealthItem]
    checking: bool
    last_check: datetime | None
    failed: int
    warnings: int


@dataclass
class ServiceView:
    name: str
    label: str
    state: str  # UI.STATE_*
    detail: str
    installed: bool
    pid: int | None = None
    uptime_s: float | None = None
    config_version: str | None = None
    config_note: str = ""
    config_current: bool | None = None


@dataclass
class VoiceView:
    state: str  # UI.VOICE_*
    paused: bool
    off: bool
    until: datetime | None
    detail: str


@dataclass
class LocationView:
    enabled: bool
    label: str = ""  # readable address at location.profile_precision
    source: str = ""
    source_label: str = ""
    approximate: bool = False
    updated_at: datetime | None = None
    age_text: str = ""
    stale: bool = False
    coordinates: str = ""
    maps_url: str = ""
    home: str = ""
    telegram_label: str = ""
    telegram_coordinates: str = ""
    telegram_updated_at: datetime | None = None
    telegram_age_text: str = ""
    telegram_maps_url: str = ""
    strip_text: str = ""  # the short place name for the status strip
    error: str = ""  # the last refresh error (from the state file), shown muted

    @property
    def has_location(self) -> bool:
        return bool(self.coordinates)


@dataclass
class ActionResult:
    name: str
    state: str  # "running" | "done" | "failed"
    detail: str
    at: str

    @property
    def label(self) -> str:
        return {"running": UI.ACTION_RUNNING, "done": UI.ACTION_DONE, "failed": UI.ACTION_FAILED}[self.state]


@dataclass
class StripView:
    services: list[ServiceView]
    voice: VoiceView
    spent_usd: float
    budget_usd: float
    location: "LocationView | None" = None


# --- health cache -----------------------------------------------------------------------------------------


class HealthCache:
    """Quick checks (`health.run_checks`, cheap) re-run at most every `refresh_s`; the integration checks
    (`health.integration_checks`, EventKit, Keychain, network) run in a background thread, once at start
    and on "Run full check"."""

    def __init__(
        self,
        config: "Config",
        paths: "Paths",
        *,
        refresh_s: float,
        quick_runner: Callable[..., list[health.Check]] | None = None,
        full_runner: Callable[..., list[health.Check]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.paths = paths
        self.refresh_s = refresh_s
        self.clock = clock
        self._quick_runner = quick_runner or (lambda c, p: health.run_checks(c, p, full=False))
        self._full_runner = full_runner or health.integration_checks
        self._lock = threading.Lock()
        self._quick: list[health.Check] = []
        self._quick_at: float | None = None
        self._full: list[health.Check] | None = None
        self._full_at: datetime | None = None
        self._thread: threading.Thread | None = None

    @property
    def checking(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_full(self) -> bool:
        """Start the integration checks in a thread; False when a run is already in progress."""
        with self._lock:
            if self.checking:
                return False
            self._thread = threading.Thread(target=self._run_full, name="dashboard-health", daemon=True)
            self._thread.start()
            return True

    def _run_full(self) -> None:
        try:
            result = self._full_runner(self.config, self.paths)
        except Exception as exc:  # the runner never raises by contract; keep the dashboard alive anyway
            log.exception("full health check failed")
            result = [health.Check(UI.HEALTH_INTEGRATIONS, False, f"check failed: {type(exc).__name__}: {exc}")]
        with self._lock:
            self._full = result
            self._full_at = now_local()

    def snapshot(self) -> HealthView:
        now = self.clock()
        if self._quick_at is None or now - self._quick_at >= self.refresh_s:
            try:
                self._quick = self._quick_runner(self.config, self.paths)
            except Exception as exc:
                log.exception("quick health check failed")
                self._quick = [health.Check("health check", False, f"check failed: {type(exc).__name__}: {exc}")]
            self._quick_at = now
        with self._lock:
            full = list(self._full) if self._full is not None else None
            full_at = self._full_at
        items = [_item(c) for c in self._quick]
        if full is not None:
            items.extend(_item(c) for c in full)
        elif self.checking:
            items.append(HealthItem(UI.HEALTH_INTEGRATIONS, UI.HEALTH_CHECKING, ""))
        return HealthView(
            items=items,
            checking=self.checking,
            last_check=full_at or now_local(),
            failed=sum(1 for i in items if i.state == UI.HEALTH_FAILED),
            warnings=sum(1 for i in items if i.state == UI.HEALTH_WARNING),
        )


def _item(check: health.Check) -> HealthItem:
    if not check.ok:
        state = UI.HEALTH_FAILED
    elif check.warn:
        state = UI.HEALTH_WARNING
    else:
        state = UI.HEALTH_OK
    return HealthItem(check.name, state, check.detail)


# --- the dashboard ---------------------------------------------------------------------------------------


class Dashboard:
    def __init__(
        self,
        config: "Config",
        paths: "Paths",
        *,
        auto_full_check: bool = True,
        clock: Callable[[], float] = time.monotonic,
        health_cache: HealthCache | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.clock = clock
        self.started_at = now_local()
        self.health = health_cache or HealthCache(config, paths, refresh_s=config.dashboard.doctor_refresh_s, clock=clock)
        self.restarting: set[str] = set()
        self.actions: dict[str, ActionResult] = {}
        self._reports: list[services.ServiceReport] = []
        self._reports_at: float | None = None
        self._lock = threading.Lock()
        self._migrate_database()
        if auto_full_check:
            self.health.run_full()

    def _migrate_database(self) -> None:
        """The dashboard reads the database read-only; opening it once through `Database` brings an older
        file up to the current schema (search index, summaries) before the first page is served."""
        try:
            from jarvis.observability.db import Database  # noqa: PLC0415

            Database(self.paths.db_file).close()
        except Exception:
            log.warning("could not migrate the database for the dashboard", exc_info=True)

    # --- reads ---

    def _db(self) -> sqlite3.Connection | None:
        return queries.connect_readonly(self.paths.db_file)

    def _service_reports(self) -> list[services.ServiceReport]:
        now = self.clock()
        if self._reports_at is None or now - self._reports_at >= SERVICE_CACHE_S:
            self._reports = services.service_reports()
            self._reports_at = now
        return self._reports

    def services_view(self) -> list[ServiceView]:
        current_version = services.config_version(self.paths)
        conn = self._db()
        views: list[ServiceView] = []
        try:
            for report in self._service_reports():
                views.append(self._service_view(report, current_version, conn))
        finally:
            if conn is not None:
                conn.close()
        return views

    def _service_view(self, report: services.ServiceReport, current_version: str, conn) -> ServiceView:
        label = SERVICE_LABELS.get(report.name, report.name.capitalize())
        if report.name in self.restarting:
            state = UI.STATE_RESTARTING
        elif report.detail.startswith("status unknown") or (report.running and report.stale):
            state = UI.STATE_ERROR
        elif report.running:
            state = UI.STATE_RUNNING
        else:
            state = UI.STATE_STOPPED
        detail = report.detail if report.installed else UI.NOT_INSTALLED
        view = ServiceView(report.name, label, state, detail, report.installed)
        if not report.running:
            return view
        pid = _pid_from_detail(report.detail)
        view.pid = pid
        started = services.process_started_at(pid) if pid else None
        if started is not None:
            view.uptime_s = max(0.0, (now_local() - started).total_seconds())
        record = services.recorded_start(conn, report.name) if conn is not None else None
        version = record.get("config_version") if record else None
        if version and str(record.get("pid")) == str(pid):
            view.config_version = str(version)
            view.config_current = version == current_version
            view.config_note = UI.CONFIG_CURRENT if view.config_current else UI.CONFIG_OUTDATED
        else:
            view.config_note = UI.CONFIG_UNKNOWN
        return view

    def voice_view(self) -> VoiceView:
        state = voice_pause.load_state(self.paths.voice_pause_file)
        running = any(r.name == "voice" and r.running for r in self._service_reports())
        if not self.config.general.use_voice or not running:
            return VoiceView(UI.VOICE_OFF, paused=state.paused, off=True, until=None, detail=voice_pause.describe(state))
        if state.paused:
            until = state.until_dt.astimezone() if state.until_dt else None
            label = UI.VOICE_PAUSED_UNTIL.format(time=until.strftime("%H:%M")) if until else UI.VOICE_PAUSED
            return VoiceView(label, paused=True, off=False, until=until, detail=voice_pause.describe(state))
        return VoiceView(UI.VOICE_LISTENING, paused=False, off=False, until=None, detail=voice_pause.describe(state))

    def location_view(self, *, now: datetime | None = None) -> LocationView:
        """The Mac's Current Location, the home line of profile.md and the latest Telegram-shared location,
        all from the location module's own state file; nothing here is logged."""
        from jarvis.location import service as location_service  # noqa: PLC0415
        from jarvis.location.store import load_state  # noqa: PLC0415

        now_utc = (now or now_local()).astimezone(timezone.utc)
        cfg = self.config.location
        state = load_state(self.paths.location_state_file)
        view = LocationView(enabled=cfg.enabled, home=location_service.home_location(self.paths) or "")
        if state.last_error:
            view.error = str(state.last_error)[:200]
        mac = state.mac
        if mac is not None:
            age = mac.age_seconds(now_utc)
            view.label = mac.label(cfg.profile_precision) or UI.LOCATION_ADDRESS_UNKNOWN
            view.source = mac.source
            view.source_label = SOURCE_LABELS.get(mac.source, mac.source)
            view.approximate = bool(mac.approximate)
            view.updated_at = mac.updated_datetime().astimezone()
            view.age_text = location_service.age_text(age, "en")
            view.stale = age > cfg.max_age_min * 60
            view.coordinates = mac.coordinates_text(5)
            view.maps_url = _maps_url(self.config.maps.open_with, view.coordinates)
            view.strip_text = mac.neighborhood or mac.place or UI.LOCATION_ADDRESS_UNKNOWN
        telegram = state.telegram
        if telegram is not None:
            view.telegram_label = telegram.label(cfg.profile_precision) or UI.LOCATION_ADDRESS_UNKNOWN
            view.telegram_coordinates = telegram.coordinates_text(5)
            view.telegram_updated_at = telegram.updated_datetime().astimezone()
            view.telegram_age_text = location_service.age_text(telegram.age_seconds(now_utc), "en")
            view.telegram_maps_url = _maps_url(self.config.maps.open_with, view.telegram_coordinates)
        return view

    def refresh_location(self) -> str | None:
        """Run the existing refresh now (Shortcut, CoreLocationCLI, IP). Returns the error text, or None."""
        from jarvis.location import service as location_service  # noqa: PLC0415

        if not self.config.location.enabled:
            return UI.LOCATION_DISABLED
        try:
            result = location_service.refresh(self.config, self.paths, force=True)
        except Exception as exc:  # the service never raises by contract; keep the card alive anyway
            log.warning("location refresh crashed: %s", type(exc).__name__)
            return UI.LOCATION_FAILED
        if result.refreshed or result.skipped:
            return None
        return UI.LOCATION_FAILED

    def today(self) -> queries.TodayGlance:
        conn = self._db()
        try:
            glance = queries.today_glance(conn, start_of_local_day_utc())
            glance.budget_usd = self.config.budget.daily_budget_usd
            return glance
        finally:
            if conn is not None:
                conn.close()

    def strip(self) -> StripView:
        conn = self._db()
        try:
            spent = queries.spent_since(conn, start_of_local_day_utc())
        finally:
            if conn is not None:
                conn.close()
        return StripView(self.services_view(), self.voice_view(), spent, self.config.budget.daily_budget_usd, self.location_view())

    def any_action_running(self) -> bool:
        return any(a.state == "running" for a in self.actions.values())

    # --- immediate actions (these are never staged) ---

    def pause_voice(self, minutes: int | None) -> str:
        state = voice_pause.pause(self.paths.voice_pause_file, by="dashboard", minutes=minutes)
        if minutes:
            return UI.PAUSED_FOR.format(duration=format_duration(minutes * 60)) + (
                f" {UI.VOICE_PAUSED_UNTIL.format(time=state.until_dt.astimezone().strftime('%H:%M'))}" if state.until_dt else ""
            )
        return UI.PAUSED_UNTIL_RESUMED

    def resume_voice(self) -> str:
        return UI.RESUMED if voice_pause.resume(self.paths.voice_pause_file) else UI.WAS_NOT_PAUSED

    def restart_service(self, name: str) -> ActionResult:
        if name not in SERVICE_NAMES:
            raise JarvisError(f"unknown service {name!r}")
        key = f"restart:{name}"

        def work() -> str:
            self.restarting.add(name)
            try:
                services.restart_agent(name, self.paths)
            finally:
                self.restarting.discard(name)
                self._reports_at = None
            return UI.RESTARTED.format(service=SERVICE_LABELS[name])

        return self._start_action(key, work)

    def send_brief(self) -> ActionResult:
        def work() -> str:
            from jarvis.scheduler import brief  # noqa: PLC0415 - heavy imports only when asked

            channels, _reason = brief.choose_channels(self.config.brief.schedule, brief.Probes.for_paths(self.paths))
            brief.deliver(self.config, self.paths, channels)
            return UI.SENT_VIA.format(channel=", ".join(channels))

        return self._start_action("brief", work)

    def send_test_message(self) -> ActionResult:
        def work() -> str:
            from jarvis.interfaces.telegram_notify import TelegramNotifier  # noqa: PLC0415

            notifier = TelegramNotifier(self.config, state_file=self.paths.telegram_state_file)
            blocked = notifier.blocked_reason()
            if blocked:
                raise JarvisError(f"not sent: {blocked}")
            notifier.notify("Jarvis Dashboard", "Test message: the Telegram connection works.")
            return UI.TEST_MESSAGE_SENT

        return self._start_action("telegram_test", work)

    def _start_action(self, key: str, work: Callable[[], str]) -> ActionResult:
        with self._lock:
            current = self.actions.get(key)
            if current is not None and current.state == "running":
                return current
            result = ActionResult(key, "running", "", now_iso())
            self.actions[key] = result

        def run() -> None:
            try:
                detail = work()
                outcome = ActionResult(key, "done", detail, now_iso())
            except Exception as exc:  # the user sees the reason in the card; the log has the trace
                log.warning("dashboard action %s failed: %s", key, exc, exc_info=not isinstance(exc, JarvisError))
                outcome = ActionResult(key, "failed", str(exc), now_iso())
            with self._lock:
                self.actions[key] = outcome

        threading.Thread(target=run, name=f"dashboard-{key}", daemon=True).start()
        return result


def _pid_from_detail(detail: str) -> int | None:
    """`running (pid 48213); runs /path` -> 48213 (the launchd status modules put the pid in the detail)."""
    marker = "(pid "
    start = detail.find(marker)
    if start < 0:
        return None
    end = detail.find(")", start)
    try:
        return int(detail[start + len(marker) : end])
    except ValueError:
        return None


def format_duration(seconds: float | None) -> str:
    """`2 d 3 h`, `1 h 05 min`, `12 min`, `45 s` for uptimes and pauses."""
    if seconds is None:
        return ""
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min"
    return f"{secs} s"


def format_money(value: float | None) -> str:
    if value is None:
        return "–"
    return f"${value:.2f}" if value >= 1 else f"${value:.3f}"


def format_ms(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value / 1000:.1f} s" if value >= 1000 else f"{int(value)} ms"


def parse_pause_minutes(preset: str | None, custom: str | None) -> int | None:
    """The Pause menu: a preset ("15", "60"), "custom" with a number of minutes, or nothing (until resumed)."""
    if preset in PAUSE_PRESETS:
        return PAUSE_PRESETS[preset]
    if preset == "custom":
        minutes = voice_pause.parse_duration((custom or "").strip())
        if minutes is None:
            raise JarvisError("enter a number of minutes between 1 and 1440")
        return minutes
    return None
