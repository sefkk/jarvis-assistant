"""The scheduler re-reads config.toml while it runs (scheduler/config_watch.py): a changed schedule moves the
next run without `jarvis services reinstall`, an invalid file is kept out and reported once, an unchanged
file is never re-parsed. No osascript, no Telegram."""

from __future__ import annotations

import os
import threading
from datetime import datetime

import pytest

from jarvis.config import load_config, update_local_config
from jarvis.scheduler import config_watch, service
from jarvis.scheduler.brief import next_run
from jarvis.scheduler.config_watch import ConfigWatch
from jarvis.scheduler.notify import Notifier
from jarvis.tools.base import Runtime, ToolContext
from jarvis.tools.brief import SetBriefSchedule
from jarvis.utils.timeutil import local_tz

TZ = local_tz()
MON = datetime(2026, 10, 19, 7, 0, tzinfo=TZ)


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.sent.append((title, message))


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def watch(config, paths, notifier):
    return ConfigWatch(config, paths, notifier=lambda cfg: notifier)


def bump(path, seconds: int) -> None:
    """Give the file a modification time of its own: edits inside one test can land in the same clock tick."""
    os.utime(path, (1_800_000_000 + seconds, 1_800_000_000 + seconds))


def test_an_unchanged_file_is_not_re_parsed(watch, config, monkeypatch):
    calls = {"n": 0}

    def counting(path):
        calls["n"] += 1
        return load_config(path)

    monkeypatch.setattr(config_watch, "load_config", counting)
    assert watch.current() is config and watch.current() is config
    assert calls["n"] == 0 and watch.reloads == 0


def test_a_hand_edit_of_config_toml_moves_the_next_run(watch, config, paths):
    assert next_run(config.brief.schedule, MON) == MON.replace(hour=9, minute=0)
    text = paths.config_file.read_text(encoding="utf-8").replace('weekday_time = "09:00"', 'weekday_time = "11:00"')
    assert text != paths.config_file.read_text(encoding="utf-8")
    paths.config_file.write_text(text, encoding="utf-8")
    bump(paths.config_file, 1)
    current = watch.current()
    assert current is not config and watch.reloads == 1
    assert current.brief.schedule.weekday_time == "11:00"
    assert next_run(current.brief.schedule, MON) == MON.replace(hour=11, minute=0)
    assert watch.current() is current  # unchanged since: the same object again


def test_a_change_made_through_jarvis_moves_the_next_run_too(watch, config, paths, db):
    ctx = ToolContext.build(config, paths)
    ctx.runtime = Runtime(client=None, db=db, usage_tracker=None, registry=None, budget=None)
    reply = SetBriefSchedule(ctx).run(SetBriefSchedule(ctx).parse_args({"weekday_time": "11:00"})).content
    assert "no restart needed" in reply and "reinstall" not in reply.lower()
    bump(paths.local_config_file, 2)
    current = watch.current()
    assert current.brief.schedule.weekday_time == "11:00"
    assert next_run(current.brief.schedule, MON) == MON.replace(hour=11, minute=0)


def broken_overlay(paths, body: str) -> None:
    """A hand edit that does not validate (`update_local_config` refuses to write one)."""
    paths.local_config_file.write_text(f"[brief.schedule]\n{body}\n", encoding="utf-8")


def test_an_invalid_config_is_kept_out_and_reported_once(watch, config, paths, notifier, caplog):
    broken_overlay(paths, 'weekday_time = "sabah"')
    bump(paths.local_config_file, 3)
    with caplog.at_level("ERROR", logger="jarvis.scheduler.config"):
        assert watch.current() is config  # the last valid config stays in force
        assert watch.current() is config
    assert watch.reloads == 0 and any("does not parse" in r.message for r in caplog.records)
    assert len(notifier.sent) == 1
    title, message = notifier.sent[0]
    assert title == "Jarvis ayarları" and message.startswith(
        "config.toml değişti ama geçerli değil; son geçerli ayarlarla devam ediyorum.\n"
    )
    assert (title, message.split("\n")[0]) == (config_watch.NOTIFICATION_TITLE, config_watch.INVALID_MESSAGE.split("\n")[0])
    assert "brief.schedule.weekday_time must be HH:MM" in message and "'sabah'" in message
    # saving the same broken file again: not reported a second time
    bump(paths.local_config_file, 4)
    assert watch.current() is config and len(notifier.sent) == 1
    # a different mistake is a new message
    broken_overlay(paths, 'late_until = "noon"')
    bump(paths.local_config_file, 5)
    assert watch.current() is config and len(notifier.sent) == 2 and "late_until" in notifier.sent[1][1]
    # fixed: reloaded, and the next mistake would be reported again
    update_local_config(paths.config_file, "brief.schedule", {"weekday_time": "11:00", "late_until": "12:00"})
    bump(paths.local_config_file, 6)
    assert watch.current().brief.schedule.weekday_time == "11:00" and watch.last_error is None and watch.reloads == 1


def test_the_invalid_config_notice_in_english(watch, config, paths, notifier):
    from jarvis import i18n

    i18n.set_language("en")
    broken_overlay(paths, 'weekday_time = "sabah"')
    bump(paths.local_config_file, 3)
    assert watch.current() is config
    title, message = notifier.sent[0]
    assert title == "Jarvis settings"
    assert message.startswith("config.toml changed but is not valid; I'm carrying on with the last valid settings.\n")


def test_a_broken_notifier_does_not_stop_the_watch(config, paths, caplog):
    def exploding(cfg):
        raise RuntimeError("no osascript here")

    watch = ConfigWatch(config, paths, notifier=exploding)
    broken_overlay(paths, 'weekday_time = "sabah"')
    bump(paths.local_config_file, 7)
    with caplog.at_level("ERROR", logger="jarvis.scheduler.config"):
        assert watch.current() is config
    assert any("could not report" in r.message for r in caplog.records)


def test_run_forever_hands_the_periodic_jobs_the_reloaded_config(config, paths, db, monkeypatch, logs):
    monkeypatch.setattr(service, "notifier_for", lambda cfg: RecordingNotifier())
    stop = threading.Event()
    seen: list[str] = []

    def job(cfg, p, database, notif):
        seen.append(cfg.brief.schedule.weekday_time)
        if len(seen) == 1:
            update_local_config(p.config_file, "brief.schedule", {"weekday_time": "11:00"})
            bump(p.local_config_file, 8)
        elif len(seen) >= 2:
            stop.set()

    monkeypatch.setattr(service, "PERIODIC_JOBS", [("probe", job)])
    service.run_forever(config, paths, interval_s=0.01, stop_event=stop)
    assert seen[:2] == ["09:00", "11:00"]
