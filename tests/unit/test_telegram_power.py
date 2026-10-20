"""/status, /lock and /sleep on Telegram: the power tools of the remote profile behind the usual
Approve / Decline (Onayla / Reddet) confirmation, and "iptal" cancelling a sleep countdown. The Mac is never touched."""

from __future__ import annotations

from datetime import datetime

import pytest

from jarvis.i18n import t
from jarvis.interfaces.telegram_bot import help_text
from jarvis.tools import power
from jarvis.tools import power as power_mod
from jarvis.tools.power import MacStatus
from jarvis.utils.timeutil import local_tz
from tests.unit.test_power import FakeTimer
from tests.unit.test_telegram_bot import FakeAssistant, audit_lines, callback_update, make_bot, message_update

NOW = datetime(2026, 10, 18, 22, 0, tzinfo=local_tz())


@pytest.fixture
def fake_timers(monkeypatch):
    made: list[FakeTimer] = []

    def make(delay, fn):
        timer = FakeTimer(delay, fn)
        made.append(timer)
        return timer

    monkeypatch.setattr(power, "_make_timer", make)
    yield made
    power.cancel_countdown()


@pytest.fixture
def no_mac(monkeypatch):
    """No pmset, no ioreg, no login framework: canned answers."""
    monkeypatch.setattr(power, "_now", lambda: NOW)
    monkeypatch.setattr(
        power,
        "collect_status",
        lambda now=None, with_battery_log=True: MacStatus(
            power_source="AC",
            battery_percent=27,
            charging=True,
            battery_state="charging",
            battery_remaining="1:32",
            idle_s=133.7,
            screen_locked=False,
            uptime_s=3600 * 5,
            sleep_blockers=["caffeinate"],
        ),
    )
    locked = {"value": False}
    monkeypatch.setattr(power, "screen_locked", lambda: locked["value"])

    def lock_now(**kw):
        locked["value"] = True
        return "login framework"

    monkeypatch.setattr(power, "lock_screen_now", lock_now)
    monkeypatch.setattr(power, "sleep_now", lambda: locked.__setitem__("slept", True))
    return locked


def test_help_lists_the_power_commands(config):
    text = help_text(config)
    assert "/status" in text and "/lock" in text and "/sleep" in text
    assert "Kapatma ve yeniden başlatma yok" in text


def test_shipped_config_allows_the_power_tools_remotely(config):
    for name in ("mac_status", "lock_screen", "sleep_mac", "schedule_sleep", "cancel_scheduled_sleep"):
        assert f"power.{name}" in config.telegram.remote_allowed_tools


def test_status_command_runs_mac_status_without_the_model(config, paths, logs, no_mac):
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    assert "mac_status" in bot.registry.names() and "sleep_mac" in bot.registry.names()
    api.batches.append([message_update("/status")])
    bot.run()
    reply = api.sent()[0]
    assert reply.startswith("Güç: adaptöre takılı, pil %27 (şarj oluyor), dolmasına 1:32")
    assert "Son kullanım: 2 dk 14 sn önce" in reply and "Ekran: kilitli değil" in reply
    assert assistant.calls == []  # a shortcut, not a turn
    assert any(a.get("decision") == "ran" and a.get("for_tool") == "mac_status" for a in audit_lines(logs))


def test_lock_command_locks_once(config, paths, logs, no_mac):
    bot, api = make_bot(config, paths)
    api.batches.extend([[message_update("/lock")], [message_update("/lock")]])
    bot.run()
    assert api.sent() == ["Ekran kilitlendi.", "Ekran zaten kilitli."]
    assert no_mac["value"] is True


def test_sleep_command_asks_with_buttons_then_warns_and_iptal_cancels(config, paths, logs, no_mac, fake_timers):
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    api.batches.append([message_update("/sleep")])
    api.batches.append(lambda a: [callback_update(a.last_action()[0], "y")])  # Onayla
    api.batches.append([message_update("iptal")])
    bot.run()
    sent = api.sent()
    confirmation = next(t for t in sent if "Onay gerekiyor" in t)
    assert "sleep_mac" in confirmation and "CONFIRM" in confirmation and "pmset sleepnow" in confirmation
    assert '"delay_s": 10' in confirmation
    assert api.of("sendMessage")[sent.index(confirmation)]["reply_markup"]  # the two buttons
    warning = next(t for t in sent if t.startswith(power_mod.WARNING_TEXT.format(seconds=10)))
    assert "System:" not in warning  # the phone gets the plain sentence, no note for the model
    timer = fake_timers[-1]
    assert timer.delay == 10 and timer.started
    assert sent[-1] == power_mod.CANCELLED_TEXT and timer.cancelled
    assert assistant.calls == []  # neither /sleep nor "iptal" reached the model
    assert "slept" not in no_mac
    decisions = [a.get("decision") for a in audit_lines(logs) if a.get("tool") == "sleep_mac"]
    assert decisions == ["countdown", "cancelled"]
    assert any(a.get("decision") == "confirmed" and a.get("for_tool") == "sleep_mac" for a in audit_lines(logs))


