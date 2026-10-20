"""The dashboard's Settings form model, the cart (staging and un-staging, validation errors, conflict
detection), the tomlkit writer (comments survive), the apply job (atomic write, only the affected services restart,
waiting for idle, rollback on failure) and the History restore."""

from __future__ import annotations

import json
import threading

import pytest

from jarvis.config import Paths, load_config
from jarvis.dashboard import cart as cart_mod
from jarvis.dashboard import settings as settings_mod
from jarvis.dashboard import tomlwrite
from jarvis.dashboard.cart import (
    AREA_MEMORY,
    AREA_PERSONA,
    AREA_PROFILE,
    AREA_SETTINGS,
    FILE_CONFIG,
    FILE_LOCAL,
    FILE_MEMORY,
    FILE_PERSONA,
    FILE_PROFILE,
    Cart,
    Probes,
)
from jarvis.dashboard.markdown import render as render_markdown
from jarvis.dashboard.ui_text import UI
from jarvis.services import ServiceReport

PROFILE = (
    "# Profile\n\n- Name: Test\n\n<!-- jarvis:auto current_location START -->\n- Current Location: Somewhere\n"
    "<!-- jarvis:auto current_location END -->\n\n## Preferences\nshort answers\n"
)


@pytest.fixture
def home(paths: Paths, monkeypatch) -> Paths:
    """A home with memory, profile and a persona file of its own."""
    persona = paths.home / "persona-copy.md"
    persona.write_text("# Persona\n\nBe kind.\n", encoding="utf-8")
    monkeypatch.setattr(Paths, "persona_file", property(lambda self: persona))
    paths.memory_file.write_text("- 2026-10-01: Likes tea\n- 2026-10-02: Gym on Tuesdays\nhand written line\n", encoding="utf-8")
    paths.profile_file.write_text(PROFILE, encoding="utf-8")
    return paths


@pytest.fixture
def cart(config, home) -> Cart:
    return Cart(config, home)


def fake_probes(*, running=("voice", "telegram"), busy=None, restart_log=None, fail_restart=False, clock=None, health=None):
    state = {"busy": set(busy or ()), "t": 0.0}
    restarted = restart_log if restart_log is not None else []

    def restart(name):
        if fail_restart:
            raise RuntimeError("launchctl exploded")
        restarted.append(name)

    def tick():
        return state["t"]

    def sleep(seconds):
        state["t"] += seconds

    probes = Probes(
        turn_in_progress=lambda channel: channel in state["busy"],
        alarm_ringing=lambda: False,
        service_reports=lambda: [
            ServiceReport(n, True, n in running, None, False, "running (pid 1)") for n in ("scheduler", "telegram", "voice")
        ],
        restart=restart,
        health=health or (lambda: []),
        sleep=sleep,
        clock=clock or tick,
    )
    probes.state = state  # type: ignore[attr-defined]
    probes.restarted = restarted  # type: ignore[attr-defined]
    return probes


def wait_job(job, timeout=10.0):
    job._thread.join(timeout)
    assert job.finished, [(s.key, s.state, s.message) for s in job.steps]
    return job


# --- settings form ------------------------------------------------------------------------------------------------


def test_sections_follow_the_fixed_order_and_mark_read_only_fields(config):
    sections = settings_mod.build_sections(config)
    keys = [s.key for s in sections]
    assert keys[:4] == ["models", "persona", "voice", "voice.sounds"] and "security" in keys and keys.index("security") == len(keys) - 1
    fields = {f.path: f for s in sections for f in s.fields}
    assert fields["persona.humor_level"].widget == "slider" and fields["persona.humor_level"].maximum == 3
    assert fields["general.use_voice"].widget == "switch" and fields["models.main_effort"].widget == "select"
    assert fields["models.main_effort"].choices == ["low", "medium", "high", "xhigh", "max"]
    assert fields["brief.schedule.weekday_time"].widget == "time" and fields["budget.daily_budget_usd"].widget == "number"
    assert fields["voice.confirm_phrases"].widget == "list" and fields["timers.notify_channels"].choices == ["mac", "telegram"]
    assert fields["voice.stt_language"].widget == "select" and fields["voice.stt_language"].choices == ["tr", "en", "auto"]
    assert fields["voice.stt_language"].restarts == ("voice",) and fields["voice.stt_model_by_language"].read_only
    assert (
        fields["security.shell_denylist"].read_only and fields["telegram.allowed_chat_id"].read_only and fields["budget.prices"].read_only
    )
    assert fields["presence.people"].read_only and fields["calendar.filters"].read_only
    assert fields["persona.humor_level"].label == "Humor level" and fields["voice.vad_silence_ms"].label == "VAD silence ms"
    assert settings_mod.restarts_for("voice.tts_rate") == ("voice",) and settings_mod.restarts_for("telegram.enabled") == ("telegram",)
    assert settings_mod.restarts_for("brief.schedule.weekday_time") == () and settings_mod.restarts_for("models.main_model") == (
        "voice",
        "telegram",
    )
    assert settings_mod.effect_text(()) == UI.APPLIES_IMMEDIATELY and settings_mod.effect_text(("voice",)) == "Restarts: voice service"


