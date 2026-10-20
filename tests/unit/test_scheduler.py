"""TimerStore CRUD, the fire_due engine (late timers, pomodoro state machine, idempotency,
failing notifier) and run_once. No osascript, no launchctl."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis import i18n
from jarvis.scheduler import engine, service
from jarvis.scheduler.engine import (
    NOTIFICATION_TITLE,
    PomodoroState,
    fire_due,
)
from jarvis.scheduler.notify import Notifier
from jarvis.scheduler.store import KIND_POMODORO, KIND_TIMER, TimerStore, iso_local
from jarvis.utils.timeutil import local_tz

TZ = local_tz()
T0 = datetime(2026, 10, 14, 9, 0, tzinfo=TZ)


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_on = fail_on or set()

    def notify(self, title: str, message: str) -> None:
        if any(word in message for word in self.fail_on):
            raise RuntimeError("osascript exploded")
        self.sent.append((title, message))


@pytest.fixture
def store(db) -> TimerStore:
    return TimerStore(db)


# --- store -----------------------------------------------------------------------------------


def test_store_add_get_pending_sorted(store):
    later = store.add("later", T0 + timedelta(minutes=30))
    sooner = store.add("sooner", T0 + timedelta(minutes=5))
    row = store.get(sooner)
    assert row is not None and row.label == "sooner" and row.status == "pending" and row.kind == KIND_TIMER
    assert row.fire_at == T0 + timedelta(minutes=5) and row.fire_at.tzinfo is not None
    assert [t.id for t in store.pending()] == [sooner, later]
    assert store.pending_count() == 2
    assert store.get(999) is None
    with pytest.raises(ValueError):
        store.add("x", T0, kind="bogus")


def test_store_stores_iso_with_offset(store, db):
    tid = store.add("iso", T0)
    raw = db.conn.execute("SELECT fire_at FROM timers WHERE id = ?", (tid,)).fetchone()["fire_at"]
    assert raw == iso_local(T0) and ("+" in raw or "-" in raw[10:])
    assert datetime.fromisoformat(raw) == T0


def test_store_due_mark_fired_and_cancel(store):
    a = store.add("a", T0 + timedelta(minutes=1))
    b = store.add("b", T0 + timedelta(minutes=10))
    assert [t.id for t in store.due(T0)] == []
    assert [t.id for t in store.due(T0 + timedelta(minutes=1))] == [a]
    assert [t.id for t in store.due(T0 + timedelta(hours=1))] == [a, b]
    assert store.mark_fired(a, T0 + timedelta(minutes=1)) is True
    assert store.mark_fired(a, T0 + timedelta(minutes=1)) is False  # only once
    fired = store.get(a)
    assert fired.status == "fired" and fired.fired_at == iso_local(T0 + timedelta(minutes=1))
    assert store.cancel(b) is True and store.cancel(b) is False and store.get(b).status == "cancelled"
    assert store.cancel(a) is False  # fired timers cannot be cancelled
    assert store.pending() == []


def test_store_cancel_by_label_and_pomodoro(store):
    store.add("Çamaşır", T0 + timedelta(minutes=5))
    store.add("çamaşır", T0 + timedelta(minutes=6))
    store.add("tea", T0 + timedelta(minutes=7))
    state = PomodoroState.start(work_min=25, break_min=5, long_break_min=15, cycles=4)
    p = store.add("Pomodoro", T0 + timedelta(minutes=25), KIND_POMODORO, state)
    assert store.active_pomodoro().id == p
    assert store.get(p).pomodoro_state_json == state.model_dump_json()
    assert store.cancel_by_label("çamaşır") == 2
    assert store.cancel_by_label("nothing") == 0
    assert store.cancel_pomodoro() == 1
    assert store.cancel_pomodoro() == 0
    assert store.active_pomodoro() is None
    assert [t.label for t in store.pending()] == ["tea"]


# --- pomodoro state ---------------------------------------------------------------------------


def test_pomodoro_state_machine_texts():
    i18n.set_language("tr")
    s = PomodoroState.start(work_min=25, break_min=5, long_break_min=15, cycles=2, label="tez")
    assert s.phase == "work" and s.cycle == 1 and s.duration_min() == 25
    assert s.notification_text() == engine.TEXT_BREAK
    b = s.next_phase()
    assert b.phase == "break" and b.cycle == 1 and b.duration_min() == 5
    assert b.notification_text() == "Odak zamanı, 2/2"
    w2 = b.next_phase()
    assert w2.phase == "work" and w2.cycle == 2 and w2.is_last_work()
    assert w2.notification_text() == engine.TEXT_LONG_BREAK
    lb = w2.next_phase()
    assert lb.phase == "long_break" and lb.duration_min() == 15 and lb.notification_text() == engine.TEXT_DONE
    assert lb.next_phase() is None
    assert PomodoroState.from_json(lb.model_dump_json()) == lb
    assert PomodoroState.from_json(None) is None and PomodoroState.from_json("{bad") is None
    assert "2 x 25 min" in s.plan() and s.describe() == "work 1/2" and lb.describe() == "long break 2/2"


def test_pomodoro_and_late_texts_in_english(store):
    i18n.set_language("en")
    s = PomodoroState.start(work_min=25, break_min=5, long_break_min=15, cycles=2)
    assert s.notification_text() == "Break time" and s.next_phase().notification_text() == "Focus time, 2/2"
    assert s.next_phase().next_phase().notification_text() == "Long break"
    store.add("tea", T0 - timedelta(minutes=10))
    notifier = RecordingNotifier()
    fire_due(store, notifier, now=T0, late_after_s=120)
    assert notifier.sent == [(NOTIFICATION_TITLE, "tea (late)")]


# --- fire_due ---------------------------------------------------------------------------------


def test_fire_due_only_due_timers_and_late_marking(store):
    on_time = store.add("tea", T0)
    late = store.add("laundry", T0 - timedelta(minutes=10))
    future = store.add("future", T0 + timedelta(minutes=1))
    notifier = RecordingNotifier()
    fired = fire_due(store, notifier, now=T0, late_after_s=120)
    assert [f.id for f in fired] == [late, on_time]
    assert notifier.sent == [(NOTIFICATION_TITLE, "laundry" + engine.LATE_SUFFIX), (NOTIFICATION_TITLE, "tea")]
    assert fired[0].late and not fired[1].late and all(f.notified for f in fired)
    assert store.get(future).status == "pending"
    assert store.get(on_time).status == "fired" and store.get(late).status == "fired"


def test_fire_due_is_idempotent(store):
    store.add("once", T0)
    notifier = RecordingNotifier()
    assert len(fire_due(store, notifier, now=T0)) == 1
    assert fire_due(store, notifier, now=T0) == []
    assert fire_due(store, notifier, now=T0 + timedelta(hours=1)) == []
    assert len(notifier.sent) == 1


def test_fire_due_skips_timers_another_process_took(store, monkeypatch):
    """Simulate a race: the row is no longer pending by the time we try to mark it."""
    tid = store.add("raced", T0)
    original = store.mark_fired

    def steal_then_mark(timer_id, fired_at=None):
        store.conn.execute("UPDATE timers SET status = 'fired' WHERE id = ?", (timer_id,))
        return original(timer_id, fired_at)

    monkeypatch.setattr(store, "mark_fired", steal_then_mark)
    notifier = RecordingNotifier()
    assert fire_due(store, notifier, now=T0) == []
    assert notifier.sent == [] and store.get(tid).status == "fired"


def test_fire_due_notifier_failure_does_not_stop_others(store, caplog):
    bad = store.add("bad", T0 - timedelta(seconds=2))
    good = store.add("good", T0 - timedelta(seconds=1))
    notifier = RecordingNotifier(fail_on={"bad"})
    with caplog.at_level("ERROR", logger="jarvis.scheduler"):
        fired = fire_due(store, notifier, now=T0)
    assert [(f.id, f.notified) for f in fired] == [(bad, False), (good, True)]
    assert fired[0].error and "RuntimeError" in fired[0].error
    assert store.get(bad).status == "fired"  # stays fired: no retry storm
    assert notifier.sent == [(NOTIFICATION_TITLE, "good")]
    assert any("notification failed" in r.message for r in caplog.records)


def test_pomodoro_full_run_with_fake_clock(store):
    i18n.set_language("tr")
    state = PomodoroState.start(work_min=25, break_min=5, long_break_min=15, cycles=4, label="tez")
    store.add("tez", T0 + timedelta(minutes=25), KIND_POMODORO, state)
    notifier = RecordingNotifier()
    clock = T0
    messages: list[str] = []
    phases: list[str] = []
    for _ in range(20):  # more than enough iterations; the loop ends when nothing is pending
        active = store.active_pomodoro()
        if active is None:
            break
        phases.append(PomodoroState.from_json(active.pomodoro_state_json).describe())
        clock = active.fire_at  # jump to the moment the phase ends
        fired = fire_due(store, notifier, now=clock)
        assert len(fired) == 1 and fired[0].kind == KIND_POMODORO and not fired[0].late
        messages.append(fired[0].message)
    assert phases == ["work 1/4", "break 1/4", "work 2/4", "break 2/4", "work 3/4", "break 3/4", "work 4/4", "long break 4/4"]
    assert messages == [
        "Mola zamanı (tez)",
        "Odak zamanı, 2/4 (tez)",
        "Mola zamanı (tez)",
        "Odak zamanı, 3/4 (tez)",
        "Mola zamanı (tez)",
        "Odak zamanı, 4/4 (tez)",
        "Uzun mola (tez)",
        "Pomodoro tamamlandı (tez)",
    ]
    # 4 x 25 + 3 x 5 + 15 = 130 minutes of wall-clock time, measured from each phase's actual end
    assert clock == T0 + timedelta(minutes=130)
    assert store.active_pomodoro() is None and store.pending() == []
    assert [m for _, m in notifier.sent] == messages


def test_pomodoro_phase_after_sleep_is_measured_from_now(store):
    """The Mac slept through a work phase: the break starts now, not cascading from fire_at."""
    state = PomodoroState.start(work_min=25, break_min=5, long_break_min=15, cycles=4)
    store.add("Pomodoro", T0, KIND_POMODORO, state)
    notifier = RecordingNotifier()
    woke = T0 + timedelta(hours=2)
    fired = fire_due(store, notifier, now=woke)
    assert fired[0].late and notifier.sent == [(NOTIFICATION_TITLE, engine.TEXT_BREAK + engine.LATE_SUFFIX)]
    nxt = store.get(fired[0].next_id)
    assert nxt.kind == KIND_POMODORO and nxt.fire_at == woke + timedelta(minutes=5)
    assert PomodoroState.from_json(nxt.pomodoro_state_json).phase == "break"
    assert fire_due(store, notifier, now=woke) == []  # the break is not due yet


def test_pomodoro_with_corrupt_state_still_fires_label(store):
    store.add("Pomodoro", T0, KIND_POMODORO, "{not json")
    notifier = RecordingNotifier()
    fired = fire_due(store, notifier, now=T0)
    assert fired[0].message == "Pomodoro" and fired[0].next_id is None and store.pending() == []


# --- service ----------------------------------------------------------------------------------


def test_run_once_opens_its_own_db_and_fires(config, paths, db, monkeypatch):
    store = TimerStore(db)
    store.add("due", datetime.now(TZ) - timedelta(minutes=1))
    store.add("later", datetime.now(TZ) + timedelta(hours=1))
    notifier = RecordingNotifier()
    monkeypatch.setattr(service, "notifier_for", lambda cfg: notifier)
    assert service.run_once(config, paths) == 1
    assert notifier.sent == [(NOTIFICATION_TITLE, "due")]
    assert service.run_once(config, paths) == 0
    assert store.get(1).status == "fired" and store.get(2).status == "pending"


def test_run_forever_survives_a_failing_tick(config, paths, db, monkeypatch, logs, caplog):
    import threading

    TimerStore(db).add("due", datetime.now(TZ) - timedelta(minutes=1))
    notifier = RecordingNotifier()
    monkeypatch.setattr(service, "notifier_for", lambda cfg: notifier)
    stop = threading.Event()
    ticks = {"n": 0}
    real_fire_due = service.fire_due

    def flaky(store, notif, now=None, late_after_s=120, **kwargs):  # kwargs: the Mac-power sleep notifier
        ticks["n"] += 1
        if ticks["n"] == 1:
            raise RuntimeError("database is locked")
        result = real_fire_due(store, notif, now, late_after_s, **kwargs)
        if ticks["n"] >= 3:
            stop.set()
        return result

    monkeypatch.setattr(service, "fire_due", flaky)
    with caplog.at_level("ERROR", logger="jarvis.scheduler"):
        service.run_forever(config, paths, interval_s=0.01, stop_event=stop)
    assert ticks["n"] == 3 and notifier.sent == [(NOTIFICATION_TITLE, "due")]
    assert any("tick failed" in r.message for r in caplog.records)


def test_engine_default_now_is_local(store, monkeypatch):
    """Without an explicit clock the engine uses the local time (and never a naive one)."""
    store.add("now", datetime.now(TZ) - timedelta(seconds=1))
    notifier = RecordingNotifier()
    assert len(engine.fire_due(store, notifier)) == 1 and notifier.sent[0][1] == "now"


# --- periodic jobs ride on the scheduler loop ----------------------------------------------


def test_run_forever_runs_the_periodic_jobs_and_isolates_failures(config, paths, db, monkeypatch, logs, caplog):
    import threading

    TimerStore(db).add("due", datetime.now(TZ) - timedelta(minutes=1))
    notifier = RecordingNotifier()
    monkeypatch.setattr(service, "notifier_for", lambda cfg: notifier)
    stop = threading.Event()
    seen: list[tuple[str, object]] = []

    def broken(cfg, p, database, notif):
        raise RuntimeError("presence exploded")

    def fine(cfg, p, database, notif):
        seen.append(("fine", notif))
        if len(seen) >= 2:
            stop.set()

    monkeypatch.setattr(service, "PERIODIC_JOBS", [("broken", broken), ("fine", fine)])
    with caplog.at_level("ERROR", logger="jarvis.scheduler"):
        service.run_forever(config, paths, interval_s=0.01, stop_event=stop)
    assert notifier.sent == [(NOTIFICATION_TITLE, "due")]  # the timer still fired
    assert len(seen) >= 2 and all(n is notifier for _, n in seen)  # the second job ran after the first failed
    assert any("periodic job broken failed" in r.message for r in caplog.records)


def test_default_periodic_jobs_are_presence_network_alarm_keep_awake_and_brief():
    assert [name for name, _ in service.PERIODIC_JOBS] == ["presence", "network", "alarm_keep_awake", "brief"]
