"""Timer and pomodoro tools against a real temporary SQLite db; launchd and the clock are faked."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis.exceptions import ToolExecutionError, ToolValidationError
from jarvis.observability.health import Check
from jarvis.scheduler import launchd
from jarvis.scheduler.engine import PomodoroState
from jarvis.scheduler.launchd import SchedulerStatus
from jarvis.scheduler.store import KIND_POMODORO, TimerStore
from jarvis.tools import timers
from jarvis.tools.base import RiskLevel, Runtime, ToolContext
from jarvis.tools.timers import (
    CancelTimer,
    ListTimers,
    SetTimer,
    StartPomodoro,
    StopPomodoro,
    health_checks,
    resolve_at,
)
from jarvis.utils.timeutil import local_tz
from tests.conftest import RecordingSink

TZ = local_tz()
NOW = datetime(2026, 10, 14, 15, 30, tzinfo=TZ)
INSTALLED = SchedulerStatus(installed=True, running=True, pid=1, detail="running (pid 1)")
NOT_INSTALLED = SchedulerStatus(installed=False, running=False, detail="not installed")


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(timers, "_now", lambda: NOW)
    return NOW


@pytest.fixture
def scheduler(monkeypatch):
    """launchd.status() is patched on the module the tools call; tests flip `.result`."""
    holder = {"result": INSTALLED, "calls": 0}

    def fake_status():
        holder["calls"] += 1
        return holder["result"]

    monkeypatch.setattr(launchd, "status", fake_status)
    return holder


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def ctx(config, paths, db, sink) -> ToolContext:
    context = ToolContext.build(config, paths)
    context.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None, sink=sink)
    return context


def run(tool_cls, ctx, **args):
    tool = tool_cls(ctx)
    return tool.run(tool.parse_args(args))


# --- metadata ----------------------------------------------------------------------------------


def test_tool_metadata():
    for cls in (SetTimer, ListTimers, CancelTimer, StartPomodoro, StopPomodoro):
        assert cls.risk is RiskLevel.SAFE and cls.taints is False and cls.delegate_ok is None
    names = {cls.name for cls in (SetTimer, ListTimers, CancelTimer, StartPomodoro, StopPomodoro)}
    assert names == {"set_timer", "list_timers", "cancel_timer", "start_pomodoro", "stop_pomodoro"}
    schema = SetTimer().api_schema()["input_schema"]
    assert schema["required"] == ["label"] and "YYYY-MM-DD 09:00" in schema["properties"]["at"]["description"]


# --- set_timer ---------------------------------------------------------------------------------


def test_set_timer_with_minutes(ctx, db, frozen, scheduler, sink):
    result = run(SetTimer, ctx, minutes=25, label="Çay")
    assert not result.is_error
    assert "Timer #1" in result.content and "15:55" in result.content and "in 25min" in result.content and "Çay" in result.content
    assert timers.SCHEDULER_WARNING not in result.content and sink.notices == []
    row = TimerStore(db).get(1)
    assert row.fire_at == NOW + timedelta(minutes=25) and row.kind == "timer" and row.status == "pending"


def test_set_timer_with_clock_time_today(ctx, db, frozen, scheduler):
    result = run(SetTimer, ctx, at="18:00", label="Çamaşır")
    assert "2026-10-14 18:00" in result.content and "in 2h 30min" in result.content
    assert TimerStore(db).get(1).fire_at == NOW.replace(hour=18, minute=0)


def test_set_timer_past_clock_time_rolls_to_tomorrow(ctx, db, frozen, scheduler):
    result = run(SetTimer, ctx, at="09:00", label="Sabah")
    assert "2026-10-15 09:00" in result.content
    assert TimerStore(db).get(1).fire_at == datetime(2026, 10, 15, 9, 0, tzinfo=TZ)
    # the same minute as now counts as past
    run(SetTimer, ctx, at="15:30", label="Şimdi")
    assert TimerStore(db).get(2).fire_at == datetime(2026, 10, 15, 15, 30, tzinfo=TZ)


def test_set_timer_with_absolute_and_iso_times(ctx, db, frozen, scheduler):
    run(SetTimer, ctx, at="2026-10-15 09:00", label="model-resolved yarın 09:00")
    assert TimerStore(db).get(1).fire_at == datetime(2026, 10, 15, 9, 0, tzinfo=TZ)
    run(SetTimer, ctx, at="2026-10-14T20:15:00+00:00", label="iso")
    assert TimerStore(db).get(2).fire_at.timestamp() == datetime.fromisoformat("2026-10-14T20:15:00+00:00").timestamp()


def test_set_timer_validation(ctx, frozen, scheduler):
    with pytest.raises(ToolValidationError, match="exactly one"):
        run(SetTimer, ctx, label="x")
    with pytest.raises(ToolValidationError, match="exactly one"):
        run(SetTimer, ctx, minutes=5, at="18:00", label="x")
    with pytest.raises(ToolValidationError, match="minutes"):
        run(SetTimer, ctx, minutes=0, label="x")
    with pytest.raises(ToolValidationError, match="minutes"):
        run(SetTimer, ctx, minutes=5000, label="x")
    with pytest.raises(ToolValidationError, match="label"):
        run(SetTimer, ctx, minutes=5, label="")
    with pytest.raises(ToolValidationError, match="past"):
        run(SetTimer, ctx, at="2026-10-14 10:00", label="x")
    with pytest.raises(ToolValidationError, match="cannot parse"):
        run(SetTimer, ctx, at="yarın 09:00", label="x")
    with pytest.raises(ToolValidationError, match="invalid clock"):
        run(SetTimer, ctx, at="25:99", label="x")
    with pytest.raises(ToolValidationError, match="year"):
        run(SetTimer, ctx, at="2030-01-01 10:00", label="x")


def test_resolve_at_accepts_dot_separator():
    assert resolve_at("18.30", NOW) == NOW.replace(hour=18, minute=30)


def test_set_timer_warns_when_scheduler_not_installed(ctx, db, frozen, scheduler, sink):
    scheduler["result"] = NOT_INSTALLED
    result = run(SetTimer, ctx, minutes=10, label="Tea")
    assert not result.is_error and "Timer #1" in result.content
    assert "jarvis scheduler install" in result.content
    assert sink.notices == [timers.SCHEDULER_WARNING]
    assert TimerStore(db).get(1).status == "pending"  # still saved


def test_set_timer_without_sink_still_works(config, paths, db, frozen, scheduler):
    scheduler["result"] = NOT_INSTALLED
    context = ToolContext.build(config, paths)  # no runtime at all
    tool = SetTimer(context, db=db)
    result = tool.run(tool.parse_args({"minutes": 1, "label": "x"}))
    assert "jarvis scheduler install" in result.content and TimerStore(db).pending_count() == 1


def test_tool_opens_its_own_db_without_runtime(config, paths, frozen, scheduler):
    context = ToolContext.build(config, paths)
    tool = SetTimer(context)
    tool.run(tool.parse_args({"minutes": 1, "label": "x"}))
    assert tool._own_db is not None and TimerStore(tool._own_db).pending_count() == 1
    tool._own_db.close()


# --- list / cancel --------------------------------------------------------------------------------


def test_list_and_cancel_by_id_and_label(ctx, db, frozen, scheduler):
    assert run(ListTimers, ctx).content == "No pending timers."
    run(SetTimer, ctx, minutes=30, label="Tea")
    run(SetTimer, ctx, minutes=5, label="Laundry")
    run(SetTimer, ctx, minutes=90, label="Laundry")
    listing = run(ListTimers, ctx).content
    assert listing.startswith("3 pending timer(s):")
    lines = listing.splitlines()[1:]
    assert lines[0].startswith("#2") and "in 5min" in lines[0] and lines[1].startswith("#1") and lines[2].startswith("#3")

    result = run(CancelTimer, ctx, id=1)
    assert "Cancelled timer #1 (Tea" in result.content
    assert "already cancelled" in run(CancelTimer, ctx, id=1).content
    with pytest.raises(ToolValidationError, match="no timer with id 99"):
        run(CancelTimer, ctx, id=99)

    assert "Cancelled 2 timer(s)" in run(CancelTimer, ctx, label="laundry").content
    assert "No pending timer labelled" in run(CancelTimer, ctx, label="laundry").content
    assert run(ListTimers, ctx).content == "No pending timers."
    store = TimerStore(db)
    assert {store.get(i).status for i in (1, 2, 3)} == {"cancelled"}


def test_cancel_by_partial_label(ctx, frozen, scheduler):
    run(SetTimer, ctx, minutes=30, label="Çamaşırı makineden al")
    assert "Cancelled 1 timer(s)" in run(CancelTimer, ctx, label="çamaşır").content


def test_cancel_validation(ctx):
    with pytest.raises(ToolValidationError, match="exactly one"):
        run(CancelTimer, ctx)
    with pytest.raises(ToolValidationError, match="exactly one"):
        run(CancelTimer, ctx, id=1, label="x")


# --- pomodoro -----------------------------------------------------------------------------------


def test_start_and_stop_pomodoro_with_defaults(ctx, db, frozen, scheduler, config, sink):
    result = run(StartPomodoro, ctx)
    assert "Pomodoro started" in result.content and "4 x 25 min work with 5 min breaks" in result.content
    assert "15 min long break" in result.content and "Focus 1/4 runs until 15:55" in result.content
    assert timers.SCHEDULER_WARNING not in result.content and sink.notices == []
    store = TimerStore(db)
    active = store.active_pomodoro()
    assert active.kind == KIND_POMODORO and active.label == "Pomodoro" and active.fire_at == NOW + timedelta(minutes=25)
    state = PomodoroState.from_json(active.pomodoro_state_json)
    assert (state.work_min, state.break_min, state.long_break_min, state.cycles, state.phase, state.cycle) == (
        config.timers.pomodoro_work_min,
        config.timers.pomodoro_break_min,
        config.timers.pomodoro_long_break_min,
        config.timers.pomodoro_cycles,
        "work",
        1,
    )
    listing = run(ListTimers, ctx).content
    assert "pomodoro work 1/4: Pomodoro" in listing

    with pytest.raises(ToolExecutionError, match="already running"):
        run(StartPomodoro, ctx, work_min=50)
    assert run(StopPomodoro, ctx).content == "Pomodoro stopped (was in work 1/4)."
    assert run(StopPomodoro, ctx).content == "No pomodoro is running."
    assert store.active_pomodoro() is None and store.get(active.id).status == "cancelled"


def test_start_pomodoro_custom_and_label(ctx, db, frozen, scheduler):
    result = run(StartPomodoro, ctx, work_min=50, break_min=10, long_break_min=30, cycles=2, label="tez")
    assert "for 'tez'" in result.content and "2 x 50 min work with 10 min breaks" in result.content
    active = TimerStore(db).active_pomodoro()
    assert active.label == "tez" and active.fire_at == NOW + timedelta(minutes=50)
    assert PomodoroState.from_json(active.pomodoro_state_json).label == "tez"
    with pytest.raises(ToolValidationError):
        run(StartPomodoro, ctx, cycles=0)


def test_scheduler_warning_follows_the_main_language(ctx, frozen, scheduler, sink):
    from jarvis import i18n

    scheduler["result"] = NOT_INSTALLED
    assert run(SetTimer, ctx, minutes=5, label="Çay").content.endswith(
        "Uyarı: arka plan zamanlayıcısı kurulu değil, bu yüzden bu zamanlayıcı kendiliğinden çalmaz. "
        "Bir kez `jarvis scheduler install` çalıştır (yeniden başlatmadan sonra da çalışır)."
    )
    i18n.set_language("en")
    run(SetTimer, ctx, minutes=5, label="Tea")
    assert sink.notices[-1] == (
        "Warning: the background scheduler is not installed, so this timer will not fire on its own. "
        "Run `jarvis scheduler install` once (it survives reboots)."
    )


def test_start_pomodoro_warns_when_scheduler_missing(ctx, frozen, scheduler, sink):
    scheduler["result"] = NOT_INSTALLED
    result = run(StartPomodoro, ctx)
    assert "jarvis scheduler install" in result.content and sink.notices == [timers.SCHEDULER_WARNING]


def test_stop_pomodoro_leaves_plain_timers(ctx, db, frozen, scheduler):
    run(SetTimer, ctx, minutes=5, label="Tea")
    run(StartPomodoro, ctx)
    run(StopPomodoro, ctx)
    assert [t.label for t in TimerStore(db).pending()] == ["Tea"]


# --- doctor -------------------------------------------------------------------------------------


def test_health_checks(config, paths, db, scheduler, frozen):
    TimerStore(db).add("Tea", NOW + timedelta(minutes=5))
    checks = health_checks(config, paths)
    assert all(isinstance(c, Check) for c in checks)
    by_name = {c.name: c for c in checks}
    assert by_name["Timer scheduler (launchd)"].ok and "pid 1" in by_name["Timer scheduler (launchd)"].detail
    assert by_name["Timers"].ok and by_name["Timers"].detail.startswith("1 pending, next 2026-10-14 15:35 (Tea)")

    scheduler["result"] = NOT_INSTALLED
    checks = {c.name: c for c in health_checks(config, paths)}
    # not installed is a warning (timers are optional), not a failure of `jarvis doctor`
    assert checks["Timer scheduler (launchd)"].warn and "jarvis scheduler install" in checks["Timer scheduler (launchd)"].detail

    scheduler["result"] = SchedulerStatus(installed=True, running=False, detail="plist present but the agent is not loaded")
    checks = {c.name: c for c in health_checks(config, paths)}
    assert not checks["Timer scheduler (launchd)"].ok and "not loaded" in checks["Timer scheduler (launchd)"].detail


def test_health_checks_never_raise(config, paths, scheduler, monkeypatch):
    monkeypatch.setattr(timers, "Database", lambda path: (_ for _ in ()).throw(RuntimeError("disk gone")))
    checks = {c.name: c for c in health_checks(config, paths)}
    assert not checks["Timers"].ok and "disk gone" in checks["Timers"].detail
