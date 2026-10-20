"""Telegram interface with a fake Bot API (recorded calls, scripted updates) and a fake clock.
`FakeApi`, `FakeClock`, the update builders and `make_bot` are shared with the security tests."""

from __future__ import annotations

import itertools
import json
import plistlib
import subprocess
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from jarvis import i18n, services
from jarvis.exceptions import JarvisError
from jarvis.i18n import t
from jarvis.interfaces import telegram_bot, telegram_launchd, telegram_state
from jarvis.interfaces.telegram_api import TelegramApiError, split_message
from jarvis.interfaces.telegram_bot import (
    TelegramBot,
    TelegramSink,
    duration_label,
    format_confirm,
    health_checks,
    help_text,
    run_bot,
    set_token,
    whoami,
)
from jarvis.interfaces.telegram_notify import TelegramNotifier
from jarvis.io.base import ConfirmRequest
from jarvis.io.voice import VOICE_UNAVAILABLE, VoiceUnavailable

USER = 1001
CHAT = 2002
_ids = itertools.count(1)


# --- fakes -----------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeApi:
    """Records every call; `batches` scripts what getUpdates returns (a list, or a callable that
    receives the api and returns a list). When the script is exhausted the clock advances by the
    poll timeout and, outside a turn, the bot is told to stop."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.batches: deque = deque()
        self.bot: TelegramBot | None = None
        self.files: dict[str, bytes] = {}
        self.errors: dict[str, Exception] = {}
        self._next_message_id = 100

    # recording
    def _record(self, method: str, **kw: Any) -> None:
        self.calls.append((method, kw))
        if method in self.errors:
            raise self.errors[method]

    def of(self, method: str) -> list[dict[str, Any]]:
        return [kw for m, kw in self.calls if m == method]

    def sent(self) -> list[str]:
        return [kw["text"] for kw in self.of("sendMessage")]

    def edits(self) -> list[str]:
        return [kw["text"] for kw in self.of("editMessageText")]

    def last_action(self) -> tuple[str, int]:
        """(action_id, message_id) of the last message that carried inline buttons."""
        for method, kw in reversed(self.calls):
            if method == "sendMessage" and kw.get("reply_markup"):
                data = kw["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                return data.split(":")[1], kw["message_id"]
        raise AssertionError("no confirmation was sent")

    # Bot API surface
    def get_me(self) -> dict[str, Any]:
        self._record("getMe")
        return {"id": 42, "username": "jarvis_test_bot", "first_name": "Jarvis"}

    def get_updates(self, offset, timeout, allowed_updates=None) -> list[dict[str, Any]]:
        self._record("getUpdates", offset=offset, timeout=timeout, allowed_updates=allowed_updates)
        if self.batches:
            batch = self.batches.popleft()
            if callable(batch):
                batch = batch(self)
            return list(batch)
        self.clock.advance(timeout)
        if self.bot is not None and not self.bot._in_turn:
            self.bot.request_stop()
        return []

    def send_message(self, chat_id, text, reply_markup=None) -> dict[str, Any]:
        if len(text) > 4096 or not text.strip():
            raise TelegramApiError("sendMessage: bad length")
        self._next_message_id += 1
        self._record("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup, message_id=self._next_message_id)
        if reply_markup:
            global _last_button_message_id
            _last_button_message_id = self._next_message_id
        return {"message_id": self._next_message_id, "chat": {"id": chat_id}}

    def send_long(self, chat_id, text) -> list[dict[str, Any]]:
        return [self.send_message(chat_id, chunk) for chunk in split_message(text)]

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None) -> dict[str, Any]:
        if len(text) > 4096:
            raise TelegramApiError("editMessageText: too long")
        self._record("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
        return {}

    def send_chat_action(self, chat_id, action="typing") -> None:
        self._record("sendChatAction", chat_id=chat_id, action=action)

    def answer_callback_query(self, callback_query_id, text=None) -> None:
        self._record("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def get_file(self, file_id) -> dict[str, Any]:
        self._record("getFile", file_id=file_id)
        return {"file_id": file_id, "file_path": f"voice/{file_id}.oga"}

    def download_file(self, file_path, max_bytes=20 * 1024 * 1024) -> bytes:
        self._record("download", file_path=file_path)
        return self.files[file_path]

    def close(self) -> None:
        self._record("close")


class FakeAssistant:
    def __init__(self, reply: Callable[[str], str] | None = None) -> None:
        self.calls: list[tuple[str, list | None]] = []
        self.sessions = 1
        self.reply = reply or (lambda text: f"echo: {text}")

    def handle(self, text, sink, *, untrusted=None, spoken=False):
        self.calls.append((text, untrusted))
        self.spoken = getattr(self, "spoken", []) + [spoken]
        reply = self.reply(text)
        sink.write_delta(reply)
        sink.finish()
        return SimpleNamespace(reply=reply)

    def new_session(self, *, resume: bool = False) -> str:
        self.sessions += 1
        self.resumed = resume
        return f"s{self.sessions}"

    def close(self) -> None:
        pass


def message_update(text: str | None = None, *, user: int = USER, chat: int = CHAT, chat_type: str = "private", **extra: Any) -> dict:
    message: dict[str, Any] = {
        "message_id": next(_ids),
        "from": {"id": user, "is_bot": False, "first_name": "Someone"},
        "chat": {"id": chat, "type": chat_type},
        "date": 0,
    }
    if text is not None:
        message["text"] = text
    message.update(extra)
    return {"update_id": next(_ids), "message": message}


_last_button_message_id: int | None = None


def callback_update(
    action_id: str, value: str, *, user: int = USER, chat: int = CHAT, message_id: int | None = None, prefix: str = "cf"
) -> dict:
    """A button press. By default it is bound to the last message that carried buttons, as a real
    press would be; pass another `message_id` to simulate a press on the wrong message."""
    if message_id is None:
        message_id = _last_button_message_id
    return {
        "update_id": next(_ids),
        "callback_query": {
            "id": f"cq{next(_ids)}",
            "from": {"id": user, "is_bot": False},
            "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}},
            "data": f"{prefix}:{action_id}:{value}",
        },
    }


def make_bot(config, paths, *, clock: FakeClock | None = None, assistant=None, assistant_factory=None, **kw) -> tuple[TelegramBot, FakeApi]:
    config.telegram.enabled = True
    config.telegram.allowed_user_id = USER
    config.telegram.allowed_chat_id = CHAT
    clock = clock or FakeClock()
    api = FakeApi(clock)
    fake = assistant or FakeAssistant()
    factory = assistant_factory or (lambda c, p, r: fake)

    def sleep(seconds: float) -> None:  # only reached on polling errors: never spin
        clock.advance(seconds)
        api.bot.request_stop()

    bot = TelegramBot(config, paths, api=api, assistant_factory=factory, clock=clock, sleep=sleep, **kw)
    api.bot = bot
    return bot, api


def audit_lines(logs: Path) -> list[dict]:
    path = logs / "audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- the sink -------------------------------------------------------------------------------------


def test_sink_sends_after_one_second_then_edits_then_finishes(config, paths):
    clock = FakeClock()
    bot, api = make_bot(config, paths, clock=clock)
    sink = TelegramSink(bot, CHAT)
    sink.write_delta("Hel")
    assert api.of("sendChatAction") and api.sent() == []  # typing first, nothing sent yet
    clock.advance(0.5)
    sink.write_delta("lo")
    assert api.sent() == []
    clock.advance(0.6)
    sink.write_delta(" wor")
    assert api.sent() == ["Hello wor"] and api.edits() == []
    clock.advance(0.3)
    sink.write_delta("ld")
    assert api.edits() == []  # less than a second since the last send
    clock.advance(1.0)
    sink.write_delta("!")
    assert api.edits() == ["Hello world!"]
    clock.advance(2.0)
    sink.write_delta("")  # empty deltas are ignored
    sink.finish()
    assert api.edits() == ["Hello world!"]  # unchanged text: no extra edit
    sink.write_delta("second turn")
    sink.finish()
    assert api.sent() == ["Hello wor", "second turn"]
    assert api.of("editMessageText")[0]["message_id"] == api.of("sendMessage")[0]["message_id"]


def test_sink_finish_splits_at_4096_and_falls_back_to_send_on_edit_error(config, paths):
    clock = FakeClock()
    bot, api = make_bot(config, paths, clock=clock)
    sink = TelegramSink(bot, CHAT)
    sink.write_delta("a" * 10)
    clock.advance(1.5)
    sink.write_delta("a" * 4990)  # 5000 chars in total; the streaming message shows the first 4096
    assert [len(t) for t in api.sent()] == [4096]
    sink.finish()
    assert [len(t) for t in api.sent()] == [4096, 904]

    api.errors["editMessageText"] = TelegramApiError("message is not modified")
    sink.write_delta("x")
    clock.advance(1.5)
    sink.write_delta("y")
    assert api.sent()[-1] == "xy"
    sink.write_delta("z")
    sink.finish()
    assert api.sent()[-1] == "xyz"  # the failed edit became a new message


def test_sink_puts_the_reply_in_its_own_message_after_the_commentary_before_a_tool(config, paths):
    """Before: "Hemen bakıyorum.Hava durumu için ... Kerem, işte durum" in one edited message."""
    clock = FakeClock()
    bot, api = make_bot(config, paths, clock=clock)
    sink = TelegramSink(bot, CHAT)
    sink.write_delta("Hemen bakıyorum.")  # still inside the first second: not sent yet
    sink.segment_break()
    sink.notice("• daily_brief")
    sink.segment_break()  # a second tool block right after: nothing new to flush
    sink.write_delta("Günaydın Kerem! ")
    sink.write_delta("Bugün maç yok.")
    sink.finish()
    assert api.sent() == ["Hemen bakıyorum.", "ℹ️ • daily_brief", "Günaydın Kerem! Bugün maç yok."]
    assert api.edits() == []


def test_sink_notice_and_debug(config, paths):
    bot, api = make_bot(config, paths)
    sink = TelegramSink(bot, CHAT)
    sink.notice("Compacting earlier conversation...")
    sink.debug("thinking...")
    assert api.sent() == ["ℹ️ Compacting earlier conversation..."]
    sink.notice("\x1b[31mred\x1b[0m")
    assert api.sent()[-1] == "ℹ️ red"


def test_format_confirm_shows_the_full_actual_args_and_is_clean():
    request = ConfirmRequest(
        tool="gmail_create_draft",
        args={"to": "a@b.c", "body": "x" * 3000 + "\x1b[2J"},
        risk="CONFIRM",
        tainted=True,
        taint_warning="This action was triggered after reading untrusted content from telegram_forward.",
        reason="why",
        command_preview="echo hi",
    )
    text = format_confirm(request)
    assert "gmail_create_draft" in text and "CONFIRM" in text and "why" in text and "echo hi" in text
    assert "telegram_forward" in text and "\x1b" not in text
    assert "x" * 3000 in text  # never truncated: the user approves what they can see
    header = format_confirm(request, include_args=False, language="tr")
    assert "x" * 3000 not in header and "tam hali" in header and header.startswith("⚠️ Onay gerekiyor")
    english = format_confirm(request, include_args=False, language="en")
    assert english.startswith("⚠️ Confirmation needed") and "in full in the message(s) above" in english


def test_confirm_sends_long_args_in_full_before_the_buttons(config, paths, logs):
    bot, api = make_bot(config, paths)
    sink = TelegramSink(bot, chat_id=CHAT)
    body = "y" * 9000  # far beyond one Telegram message
    request = ConfirmRequest(tool="gmail_create_draft", args={"to": "a@b.c", "body": body}, risk="CONFIRM")
    api.batches.append(lambda a: [callback_update(a.last_action()[0], "y")])
    assert sink.confirm(request) is True
    sent = api.sent()
    assert body in "".join(sent)  # the full body reached the phone, split over several messages
    assert "tam hali" in sent[-1] and api.of("sendMessage")[-1]["reply_markup"]
    assert all(len(s) <= 4096 for s in sent)


def test_nothing_is_edited_into_the_chat_after_stop(config, paths, logs):
    bot, api = make_bot(config, paths)
    sink = TelegramSink(bot, chat_id=CHAT)
    sink._message_id = 77  # a streaming message already exists
    sink.parts = ["Ajandanız: "] if hasattr(sink, "parts") else None
    bot._stopped = True
    sink.write_delta("gizli toplantı 15:00")
    sink.finish()
    assert api.of("editMessageText") == [] and api.sent() == []


def test_notifier_respects_the_kill_switch_and_the_enabled_flag(config, paths, tmp_path):
    from jarvis.interfaces import telegram_state
    from jarvis.interfaces.telegram_notify import TelegramNotifier

    config.telegram.enabled = True
    config.telegram.allowed_chat_id = CHAT
    api = FakeApi(FakeClock())
    notifier = TelegramNotifier(config, api=api, state_file=paths.telegram_state_file)
    notifier.notify("Jarvis", "timer done")
    assert api.sent() == ["Jarvis: timer done"]
    telegram_state.request_stop(paths.telegram_state_file)
    notifier.notify("Jarvis", "agenda summary")
    assert api.sent() == ["Jarvis: timer done"]  # blocked while stopped
    assert notifier.blocked_reason() == "the kill switch (/stop) is active"
    config.telegram.enabled = False
    disabled = TelegramNotifier(config, api=api, state_file=tmp_path / "other_state.json")
    disabled.notify("Jarvis", "x")
    assert api.sent() == ["Jarvis: timer done"]


# --- commands and turns ----------------------------------------------------------------------------


def test_start_help_new_and_unknown_commands(config, paths, logs):
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    api.batches.extend(
        [
            [message_update("/start")],
            [message_update("/help@jarvis_test_bot")],
            [message_update("hello there")],
            [message_update("/new")],
            [message_update("/weather Istanbul")],  # unknown slash command: normal text
        ]
    )
    bot.run()
    assert api.sent()[0] == t("telegram.bot.greeting", "tr")
    assert api.sent()[1] == help_text(config, "tr") and "Fenerbahçe" in api.sent()[1] and "/location" in api.sent()[1]
    assert api.sent()[2] == "echo: hello there"
    assert "Yeni oturum" in api.sent()[3]
    assert assistant.sessions == 2
    assert [c[0] for c in assistant.calls] == ["hello there", "/weather Istanbul"]
    assert all(c[1] is None for c in assistant.calls)


def test_offset_advances_past_every_update(config, paths):
    bot, api = make_bot(config, paths)
    u1, u2 = message_update("a"), message_update("b", user=999)
    api.batches.append([u1, u2])
    bot.run()
    polls = api.of("getUpdates")
    assert polls[0]["offset"] is None and polls[1]["offset"] == u2["update_id"] + 1
    assert polls[0]["allowed_updates"] == ["message", "callback_query"]


def test_day_rollover_starts_a_new_session(config, paths):
    clock = FakeClock()
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, clock=clock, assistant=assistant)
    api.batches.append([message_update("one")])
    api.batches.append(lambda a: (clock.advance(30), [message_update("two")])[1])
    api.batches.append(lambda a: (clock.advance(86_400), [message_update("three")])[1])
    bot.run()
    assert assistant.sessions == 2  # only the date change rolled the session
    assert [c[0] for c in assistant.calls] == ["one", "two", "three"]


def test_non_text_message_gets_a_short_reply(config, paths):
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    api.batches.append([message_update(photo=[{"file_id": "p"}])])
    bot.run()
    assert assistant.calls == [] and "metin" in api.sent()[0]


def test_turn_crash_is_reported_not_fatal(config, paths):
    class Broken(FakeAssistant):
        def handle(self, text, sink, *, untrusted=None):
            raise RuntimeError("boom")

    bot, api = make_bot(config, paths, assistant=Broken())
    api.batches.append([message_update("x")])
    bot.run()
    assert "RuntimeError" in api.sent()[0] and "boom" not in api.sent()[0]


# --- voice -----------------------------------------------------------------------------------------


class FakeStt:
    def __init__(self, text="yarın hava nasıl", language="tr"):
        self.text, self.language = text, language
        self.audio: list[bytes] = []

    def transcribe(self, audio, sample_rate=16000):
        self.audio.append(audio)
        return self.text, self.language


def test_voice_message_is_transcribed_echoed_and_handled_as_trusted(config, paths, logs):
    assistant = FakeAssistant()
    stt = FakeStt()
    bot, api = make_bot(config, paths, assistant=assistant, stt_factory=lambda: stt)
    api.files["voice/v1.oga"] = b"OggS-fake"
    api.batches.append([message_update(voice={"file_id": "v1", "duration": 4, "mime_type": "audio/ogg", "file_size": 9})])
    bot.run()
    assert stt.audio == [b"OggS-fake"]
    assert api.sent()[0] == "🎙️ Anladığım: yarın hava nasıl"
    assert api.sent()[1] == "echo: yarın hava nasıl"
    assert assistant.calls == [("yarın hava nasıl", None)]
    assert assistant.spoken == [True]  # tools treat names in a voice note as possibly misheard
    voice_lines = [l for l in audit_lines(logs) if l.get("decision") == "voice"]
    assert voice_lines and voice_lines[0]["language"] == "tr"


def test_a_correction_after_a_voice_note_is_learned(config, paths):
    from jarvis.voice.corrections import CorrectionStore

    assistant = FakeAssistant()
    stt = FakeStt(text="Spotify'da Sezen Aksa'dan çal")
    stt.corrections = CorrectionStore(paths.corrections_file)
    bot, api = make_bot(config, paths, assistant=assistant, stt_factory=lambda: stt)
    api.files["voice/v1.oga"] = b"OggS-fake"
    api.batches.append([message_update(voice={"file_id": "v1", "duration": 3, "mime_type": "audio/ogg", "file_size": 9})])
    api.batches.append([message_update(text="Hayır, Sezen Aksu dedim")])
    bot.run()
    assert [c[0] for c in assistant.calls] == ["Spotify'da Sezen Aksa'dan çal", "Hayır, Sezen Aksu dedim"]
    assert assistant.spoken == [True, False]
    assert "- Sezen Aksa → Sezen Aksu" in paths.corrections_file.read_text(encoding="utf-8")


def test_voice_unavailable_reply_and_limits(config, paths, monkeypatch):
    assistant = FakeAssistant()
    from jarvis.io import voice

    class Missing(voice.SpeechToText):
        def transcribe(self, audio, sample_rate=16000):
            raise VoiceUnavailable(VOICE_UNAVAILABLE)

    monkeypatch.setattr(voice, "SpeechToText", Missing)  # the default factory picks this up
    bot, api = make_bot(config, paths, assistant=assistant)
    api.files["voice/v1.oga"] = b"x"
    api.batches.extend(
        [
            [message_update(voice={"file_id": "v1", "duration": 3, "file_size": 1})],
            [message_update(voice={"file_id": "v2", "duration": 601, "file_size": 1})],
            [message_update(voice={"file_id": "v3", "duration": 3, "file_size": 21 * 1024 * 1024})],
        ]
    )
    bot.run()
    assert api.sent()[0] == VOICE_UNAVAILABLE
    assert "uzun" in api.sent()[1] and "büyük" in api.sent()[2]
    assert assistant.calls == [] and len(api.of("download")) == 1


def test_empty_transcript(config, paths):
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant, stt_factory=lambda: FakeStt(text="  "))
    api.files["voice/v1.oga"] = b"x"
    api.batches.append([message_update(voice={"file_id": "v1", "duration": 1})])
    bot.run()
    assert "anlayamadım" in api.sent()[0] and assistant.calls == []


# --- SpeechToText itself (no mlx, no ffmpeg) --------------------------------------------------------


def test_speech_to_text_raises_voice_unavailable_without_dependencies(monkeypatch):
    from jarvis.io.voice import SpeechToText

    stt = SpeechToText(ffmpeg="definitely-not-a-real-ffmpeg-binary")
    monkeypatch.setattr(SpeechToText, "_load_whisper", lambda self: SimpleNamespace(transcribe=lambda *a, **k: {"text": "x"}))
    with pytest.raises(VoiceUnavailable, match=r"\.\[voice\]"):
        stt.transcribe(b"audio")
    assert stt.transcribe(b"") == ("", None)
    assert SpeechToText.available(ffmpeg="definitely-not-a-real-ffmpeg-binary") is False


def test_speech_to_text_runs_ffmpeg_as_argv_and_cleans_up(monkeypatch, tmp_path):
    from jarvis.io.voice import SpeechToText

    seen: dict[str, Any] = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        assert kw.get("shell") is not True
        Path(argv[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fake_transcribe(path, path_or_hf_repo):
        seen["wav"] = Path(path)
        seen["model"] = path_or_hf_repo
        assert Path(path).read_bytes() == b"RIFF"
        return {"text": "  merhaba dünya ", "language": "tr"}

    monkeypatch.setattr("jarvis.io.voice.subprocess.run", fake_run)
    monkeypatch.setattr("jarvis.io.voice.shutil.which", lambda name: "/opt/ffmpeg")
    monkeypatch.setattr(SpeechToText, "_load_whisper", lambda self: SimpleNamespace(transcribe=fake_transcribe))
    stt = SpeechToText()
    assert stt.transcribe(b"OggS") == ("merhaba dünya", "tr")
    assert seen["argv"][:2] == ["ffmpeg", "-y"] and "-ac" in seen["argv"] and "16000" in seen["argv"]
    assert seen["model"] == "mlx-community/whisper-small-mlx"
    assert not seen["wav"].exists() and not seen["wav"].parent.exists()  # temp files deleted


# --- state file --------------------------------------------------------------------------------


def test_state_round_trip_and_two_step_enable(paths):
    path = paths.telegram_state_file
    assert telegram_state.load_state(path) == telegram_state.TelegramState()
    assert "not stopped" in telegram_state.enable_from_mac(path)
    telegram_state.request_stop(path)
    state = telegram_state.load_state(path)
    assert state.stopped and state.stopped_at and not state.reenable_requested
    assert "Send /start" in telegram_state.enable_from_mac(path)
    assert telegram_state.load_state(path).stopped
    telegram_state.request_reenable(path)
    assert telegram_state.load_state(path).reenable_requested
    assert "re-enabled" in telegram_state.enable_from_mac(path)
    state = telegram_state.load_state(path)
    assert not state.stopped and not state.reenable_requested and state.enabled_at
    assert not path.with_suffix(".json.tmp").exists()


def test_corrupt_state_file_fails_closed(paths):
    paths.telegram_state_file.write_text("{not json")
    assert telegram_state.load_state(paths.telegram_state_file).stopped is True


# --- launchd ------------------------------------------------------------------------------------


class FakeLaunchctl:
    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        code, out, err = self.results.get(argv[0], (0, "", ""))
        return subprocess.CompletedProcess(["launchctl", *argv], code, out, err)


@pytest.fixture
def plist(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "LaunchAgents" / f"{telegram_launchd.LABEL}.plist"
    monkeypatch.setattr(telegram_launchd, "plist_path", lambda: target)
    monkeypatch.setattr(telegram_launchd, "_uid", lambda: 501)
    fake = tmp_path / "venv" / "bin" / "jarvis"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(telegram_launchd, "jarvis_executable", lambda: fake)
    return target


def test_launchd_install_status_uninstall(paths, plist, monkeypatch):
    fake = FakeLaunchctl({"print": (0, "state = running\npid = 777\n", "")})
    monkeypatch.setattr(telegram_launchd, "_run_launchctl", fake)
    assert telegram_launchd.LABEL == f"{services.LABEL_PREFIX}.telegram"
    target = telegram_launchd.install(paths)
    data = plistlib.loads(target.read_bytes())
    assert data["ProgramArguments"][1:] == ["telegram", "run"] and data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["Label"] == telegram_launchd.LABEL and data["EnvironmentVariables"]["JARVIS_HOME"] == str(Path(paths.home).resolve())
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]  # ffmpeg for voice under launchd
    assert [c[0] for c in fake.calls] == ["bootout", "bootstrap"]
    status = telegram_launchd.status()
    assert status.installed and status.running and status.pid == 777
    assert telegram_launchd.uninstall() is True and not target.exists()
    assert telegram_launchd.status().installed is False


def test_launchd_status_never_raises(plist, monkeypatch):
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(b"x")

    def explode(argv):
        raise OSError("no launchctl")

    monkeypatch.setattr(telegram_launchd, "_run_launchctl", explode)
    status = telegram_launchd.status()
    assert status.installed is False and "unknown" in status.detail


# --- notifier --------------------------------------------------------------------------------------


def test_notifier_requires_chat_id_and_token(config, monkeypatch):
    monkeypatch.setattr("jarvis.interfaces.telegram_notify.secrets.get_secret", lambda name: None)
    with pytest.raises(JarvisError, match="allowed_chat_id"):
        TelegramNotifier(config)
    config.telegram.allowed_chat_id = CHAT
    with pytest.raises(JarvisError, match="set-token"):
        TelegramNotifier(config)


def test_notifier_sends_title_and_message(config, paths):
    config.telegram.allowed_chat_id = CHAT
    config.telegram.enabled = True
    api = FakeApi(FakeClock())
    notifier = TelegramNotifier(config, api=api, state_file=paths.telegram_state_file)
    assert notifier.name == "telegram"
    notifier.notify("Timer", "tea is ready")
    assert api.of("sendMessage") == [{"chat_id": CHAT, "text": "Timer: tea is ready", "reply_markup": None, "message_id": 101}]


# --- CLI helpers -----------------------------------------------------------------------------------


def test_run_bot_refuses_when_disabled_or_unconfigured(config, paths, monkeypatch):
    out: list[str] = []
    config.telegram.enabled = False
    assert run_bot(config, paths, out=out.append) == 1 and "Telegram kapalı" in out[-1]
    config.telegram.enabled = True
    assert run_bot(config, paths, out=out.append) == 1 and "whoami" in out[-1]
    config.telegram.allowed_user_id = USER
    config.telegram.allowed_chat_id = CHAT
    monkeypatch.setattr("jarvis.interfaces.telegram_bot.secrets.get_secret", lambda name: None)
    assert run_bot(config, paths, out=out.append) == 1 and "set-token" in out[-1]


def test_bot_refuses_to_run_with_zero_ids(config, paths):
    api = FakeApi(FakeClock())
    bot = TelegramBot(config, paths, api=api, assistant_factory=lambda c, p, r: FakeAssistant())
    with pytest.raises(JarvisError, match="whoami"):
        bot.run()
    assert api.calls == []


def test_whoami_prints_ids_and_replies_only_in_private(config, paths, logs):
    clock = FakeClock()
    api = FakeApi(clock)
    out: list[str] = []
    api.batches.append([message_update("hi", user=555, chat=555)])
    found = whoami(config, paths, timeout_s=30, api=api, out=out.append, clock=clock)
    assert found == {"user_id": 555, "chat_id": 555, "chat_type": "private"}
    assert api.sent() == ["Jarvis kurulumu: kullanıcı id 555, sohbet id 555"]
    assert any("@jarvis_test_bot" in line for line in out)
    assert api.of("getUpdates")[0]["allowed_updates"] == ["message"]

    api = FakeApi(clock)
    api.batches.append([message_update("hi", user=555, chat=-100, chat_type="supergroup")])
    found = whoami(config, paths, timeout_s=30, api=api, out=out.append, clock=clock)
    assert found["chat_type"] == "supergroup" and api.sent() == []

    api = FakeApi(clock)  # nothing arrives
    assert whoami(config, paths, timeout_s=5, api=api, out=out.append, clock=clock) is None


def test_set_token_stores_in_keychain(monkeypatch):
    stored: dict[str, str] = {}
    monkeypatch.setattr("jarvis.interfaces.telegram_bot.secrets.set_secret", lambda n, v: stored.__setitem__(n, v))
    out: list[str] = []
    assert set_token(prompt=lambda _: "  123456789:AAHfakeTOKENvalue_for-tests-0123456789 ", out=out.append)
    assert stored == {"telegram_bot_token": "123456789:AAHfakeTOKENvalue_for-tests-0123456789"}
    assert not any("123456789:AAH" in line for line in out)
    assert set_token(prompt=lambda _: "", out=out.append) is False


def test_health_checks_never_raise_and_cover_everything(config, paths, monkeypatch):
    monkeypatch.setattr("jarvis.interfaces.telegram_bot.secrets.get_secret", lambda name: None)
    monkeypatch.setattr(telegram_launchd, "plist_path", lambda: paths.home / "nope.plist")
    checks = health_checks(config, paths)
    names = [c.name for c in checks]
    assert names == [
        "telegram enabled",
        "telegram token",
        "telegram ids",
        "telegram remote tools",
        "telegram agent",
        "telegram kill switch",
        "telegram voice",
    ]
    assert all(c.ok for c in checks)  # disabled: nothing is a failure
    config.telegram.enabled = True
    checks = {c.name: c for c in health_checks(config, paths)}
    assert not checks["telegram token"].ok and not checks["telegram ids"].ok and not checks["telegram agent"].ok
    telegram_state.request_stop(paths.telegram_state_file)
    checks = {c.name: c for c in health_checks(config, paths)}
    assert not checks["telegram kill switch"].ok and "STOPPED" in checks["telegram kill switch"].detail


def test_enable_from_mac_wrapper(paths):
    assert "not stopped" in telegram_bot.enable_from_mac(paths)


def test_mute_and_unmute_commands(config, paths, logs):
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    api.batches.extend(
        [
            [message_update("/mute 60m")],
            [message_update("/mute soon")],
            [message_update("/mute")],
            [message_update("/unmute")],
            [message_update("/unmute")],
        ]
    )
    bot.run()
    sent = api.sent()
    assert sent[0].startswith("🔇") and "60 dk" in sent[0] and "/unmute" in sent[0]
    assert "Süreyi anlayamadım" in sent[1]
    assert sent[2].startswith("🔇") and "durduruldu" in sent[2]
    assert sent[3].startswith("🎙️") and "zaten" in sent[4]
    assert not paths.voice_pause_file.exists()
    assert assistant.calls == []  # commands never reach the model
    audit = (logs / "audit.jsonl").read_text()
    assert '"reason": "mute_command"' in audit and '"reason": "unmute_command"' in audit


def test_mute_records_telegram_as_the_source(config, paths):
    from jarvis.voice import pause as vpause

    bot, api = make_bot(config, paths)
    api.batches.append([message_update("/mute 15m")])
    bot.run()
    state = vpause.load_state(paths.voice_pause_file)
    assert state.by == "telegram" and 14 <= state.remaining_minutes() <= 15


# --- English ----------------------------------------------------------------------------------------


@pytest.fixture
def english(config):
    config.general.language = "en"
    i18n.set_language("en")
    return config


def test_english_commands_help_and_location_alias(english, paths):
    config = english
    assistant = FakeAssistant()
    bot, api = make_bot(config, paths, assistant=assistant)
    api.batches.extend(
        [
            [message_update("/start")],
            [message_update("/help")],
            [message_update("/new")],
            [message_update("/location")],
            [message_update(photo=[{"file_id": "p"}])],
        ]
    )
    bot.run()
    messages = api.of("sendMessage")
    sent = [m["text"] for m in messages]
    assert sent[0].startswith("Hi, Jarvis here.")
    assert sent[1].startswith("What I can do from the phone:") and "/location" in sent[1]
    assert "Fenerbahçe: matches, standings" in sent[1]  # football.team
    assert sent[2] == "New session started."
    assert sent[3].startswith("📍 To share your location")
    button = messages[3]["reply_markup"]["keyboard"][0][0]
    assert button == {"text": "📍 Share my location", "request_location": True}
    assert sent[4] == "For now I only understand text and voice messages."
    assert assistant.calls == []


def test_commands_are_english_in_every_language():
    for language in ("en", "tr"):
        help_text_ = i18n.t("telegram.help.text", language, team="X")
        assert "/location" in help_text_ and "/konum" not in help_text_


def test_english_mute_and_voice_texts(english, paths):
    config = english
    stt = FakeStt(text="  ")
    bot, api = make_bot(config, paths, stt_factory=lambda: stt)
    api.files["voice/v1.oga"] = b"x"
    api.batches.extend(
        [
            [message_update("/mute 150m")],
            [message_update("/mute soon")],
            [message_update("/unmute")],
            [message_update(voice={"file_id": "v1", "duration": 1})],
            [message_update(voice={"file_id": "v2", "duration": 601, "file_size": 1})],
        ]
    )
    bot.run()
    sent = api.sent()
    assert sent[0].startswith("🔇 Listening on the Mac is paused for 2 h 30 min;")
    assert sent[1].startswith("I didn't get the duration.")
    assert sent[2] == "🎙️ Listening on the Mac is back on."
    assert sent[3] == "I couldn't make out the voice message."
    assert sent[4] == "That voice message is too long (601 s). 10 minutes at most."


def test_voice_echo_follows_the_language_heard(config, paths):
    bot, api = make_bot(config, paths, stt_factory=lambda: FakeStt(text="what's the weather", language="en"))
    api.files["voice/v1.oga"] = b"x"
    api.batches.append([message_update(voice={"file_id": "v1", "duration": 2})])
    bot.run()
    assert api.sent()[0] == "🎙️ I heard: what's the weather"  # the main language is Turkish here


def test_duration_label_matches_the_voice_pause_label_in_turkish():
    from jarvis.voice import pause as vpause

    for minutes in (1, 60, 119, 120, 150, 180):
        assert duration_label(minutes, "tr") == vpause.duration_label(minutes, "tr")
    assert duration_label(150, "en") == "2 h 30 min" and duration_label(45, "en") == "45 min"


def test_english_cli_lines(english, paths, monkeypatch):
    config = english
    out: list[str] = []
    config.telegram.enabled = False
    assert run_bot(config, paths, out=out.append) == 1 and out[-1].startswith("Telegram is disabled")
    clock = FakeClock()
    api = FakeApi(clock)
    api.batches.append([message_update("hi", user=555, chat=555)])
    whoami(config, paths, timeout_s=30, api=api, out=out.append, clock=clock)
    assert api.sent() == ["Jarvis setup: user id 555, chat id 555"]
    monkeypatch.setattr("jarvis.interfaces.telegram_bot.secrets.set_secret", lambda n, v: None)
    prompts: list[str] = []
    assert set_token(prompt=lambda p: prompts.append(p) or "x", out=out.append)
    assert prompts == ["Telegram bot token (from BotFather): "] and out[-2].startswith("Warning:")