def test_coerce_each_widget(config):
    spec = settings_mod.field_spec
    assert (
        settings_mod.coerce(spec(config, "general.use_voice"), "on") is True
        and settings_mod.coerce(spec(config, "general.use_voice"), None) is False
    )
    assert settings_mod.coerce(spec(config, "persona.humor_level"), "3") == 3
    with pytest.raises(ValueError, match="at most 3"):
        settings_mod.coerce(spec(config, "persona.humor_level"), "4")
    with pytest.raises(ValueError, match="whole number"):
        settings_mod.coerce(spec(config, "persona.humor_level"), "two")
    assert settings_mod.coerce(spec(config, "budget.daily_budget_usd"), "1,5") == 1.5
    with pytest.raises(ValueError, match="at least"):
        settings_mod.coerce(spec(config, "budget.daily_budget_usd"), "-1")
    with pytest.raises(ValueError, match="choose one of"):
        settings_mod.coerce(spec(config, "models.main_effort"), "ultra")
    assert settings_mod.coerce(spec(config, "voice.confirm_phrases"), "evet, tamam ,onayla") == ["evet", "tamam", "onayla"]
    with pytest.raises(ValueError, match="allowed values"):
        settings_mod.coerce(spec(config, "timers.notify_channels"), "mac, sms")
    assert settings_mod.coerce(spec(config, "brief.schedule.weekday_time"), "7:05") == "7:05"
    with pytest.raises(ValueError, match="HH:MM"):
        settings_mod.coerce(spec(config, "brief.schedule.weekday_time"), "noon")


def test_secret_status_never_carries_the_value(monkeypatch):
    from jarvis.utils import secrets

    monkeypatch.setattr(secrets, "get_secret", lambda name: "sk-SECRET" if name == "anthropic_api_key" else None)
    rows = settings_mod.secret_status()
    assert {r["name"]: r["status"] for r in rows}["anthropic_api_key"] == UI.IN_KEYCHAIN
    assert {r["name"]: r["status"] for r in rows}["telegram_bot_token"] == UI.MISSING
    assert "sk-SECRET" not in json.dumps(rows)


def test_tomlkit_writer_keeps_comments_and_creates_tables():
    text = '[models]\n# above\nmain_model = "a"  # inline\n\n[brief.schedule]\nweekday_time = "09:00"  # when\n'
    out = tomlwrite.render_with(
        text, {"models.main_model": "b", "brief.schedule.weekday_time": "08:30", "persona.humor_level": 3, "loop.x": 2.0}
    )
    assert '# above\nmain_model = "b"  # inline' in out and 'weekday_time = "08:30"  # when' in out
    assert "[persona]\nhumor_level = 3" in out and "x = 2.0" in out
    assert tomlwrite.render_with("[a]\nn = 1\n", {"a.n": 2.0}) == "[a]\nn = 2\n"  # a float that is whole stays an int key


def test_markdown_preview_is_sanitized():
    html = str(render_markdown("# Hi <b>x</b>\n\n**bold** `c` *i*\n\n- a\n- <script>alert(1)</script>\n\n```\n<raw>\n```\n"))
    assert (
        "<h1>Hi &lt;b&gt;x&lt;/b&gt;</h1>" in html and "<strong>bold</strong>" in html and "<code>c</code>" in html and "<em>i</em>" in html
    )
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<pre><code>&lt;raw&gt;</code></pre>" in html
    assert str(render_markdown("")) == ""


# --- staging -------------------------------------------------------------------------------------------------------


