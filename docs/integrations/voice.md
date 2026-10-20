# Voice: "Hey Jarvis"

The voice service listens on your Mac for the wake word "Hey Jarvis" (or a held hotkey, ⌥ Space by default),
records what you say, turns it into text on the Mac and speaks the answer, sentence by sentence while it is
still being written. After a reply it keeps listening for a few seconds, so you can go on without the wake
word.

Wake word, recording and speech recognition all run on the Mac: no audio leaves it unless you choose
ElevenLabs Scribe for recognition, and nothing reaches the Anthropic API before the wake word or the hotkey.
Replies are spoken by the Mac's own `say` voice, or by ElevenLabs if you set it up ([elevenlabs](elevenlabs.md)).

## What you need

- The voice extra of the installation (the Quick start in the README installs it):

  ```
  uv tool install --editable '.[voice]' --python 3.12
  ```

  Development checkout: `uv sync --extra voice`; pip: `requirements-voice.txt`.
- `ffmpeg` (`brew install ffmpeg`).
- For Apple's on-device speech recognition (the default engine): the Xcode Command Line Tools, which provide
  the Swift compiler `swiftc`:

  ```
  xcode-select --install
  ```

  Without them Jarvis uses the local Whisper model instead (slower, still on the Mac).
- A microphone. The built-in one is best; a Bluetooth headset microphone (AirPods) switches the link to a
  low-quality mode and makes recognition worse.
- Disk space: the first start downloads the wake-word model and a Whisper model (about 500 MB) once.

## Set it up

1. Turn voice on. `jarvis setup` does it when you answer yes to "Voice", or set it by hand:

   ```toml
   [general]
   use_voice = true
   ```

   `voice.stt_language` follows the main language you chose in `jarvis setup` (`"en"` or `"tr"`).
2. Run it in the foreground once. It prints every state change, what it heard and the timings:

   ```
   jarvis voice run --foreground
   ```

   macOS asks for the **Microphone** for your terminal app: click **Allow**. The first start compiles the small
   Apple speech helper into `data/bin/` and downloads the models; then it says it is ready.
3. Say "Hey Jarvis", wait for the short sound, then "what time is it?". Speaking right after the wake word is
   fine; Jarvis keeps the second before the recording starts.
4. When the first request with Apple recognition runs, macOS asks for **Speech Recognition**: allow it. (If
   it never asks, run `jarvis voice debug-record` once in the terminal.)
5. Stop with Ctrl-C and install the background service, which starts at login and restarts after a crash:

   ```
   jarvis voice install
   ```

   It prints a line like `Under launchd the Microphone and Input Monitoring permissions belong to
   /opt/homebrew/.../python3.12`. That Python, not your terminal, now needs the permissions below.
6. Grant the permissions to that Python (System Settings > Privacy & Security):
   - **Microphone**: allow the prompt when it appears, or add the Python with **+** (Cmd+Shift+G pastes a path).
   - **Input Monitoring**: add the same Python (needed for the hotkey only).
   - Then `jarvis services reinstall`.

Only one voice service may run at a time: stop the background one (`jarvis voice uninstall`) before using
`jarvis voice run --foreground`, then install it again.

### Everyday use

- "Hey Jarvis, ..." or hold ⌥ Space while you talk. Pressing the hotkey while Jarvis speaks interrupts it.
- Pause listening (the microphone is released, the orange dot goes away):

  ```
  jarvis voice pause            # until you resume
  jarvis voice pause --for 60m  # comes back by itself
  jarvis voice resume
  jarvis voice                  # status: running, listening or paused
  ```

  Or say "Hey Jarvis, stop listening" (the phrases are `voice.pause_phrases`), or `/mute` and `/unmute` on
  Telegram. Holding the hotkey while paused asks whether to listen again.
- Risky actions ask for confirmation: a dialog appears with the real arguments, and you can click the button or
  say "confirm" (`voice.confirm_phrases`). A bare "yes" is asked once more. For shell commands, file writes and
  deletes only the button counts.

