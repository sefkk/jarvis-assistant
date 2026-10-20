# io: input and output interfaces

The `Assistant` only talks to the abstract interfaces in `base.py`, so the core does not know whether a
reply is printed, spoken or sent to a phone.

- `base.py`
  - `InputSource.read()`: blocks until the next utterance; `None` means the user wants to quit.
  - `OutputSink`: what a turn writes to. Required: `write_delta` (a streamed chunk), `finish`, `confirm`,
    `notice`, `debug`. Optional with defaults: `confirm_detailed` (how and why a confirmation was answered),
    `error` (the final failure message, with a `spoken` variant), `segment_break` (the model is about to
    call a tool, so the text before it was a status line), `taint_changed` and `ask_choice`.
  - `ConfirmRequest` / `ConfirmOutcome`: the confirmation shown to the user and its answer.
  - `compose_reply`: the reply of a turn is the text after the last tool block; short status lines
    written before a tool call are left out.
- `text.py`: the terminal (`TerminalInput`, `TerminalOutput`, built on `rich`). Everything printed passes
  through `sanitize.for_terminal`.
- `voice.py`: `SpeechToText` (the configured engine with local Whisper as the fallback), `VoiceInputSource`
  and `VoiceOutputSink` (streams the reply sentence by sentence into text-to-speech and asks confirmations
  with a dialog plus a spoken phrase). The voice extra is imported lazily, so this module imports without it.

Other sinks live with their interface: `TelegramSink` in `interfaces/telegram_bot.py`, `QuietSink` (the
scheduled morning brief) in `scheduler/brief.py`, `NullSink` in `context.py` (compaction and the memory pass).

## Adding an interface

1. Implement an `OutputSink` (and an `InputSource` if the interface reads input in a loop).
2. Build an `Assistant(config, paths, channel="<name>")` and call `assistant.handle(text, sink)` per message.
   The channel is told to the model in the system prompt (`context.CHANNEL_NOTES`).
3. Pass content the user did not type (a forwarded message) as `untrusted=[(source, text)]`, so it is
   wrapped and taints the turn.
4. Send every user-facing string through `jarvis.i18n.t` and clean outgoing text (`sanitize.for_terminal`).

## Tests

`tests/unit/test_assistant.py`, `test_voice_sink.py`, `test_confirm_terminal_telegram.py`,
`test_exit_words.py`; `tests/conftest.py` has a `RecordingSink` for tests that drive the `Assistant`.