def test_stage_setting_unstages_when_back_to_the_current_value(cart):
    item, error = cart.stage_setting("persona.humor_level", "3")
    assert error is None and item.new == 3 and item.old == 2 and item.file == FILE_CONFIG and item.area == AREA_SETTINGS
    assert item.restarts == ("voice", "telegram") and item.effect == "Restarts: voice service, telegram service"
    assert cart.count() == 1 and cart.staged_settings() == {"persona.humor_level": 3}
    item2, _ = cart.stage_setting("persona.humor_level", "1")
    assert item2.id == item.id and item2.new == 1 and cart.count() == 1  # replaced in place
    none, _ = cart.stage_setting("persona.humor_level", "2")
    assert none is None and cart.count() == 0


def test_stage_setting_rejects_read_only_and_bad_values(cart):
    assert cart.stage_setting("security.shell_timeout_seconds", "5") == (None, UI.READ_ONLY)
    assert cart.stage_setting("telegram.allowed_chat_id", "1") == (None, UI.READ_ONLY)
    item, error = cart.stage_setting("persona.humor_level", "9")
    assert item is None and "at most" in error
    assert cart.stage_setting("nope.nothing", "1") == (None, "unknown setting")
    assert cart.count() == 0


def test_setting_goes_to_the_overlay_when_it_is_defined_there(cart, home):
    home.local_config_file.write_text("[persona]\nhumor_level = 1\n", encoding="utf-8")
    cart.config = load_config(home.config_file)
    item, _ = cart.stage_setting("persona.humor_level", "3")
    assert item.file == FILE_LOCAL and item.old == 1
    item, _ = cart.stage_setting("models.main_effort", "low")
    assert item.file == FILE_CONFIG
    texts = cart.render_config_texts(cart.items())
    assert "humor_level = 3" in texts[FILE_LOCAL] and 'main_effort = "low"' in texts[FILE_CONFIG]


def test_memory_staging(cart):
    item, _ = cart.stage_memory_edit("- 2026-10-02: Gym on Tuesdays", "Gym on <b>Wednesdays</b>")
    assert item.kind == "memory_edit" and item.new == "- 2026-10-02: Gym on ‹b›Wednesdays‹/b›" and item.area == AREA_MEMORY
    deleted, _ = cart.stage_memory_delete("hand written line")
    assert deleted.kind == "memory_delete" and deleted.new == ""
    added, _ = cart.stage_memory_add("Drinks coffee at 3")
    assert added.kind == "memory_add" and added.new.endswith(": Drinks coffee at 3")
    assert cart.stage_memory_add("   ") == (None, UI.MEMORY_EMPTY_TEXT)
    assert cart.stage_memory_edit("not a line", "x") == (None, UI.CONFLICT_TEXT)
    assert cart.stage_memory_delete("- 2026-10-02: Gym on Tuesdays")[0].kind == "memory_delete"  # replaces the edit of the same line
    assert cart.count() == 3
    text = cart.render_texts(cart.items())[FILE_MEMORY]
    assert text == "- 2026-10-01: Likes tea\n" + added.new + "\n"
    assert item.diff()[0][0] == "del" and item.diff()[1][0] == "add"


def test_text_staging_keeps_the_profile_auto_block(cart, home):
    item, _ = cart.stage_text(AREA_PROFILE, "# Profile\n\n- Name: New\n\n## Preferences\nlong answers\n")
    assert item.file == FILE_PROFILE and "Current Location" not in item.old and item.restarts == ()
    rendered = cart.render_texts(cart.items())[FILE_PROFILE]
    assert "- Name: New" in rendered and "- Current Location: Somewhere" in rendered and rendered.count("jarvis:auto") == 2
    assert cart.stage_text(AREA_PERSONA, "   ") == (None, UI.PERSONA_EMPTY)
    persona, _ = cart.stage_text(AREA_PERSONA, "# Persona\n\nBe brief.\n")
    assert persona.restarts == ("voice", "telegram") and persona.file == FILE_PERSONA
    none, _ = cart.stage_text(AREA_PERSONA, "# Persona\n\nBe kind.\n")  # back to the file's text
    assert none is None and cart.by_key("text:persona") is None


# --- validation and conflicts ----------------------------------------------------------------------------------------