### Make recognition better

- `data/vocabulary.md`: names, places and artists Jarvis should expect, one per line (`- Ada Lovelace`).
- `data/corrections.md`: when Jarvis mishears a name, say "no, I said Ada" right after; the fix is stored and
  applied from then on. You can edit the file.
- Compare the engines on your own voice. This records 15 sentences once and scores every option (accuracy,
  speed, cost); results go to `data/stt_test/`:

  ```
  jarvis voice test-stt
  jarvis voice test-stt --compare data/stt_test/<folder>   # score the same recordings again
  ```

  `--cloud` adds ElevenLabs Scribe (paid, the audio leaves the Mac), `--download` the large Whisper models.
- See one utterance exactly as the service hears it (device, levels, WAV file, engine, transcript):

  ```
  jarvis voice debug-record
  ```
- The wake word triggers too easily or not at all: `jarvis voice calibrate` shows live scores; adjust
  `voice.wake_word_threshold` (0.5 by default).

### Choose the engines

```toml
[voice]
stt_engine = "apple"        # "apple" (on-device), "whisper" (local), "elevenlabs" (Scribe, cloud, paid), "hybrid"
stt_language = "en"         # "en" | "tr"; "auto" works with Whisper only
tts_engine = "say"          # "say" or "elevenlabs"
tts_voice_en = "Samantha"   # the `say` voice for English (`say -v '?'` lists them)
tts_voice_tr = "Yelda"      # the `say` voice for Turkish
follow_up_mode = "always"   # "always" | "questions_only" | "off"
save_audio = false          # true keeps every recording in data/voice_audio/ (debugging only)
```

The `say` voices sound much better in their Enhanced or Premium versions: System Settings > Accessibility >
Spoken Content > System Voice > **Manage Voices...**, download for example "Samantha (Enhanced)". Jarvis uses
the best installed version automatically.

Your own cue sounds (heard, ready, thinking, alarm) go into `sounds/`; see `sounds/README.md`. Without them
macOS system sounds play.

After changing `[voice]`, run `jarvis services reinstall` so the background service reads it.

## macOS permissions

| Permission | Where | Who needs it |
|---|---|---|
| Microphone | Privacy & Security > Microphone | the terminal app (foreground runs, `debug-record`, `test-stt`); the tool environment's Python (background service) |
| Input Monitoring | Privacy & Security > Input Monitoring | the same two; only for the hotkey |
| Speech Recognition | Privacy & Security > Speech Recognition | asked on the first Apple recognition |
| Calendars, Reminders, Automation ... | see each integration | the tool environment's Python, for what you ask by voice |

Which Python exactly: [troubleshooting](../troubleshooting.md#which-app-gets-the-permission).

## Check that it works

`jarvis doctor` shows (among others): `voice enabled  use_voice = true`, `voice packages  installed`,
`voice microphone  granted to this process`, `voice input monitoring`, `voice wake word model`,
`voice whisper model`, `voice speech engine  apple, Whisper as the fallback; Apple helper compiled`,
`voice agent` running and `voice listening`. These describe the terminal you run `doctor` from; the background
service's own permissions show in `logs/voice.out.log` and `logs/jarvis.log`.

Speak a test: "Hey Jarvis, what's the weather tomorrow?"

## Common problems

See [troubleshooting](../troubleshooting.md#voice) for the full list. The most common ones:

| Problem | Fix |
|---|---|
| `jarvis voice install` refuses | Set `use_voice = true` under `[general]`. |
| "Missing voice packages" | Reinstall with the voice extra: `uv tool install --editable '.[voice]' --python 3.12 --reinstall`, then `jarvis services reinstall`. |
| Works in the foreground, deaf in the background | The tool environment's Python lacks the Microphone permission. |
| The hotkey does nothing | Input Monitoring is missing for that process. The wake word still works. |
| `swiftc missing` | `xcode-select --install`. Until then Whisper is used. |
| Jarvis is paused | `jarvis voice resume`. |