def test_sleep_command_reddet_does_nothing(config, paths, logs, no_mac, fake_timers):
    bot, api = make_bot(config, paths)
    api.batches.append([message_update("/sleep 30")])
    api.batches.append(lambda a: [callback_update(a.last_action()[0], "n")])  # Reddet
    bot.run()
    assert api.sent()[-1] == t("telegram.power.denied", "tr", command="/sleep")
    assert fake_timers == [] and power.pending_countdown() is None
    assert any(a.get("decision") == "denied" and a.get("reason") == "command_denied" for a in audit_lines(logs))


def test_sleep_command_timeout_does_nothing(config, paths, logs, no_mac, fake_timers):
    bot, api = make_bot(config, paths)
    api.batches.append([message_update("/sleep")])  # nobody presses: the fake clock runs past the timeout
    bot.run()
    assert api.sent()[-1] == t("telegram.power.denied", "tr", command="/sleep") and fake_timers == []


def test_sleep_command_argument(config, paths, logs, no_mac, fake_timers):
    bot, api = make_bot(config, paths)
    api.batches.append([message_update("/sleep abc")])
    api.batches.append([message_update("/sleep 2")])  # below the minimum: validation error, usage
    api.batches.append([message_update("/sleep 45")])
    api.batches.append(lambda a: [callback_update(a.last_action()[0], "y")])
    bot.run()
    sent = api.sent()
    usage = t("telegram.power.sleep_usage", "tr")
    assert sent[0] == usage and sent[1] == usage
    assert any(t.startswith(power_mod.WARNING_TEXT.format(seconds=45)) for t in sent)
    assert fake_timers[-1].delay == 45


def test_sleep_fires_when_nobody_cancels(config, paths, logs, no_mac, fake_timers):
    bot, api = make_bot(config, paths)
    api.batches.append([message_update("/sleep")])
    api.batches.append(lambda a: [callback_update(a.last_action()[0], "y")])
    bot.run()
    fake_timers[-1].fire()
    assert no_mac.get("slept") is True
    assert any(a.get("decision") == "slept" for a in audit_lines(logs))


def test_iptal_without_a_pending_sleep_is_a_normal_message(config, paths, no_mac):
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    api.batches.append([message_update("iptal")])
    bot.run()
    assert [c[0] for c in assistant.calls] == ["iptal"] and api.sent() == ["echo: iptal"]


def test_power_commands_refuse_when_not_in_the_remote_profile(config, paths, logs, no_mac, fake_timers):
    config.telegram.remote_allowed_tools = [p for p in config.telegram.remote_allowed_tools if not p.startswith("power.")]
    bot, api = make_bot(config, paths)
    assert "sleep_mac" in bot.forbidden
    api.batches.extend([[message_update("/sleep")], [message_update("/status")]])
    bot.run()
    assert "power.sleep_mac" in api.sent()[0] and "remote_allowed_tools" in api.sent()[0]
    assert "power.mac_status" in api.sent()[1]
    assert fake_timers == []
    assert sum(1 for a in audit_lines(logs) if a.get("reason") == "tool_not_remote") == 2


def test_sleep_mac_in_a_model_turn_sends_the_warning_as_a_notice(config, paths, logs, no_mac, fake_timers):
    """Through the model (a real turn), the tool pushes the warning to the phone at once; the result tells
    the model it already went out."""
    from jarvis.tools.base import Runtime

    class SleepingAssistant(FakeAssistant):
        def handle(self, text, sink, *, untrusted=None):
            self.calls.append((text, untrusted))
            tool = bot.registry.get("sleep_mac")
            tool.ctx.runtime = Runtime(client=None, db=None, usage_tracker=None, registry=None, budget=None, channel="telegram", sink=sink)
            result = tool.run(tool.parse_args({}))
            sink.write_delta("Tamam, 10 saniye içinde uyutuyorum; 'iptal' dersen vazgeçerim.")
            sink.finish()
            self.result = result

    assistant = SleepingAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    api.batches.append([message_update("bilgisayarı uyut")])
    bot.run()
    assert api.sent()[0] == "ℹ️ " + power_mod.WARNING_TEXT.format(seconds=10)
    assert "already sent to the phone" in assistant.result.content
    assert fake_timers[-1].started