def test_check_marks_invalid_config_and_conflicts(cart, home):
    cart.stage_setting("models.main_effort", "max")  # main_thinking is off: invalid combination
    cart.stage_setting("persona.humor_level", "3")
    cart.stage_memory_delete("hand written line")
    cart.stage_text(AREA_PERSONA, "# Persona\n\nNew.\n")
    items, general = cart.check()
    by_key = {i.key: i for i in items}
    assert "main_thinking = false requires" in (by_key["setting:models.main_effort"].error or "") or any(
        "main_thinking" in g for g in general
    )
    assert by_key["setting:persona.humor_level"].error is None
    # the files change under the cart: every touched item is a conflict
    text = home.config_file.read_text(encoding="utf-8").replace("humor_level = 2", "humor_level = 1")
    home.config_file.write_text(text, encoding="utf-8")
    home.memory_file.write_text("- 2026-10-01: Likes tea\n", encoding="utf-8")
    home.persona_file.write_text("# Persona\n\nChanged by hand.\n", encoding="utf-8")
    items, _ = cart.check()
    by_key = {i.key: i for i in items}
    assert by_key["setting:persona.humor_level"].conflict and by_key["setting:persona.humor_level"].error == UI.CONFLICT_TEXT
    assert by_key["memory:" + cart_mod._hash("hand written line")].conflict
    assert by_key["text:persona"].conflict and not by_key["setting:models.main_effort"].conflict


# --- applying -----------------------------------------------------------------------------------------------------------


def test_apply_writes_everything_restarts_only_affected_services_and_records_a_backup(cart, home):
    cart.stage_setting("persona.humor_level", "3")
    cart.stage_setting("brief.schedule.weekday_time", "08:30")
    cart.stage_memory_add("Drinks coffee")
    cart.stage_text(AREA_PROFILE, "# Profile\n\n- Name: New\n\n## Preferences\nlong answers\n")
    probes = fake_probes()
    job = wait_job(cart.start_apply(probes=probes))
    assert job.success and job.result_text == UI.SAVE_OK and [s.state for s in job.steps] == ["done"] * 7
    assert probes.restarted == ["voice", "telegram"]  # the brief and profile changes alone would restart nothing
    config = load_config(home.config_file)
    assert config.persona.humor_level == 3 and config.brief.schedule.weekday_time == "08:30"
    assert "# How much Jarvis jokes" in home.config_file.read_text(encoding="utf-8")  # comments survive
    assert home.memory_file.read_text(encoding="utf-8").endswith(": Drinks coffee\n")
    profile = home.profile_file.read_text(encoding="utf-8")
    assert "- Name: New" in profile and "Current Location: Somewhere" in profile
    assert cart.count() == 0
    backups = cart.backups()
    assert len(backups) == 1 and set(backups[0]["files"]) == {FILE_CONFIG, FILE_MEMORY, FILE_PROFILE} and len(backups[0]["items"]) == 4
    assert (home.backups_dir / backups[0]["id"] / FILE_CONFIG).read_text(encoding="utf-8").count("humor_level = 2") == 1


def test_apply_only_restarts_running_services_and_skips_when_none_needed(cart, home):
    cart.stage_setting("voice.tts_rate", "200")
    probes = fake_probes(running=("telegram",))  # voice is installed but stopped
    job = wait_job(cart.start_apply(probes=probes))
    assert job.success and probes.restarted == [] and "not running" in job.step("restart").message
    cart.stage_setting("brief.schedule.weekend_time", "11:00")
    job = wait_job(cart.start_apply(probes=fake_probes()))
    assert job.success and job.step("restart").message == UI.NO_SERVICE_RESTART


def test_apply_waits_for_a_busy_service_then_asks_and_forces(cart, home):
    cart.stage_setting("voice.tts_rate", "200")
    probes = fake_probes(busy={"voice"})
    job = cart.start_apply(probes=probes)
    deadline = threading.Event()
    for _ in range(200):
        if job.waiting_for == "voice":
            break
        deadline.wait(0.02)
    assert job.waiting_for == "voice" and UI.RESTART_WAIT.format(service="voice service") == job.step("restart").message
    assert probes.state["t"] >= cart_mod.IDLE_WAIT_S and probes.restarted == []
    job.keep_waiting()
    for _ in range(200):
        if job.waiting_for == "voice" and probes.state["t"] >= 2 * cart_mod.IDLE_WAIT_S:
            break
        deadline.wait(0.02)
    assert job.waiting_for == "voice"
    job.force_restart()
    wait_job(job)
    assert job.success and probes.restarted == ["voice"]


