# Interfaces

Every interface drives the same `Assistant` through an `io.base.OutputSink` (see
[../io/README.md](../io/README.md)). This folder holds the Telegram bot; the other interfaces live in
their own packages. Where each one is:

| Interface | Command | Code |
| --- | --- | --- |
| CLI chat | `jarvis` (or `jarvis chat`) | `main.py` (`build_parser`, `cmd_chat`), `io/text.py` |
| Voice service | `jarvis voice run`, `jarvis voice install` | `voice/`, `io/voice.py` |
| Telegram bot | `jarvis telegram run`, `jarvis telegram install` | `interfaces/` (this folder) |
| Scheduler | `jarvis scheduler run`, `jarvis scheduler install` | `scheduler/` |
| Dashboard | `jarvis dashboard` | `dashboard/` (see its README) |

## Telegram (this folder)

- `telegram_bot.py`: `TelegramBot` long-polls `getUpdates` (no inbound port) and handles one message at a
  time; `TelegramSink` streams by editing one message and asks confirmations with inline buttons. Only the
  allowlisted user and chat (`telegram.allowed_user_id`, `telegram.allowed_chat_id`) are answered, the
  tools are limited to `telegram.remote_allowed_tools`, forwarded messages are untrusted content, and the
  rate limit is `telegram.max_messages_per_minute`. The slash commands are in `COMMANDS` and stay English
  in every language.
- `telegram_api.py`: a thin Bot API client over `httpx`; the token never reaches a log or a message.
- `telegram_state.py`: the kill switch. `/stop` from the phone stops the bot; turning it back on needs
  `/start` from the phone and `jarvis telegram enable` on the Mac.
- `telegram_notify.py`: the Telegram channel for timers, alarms and other notifications.
- `telegram_launchd.py`: the launchd agent (`jarvis telegram install|uninstall|status`).

## Voice (`voice/`)

`service.py` is the state machine (idle, listening, transcribing, thinking, speaking, with a follow-up
window). Around it: `wakeword.py` (openWakeWord), `vad.py` (Silero VAD), `hotkey.py` (push to talk),
`audio.py`, `stt_engines.py` (Apple Speech through `apple_speech.swift`, mlx-whisper, ElevenLabs Scribe,
hybrid), `tts.py`, `say.py` and `elevenlabs.py` (speech output), `normalize.py` (numbers and dates as words),
`cues.py` (sounds), `confirm.py` (spoken confirmations), `pause.py`, `announce.py` (speech requests from
other processes), `names.py`, `vocabulary.py`, `corrections.py`, `debug.py` (`jarvis voice debug-record`),
`stt_test.py` (`jarvis voice test-stt`), `health.py` and `launchd.py`.

## Scheduler (`scheduler/`)

`service.py` runs the loop behind `jarvis scheduler run`: `engine.py` fires due timers and pomodoros
(`store.py` keeps them in SQLite), `alarm.py` rings alarms, `brief.py` delivers the automatic morning brief
(one `Assistant` turn), `notify.py` picks the notification channels and
`config_watch.py` re-reads the config while the loop runs. The presence scan and the periodic internet
check hook into the same loop. `launchd.py` installs the agent.

## Tests

`tests/unit/test_telegram_bot.py`, `test_telegram_api.py`, `test_telegram_power.py`,
`tests/security/test_telegram_security.py`, `tests/unit/test_voice_*.py`, `test_scheduler.py`,
`test_alarms.py`, `test_brief_schedule.py`, `test_config_watch.py`, `test_launchd.py`.
