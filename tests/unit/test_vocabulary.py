"""Speech vocabulary (voice/vocabulary.py): the user's file, collected terms, filters and limits."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from jarvis.observability.db import Database
from jarvis.utils.calendar_types import CalendarEvent
from jarvis.voice import vocabulary as vocab


def test_clean_term_filters_secrets_emails_urls_and_numbers():
    assert vocab.clean_term("  Sezen Aksu ") == "Sezen Aksu"
    assert vocab.clean_term("- Kadıköy,") == "Kadıköy"
    for bad in (
        "ali@example.com",
        "https://x.y",
        "www.x.com",
        "sk_live_abcdef",
        "AIzaSyD-whatever-it-is",
        "abcdefghijklmnopqrstuvwxyz012",
        "05551234567",
        "12 34 56",
        "a",
        "x" * 51,
        "one two three four five six",
        "home",
        "Ev",
        None,
        42,
    ):
        assert vocab.clean_term(bad) is None, bad


def test_user_file_is_created_and_read(paths):
    vocab.ensure_user_file(paths.vocabulary_file)
    assert paths.vocabulary_file.read_text(encoding="utf-8").startswith("# Speech vocabulary")
    with paths.vocabulary_file.open("a", encoding="utf-8") as fh:
        fh.write("- Sezen Aksu\nMor ve Ötesi  # band\n# a comment\n\nsezen aksu\n")
    assert vocab.read_user_terms(paths.vocabulary_file) == ["Sezen Aksu", "Mor ve Ötesi"]
    vocab.ensure_user_file(paths.vocabulary_file)  # never overwritten
    assert "Mor ve Ötesi" in paths.vocabulary_file.read_text(encoding="utf-8")


def test_profile_terms_name_and_people_sections(tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text(
        "# Profile\n\n- Name: Deniz\n- Job: tester\n\n## People\n\n- Ayşe (sister): likes tea\n- Mert - friend\n"
        "\n## Preferences\n\n- Coffee: black\n",
        encoding="utf-8",
    )
    assert vocab.profile_terms(profile) == ["Deniz", "Ayşe", "Mert"]
    assert vocab.profile_terms(tmp_path / "missing.md") == []


def test_config_terms(config):
    config.sheets.aliases = {"antrenman": "a" * 20}
    terms = vocab.config_terms(config)
    assert terms[:2] == ["Fenerbahçe", "Jarvis"] and "antrenman" in terms
    assert all(entry.name in terms for entry in config.shortcuts.allowed)


def _add_call(db, tool, args, when=None):
    db.conn.execute("PRAGMA foreign_keys = OFF")
    db.conn.execute(
        "INSERT INTO tool_calls (turn_id, tool, args_json, risk, decision, duration_ms, error, is_delegate, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("t", tool, json.dumps(args), "safe", "ran", 1.0, None, 0, (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.000Z")),
    )
    db.conn.commit()


def test_history_terms_from_spotify_maps_and_events_only(paths):
    db = Database(paths.db_file)
    _add_call(db, "search_and_play", {"query": "Sezen Aksu Gülümse", "kind": "track"})
    _add_call(db, "search_and_play", {"query": "Sezen Aksu Gülümse"})
    _add_call(db, "travel_time", {"destination": "Bağdat Cd. No:12, Kadıköy/İstanbul", "origin": "home"})
    _add_call(db, "apple_create_event", {"title": "Secret meeting", "location": "Moda Sahnesi"})
    _add_call(db, "gmail_create_draft", {"to": "x@y.com", "subject": "Private Topic"})
    _add_call(db, "search_places", {"query": "Old Place"}, when=datetime.now(timezone.utc) - timedelta(days=400))
    terms = vocab.history_terms(db)
    assert terms[0] == "Sezen Aksu Gülümse"
    assert {"Bağdat Cd", "Kadıköy", "İstanbul", "Moda Sahnesi"} <= set(terms)
    assert not {"Secret meeting", "Private Topic", "Old Place", "home"} & set(terms)
    assert vocab.history_terms(None) == []


def test_calendar_terms_use_locations_only(config):
    now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    events = [
        SimpleNamespace(title="Private title", location="Zorlu PSM, Beşiktaş", notes="secret"),
        SimpleNamespace(title="x", location=""),
    ]
    seen = {}

    def fetch(cfg, start, end):
        seen["range"] = (start, end)
        return events, {}

    assert vocab.calendar_terms(config, now=now, fetch=fetch) == ["Zorlu PSM", "Beşiktaş"]
    assert seen["range"] == (now - timedelta(days=14), now + timedelta(days=60))
    assert vocab.calendar_terms(config, fetch=lambda *a: (_ for _ in ()).throw(RuntimeError("no access"))) == []


def test_build_merges_orders_dedupes_and_writes_the_auto_file(config, paths):
    paths.vocabulary_file.parent.mkdir(parents=True, exist_ok=True)
    paths.vocabulary_file.write_text("- Mor ve Ötesi\n- Kadıköy\n", encoding="utf-8")
    paths.profile_file.write_text("# Profile\n\n- Name: Deniz\n", encoding="utf-8")
    db = Database(paths.db_file)
    _add_call(db, "search_places", {"query": "kadıköy"})
    built = vocab.build(
        config, paths, db=db, calendar_fetch=lambda *a: ([SimpleNamespace(location="Moda")], {}), corrections=["Sezen Aksu"]
    )
    assert built.user == ["Mor ve Ötesi", "Kadıköy"]
    assert built.auto[:2] == ["Sezen Aksu", "Deniz"] and "kadıköy" not in [t for t in built.auto]  # the user's spelling wins
    assert "Moda" in built.auto and "Fenerbahçe" in built.auto
    assert built.terms[0] == "Mor ve Ötesi"
    auto = paths.vocabulary_auto_file.read_text(encoding="utf-8")
    assert auto.startswith("# Jarvis'in topladığı konuşma sözlüğü") and "- Sezen Aksu" in auto  # the test config is Turkish
    quick = vocab.build(config, paths, db=db, calendar=False, write=False)
    assert "Moda" not in quick.terms


def test_calendar_default_never_asks_for_the_apple_permission(config, monkeypatch):
    from jarvis.tools import calendar_merged as cm

    called = []
    monkeypatch.setattr(
        cm,
        "_fetchers",
        lambda: {
            "apple": lambda *a: called.append("apple") or [],
            "google": lambda *a: (
                called.append("google")
                or [
                    CalendarEvent(
                        source="google",
                        calendar="c",
                        title="private",
                        location="Moda",
                        start=datetime(2026, 10, 20, 9, tzinfo=timezone.utc),
                        end=datetime(2026, 10, 20, 10, tzinfo=timezone.utc),
                    )
                ]
            ),
        },
    )
    monkeypatch.setattr(cm, "_apple_not_ready", lambda: "not granted")
    assert vocab.calendar_terms(config) == ["Moda"] and called == ["google"]


def test_build_survives_a_broken_source(config, paths, monkeypatch):
    monkeypatch.setattr(vocab, "profile_terms", lambda p: (_ for _ in ()).throw(OSError("unreadable")))
    built = vocab.build(config, paths, calendar=False)
    assert "Fenerbahçe" in built.terms


def test_engine_forms_and_limits():
    v = vocab.Vocabulary(user=[f"Name{i}" for i in range(30)], auto=["Extra"])
    assert len(v.for_apple()) == vocab.APPLE_LIMIT and v.for_apple()[0] == "Name0"
    assert len(v.for_keyterms()) == 31
    prompt = v.whisper_prompt()
    assert prompt.startswith("Name0, Name1") and prompt.endswith(".") and len(prompt) <= vocab.WHISPER_PROMPT_CHARS + 1
    long = vocab.Vocabulary(user=[f"Uzun Bir İsim {i:03d}x" for i in range(100)])
    assert len(long.whisper_prompt()) <= vocab.WHISPER_PROMPT_CHARS + 1
    assert vocab.Vocabulary().whisper_prompt() is None


def test_cache_rebuilds_in_the_background_when_stale_or_the_file_changes(tmp_path):
    watch = tmp_path / "vocabulary.md"
    watch.write_text("a", encoding="utf-8")
    now = [0.0]
    builds = []
    done = threading.Event()

    def builder():
        builds.append(len(builds))
        done.set()
        return vocab.Vocabulary(user=[f"v{len(builds)}"])

    cache = vocab.VocabularyCache(builder, refresh_s=100, watch=watch, clock=lambda: now[0])
    assert cache.get().user == ["v1"] and len(builds) == 1  # the first build is synchronous
    assert cache.get().user == ["v1"] and len(builds) == 1  # fresh: no rebuild
    now[0] = 150.0
    done.clear()
    assert cache.get().user == ["v1"]  # stale: the old list is returned at once, the rebuild runs behind
    assert done.wait(2) and cache.get().user == ["v2"]
    cache.invalidate()
    done.clear()
    cache.get()
    assert done.wait(2) and len(builds) == 3


def test_seed_terms_follow_the_configured_team(config, paths):
    from jarvis import i18n

    config.football.team = "Arsenal"
    assert vocab.config_terms(config)[:2] == ["Arsenal", "Jarvis"]
    config.football.team = ""
    assert vocab.config_terms(config)[0] == "Jarvis"
    i18n.set_language("en")
    vocab.ensure_user_file(paths.vocabulary_file)
    assert paths.vocabulary_file.read_text(encoding="utf-8").startswith("# Speech vocabulary")