def test_apply_rolls_back_when_the_restart_fails(cart, home):
    original = home.config_file.read_text(encoding="utf-8")
    cart.stage_setting("persona.humor_level", "3")
    cart.stage_memory_add("Drinks coffee")
    job = wait_job(cart.start_apply(probes=fake_probes(fail_restart=True)))
    assert not job.success and job.failed_step == UI.STEP_RESTARTING
    assert job.result_text == UI.SAVE_FAILED.format(step=UI.STEP_RESTARTING)
    assert home.config_file.read_text(encoding="utf-8") == original and "Drinks coffee" not in home.memory_file.read_text(encoding="utf-8")
    assert cart.count() == 2  # the changes stay staged for another try
    assert [s.state for s in job.steps] == ["done", "done", "done", "done", "failed", "skipped", "skipped"]


def test_apply_rolls_back_when_health_fails_after_the_restart(cart, home):
    from jarvis.observability.health import Check

    original = home.config_file.read_text(encoding="utf-8")
    cart.stage_setting("persona.humor_level", "3")
    probes = fake_probes(health=lambda: [Check("Anthropic API key", False, "missing", fatal=True)])
    job = wait_job(cart.start_apply(probes=probes))
    assert not job.success and job.failed_step == UI.STEP_HEALTH
    assert home.config_file.read_text(encoding="utf-8") == original
    assert probes.restarted == ["voice", "telegram", "voice", "telegram"]  # restarted again after the rollback


def test_apply_stops_on_conflicts_and_validation_errors_without_writing(cart, home):
    cart.stage_setting("models.main_effort", "max")
    original = home.config_file.read_text(encoding="utf-8")
    job = wait_job(cart.start_apply(probes=fake_probes()))
    assert not job.success and job.failed_step == UI.STEP_VALIDATING and home.config_file.read_text(encoding="utf-8") == original
    assert cart.backups() == []
    cart.clear()
    cart.stage_setting("persona.humor_level", "3")
    home.config_file.write_text(original.replace("humor_level = 2", "humor_level = 0"), encoding="utf-8")
    job = wait_job(cart.start_apply(probes=fake_probes()))
    assert not job.success and job.failed_step == UI.STEP_CONFLICTS and job.result_text.startswith(UI.STEP_CONFLICTS)
    cart.clear()
    job = wait_job(cart.start_apply(probes=fake_probes()))
    assert not job.success and UI.NO_CHANGES in job.result_text


def test_apply_rolls_back_when_a_write_fails(cart, home, monkeypatch):
    cart.stage_setting("persona.humor_level", "3")
    cart.stage_memory_add("Drinks coffee")
    original = home.config_file.read_text(encoding="utf-8")
    import os

    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(cart_mod.os, "replace", flaky)
    job = wait_job(cart.start_apply(probes=fake_probes()))
    assert not job.success and job.failed_step == UI.STEP_WRITING
    assert home.config_file.read_text(encoding="utf-8") == original  # the first replace is undone from the backup
    assert "Drinks coffee" not in home.memory_file.read_text(encoding="utf-8")


def test_backups_are_pruned_to_the_configured_count(cart, home):
    cart.config.dashboard.backups_keep = 2
    for n in range(3):
        cart.stage_memory_add(f"fact {n}")
        wait_job(cart.start_apply(probes=fake_probes()))
    assert len(cart.backups()) == 2 and len(list(home.backups_dir.iterdir())) == 2


def test_history_restore_stages_the_files_that_differ(cart, home):
    cart.stage_setting("persona.humor_level", "3")
    cart.stage_text(AREA_PERSONA, "# Persona\n\nBe brief.\n")
    wait_job(cart.start_apply(probes=fake_probes()))
    backup_id = cart.backups()[0]["id"]
    staged, error = cart.stage_restore(backup_id)
    assert error is None and {i.file for i in staged} == {FILE_CONFIG, FILE_PERSONA}
    config_item = next(i for i in staged if i.file == FILE_CONFIG)
    assert config_item.kind == "file_restore" and config_item.restarts == ("voice", "telegram") and config_item.area == AREA_SETTINGS
    assert any(kind == "del" and "humor_level = 3" in line for kind, line in config_item.diff())
    items, general = cart.check()
    assert not general and all(not i.error for i in items)
    job = wait_job(cart.start_apply(probes=fake_probes()))
    assert (
        job.success
        and load_config(home.config_file).persona.humor_level == 2
        and home.persona_file.read_text(encoding="utf-8") == "# Persona\n\nBe kind.\n"
    )
    assert cart.stage_restore(backup_id) == ([], None)  # now identical
    assert cart.stage_restore("../etc") == ([], UI.HISTORY_MISSING) and cart.stage_restore("nope-1") == ([], UI.HISTORY_MISSING)
