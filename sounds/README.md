# sounds/ — voice cue files and the alarm sound

Put your own short cue sounds in this folder. Jarvis looks here (the Jarvis home, resolved through
`JARVIS_HOME`) when the voice service needs to play a cue. The repository ships no sound files: until you
add your own, Jarvis plays macOS system sounds (see "Lookup order"). Files you put here stay out of git.

## Which files

| File | When it plays | Blocking? |
| --- | --- | --- |
| `voice_heard.m4a` | Right after Jarvis has finished recording your request (end of speech detected, or the hotkey released). It tells you "I heard you" during the silence while Jarvis transcribes and thinks. | No. Transcription and the API call start immediately while it plays. If the reply's first audio is ready while the cue is still playing, the cue is stopped so they never overlap. |
| `voice_ready.m4a` | When Jarvis has finished speaking and starts listening again (the follow-up window, no wake word needed). | Yes, for the microphone. The follow-up window and its seconds start only after the cue has finished, so the microphone never hears the cue (no self-trigger, no fake transcript). |
| `thinking_sound.m4a` | Loops quietly while Jarvis works on your answer (transcription, the API, tool calls), so silence never means "stuck". It is armed right after `voice_heard`, waits for that cue to end and then `thinking_delay_ms` (200 ms in `config.toml`) more, and only starts if no spoken reply arrived meanwhile: fast replies stay clean. | No. It repeats (with `thinking_gap_ms` of silence between runs) until the reply's first audio, a confirmation question, an error sentence, a barge-in or a pause stops it at once. It never plays over Jarvis's voice. Its own volume: `thinking_volume` (0.8 in `config.toml`). |

| `alarm.m4a` | When an alarm (`set_alarm`, "set an alarm for 7") goes off: the file loops at full volume until you press Stop / Snooze, hold the push-to-talk hotkey, say a stop or snooze word ("stop", "snooze"), send /stop_alarm on Telegram or run `jarvis alarms stop`; at most `max_ring_min` (10) minutes. The system volume is raised to `[alarm] volume` (70) while it rings and put back afterwards. | Not a voice cue: it is played by the scheduler process, so it rings with the voice service off too, and Do Not Disturb does not silence it (it is our own sound, not a notification). Without the file the macOS `Sosumi.aiff` loops instead; `jarvis doctor` warns. Name: `[alarm] sound = "alarm"`. Pick something 2-10 s long that can loop; `sound_volume` is the extra `afplay -v` gain. Try it with `jarvis alarms test`. |

The wake-word "listening" sound (Tink) and the "no speech" sound (Pop) are not affected; they keep
the `listen_sound` / `no_speech_sound` settings under `[voice]`.

Rules:

- No cue plays while listening is paused.
- A barge-in (hotkey while Jarvis speaks) stops any cue immediately.
- `voice_heard` never plays for a recording that is dropped as empty (nothing said, cancelled). If
  the transcript is later dropped as noise or a hallucination, the cue is cut at that moment.
- A cue never holds the microphone longer than 3 seconds, whatever the file length.
- The thinking sound is killed before any sentence is spoken (reply, confirmation, choice dialog,
  error), and after a confirmation has been answered it comes back (with the same delay) while the
  turn goes on working. In the text chat a small "Thinking…" spinner plays this role instead.

## Lookup order

For each cue Jarvis takes the first file that exists, in this order:

1. `sounds/<name>.m4a`
2. `sounds/<name>.mp3`
3. `sounds/<name>.wav`
4. `sounds/<name>.aiff`

`<name>` comes from `config.toml`, `[voice.sounds]` (`heard = "voice_heard"`, `ready = "voice_ready"`,
`thinking = "thinking_sound"`). When no file is found, a macOS system sound plays instead (`Bottle.aiff`
for heard, `Purr.aiff` for ready, `Submarine.aiff` for thinking) and `jarvis doctor` shows a warning.
Nothing crashes without the files.

## Recommended format

- `.m4a` (AAC), mono or stereo, 44.1 or 48 kHz.
- Short: 0.3–1 second. `voice_ready` delays the follow-up window by its own length, so keep it short.
  `thinking_sound` is the exception: 1–3 seconds of something soft that can repeat (a hum, a slow
  tick); it is cut mid-file whenever the reply starts, so it needs no clean ending.
- Normalized volume (peak around -1 dBFS, no clipping); fine-tune with `volume` in `[voice.sounds]`
  (passed to `afplay -v`, 1.0 = the file's own level).
- Distinct from each other and from Tink / Pop, so you can tell the states apart without looking.

Everything is played with `afplay` (argv, never a shell). Switch a cue off with `heard_enabled = false`,
`ready_enabled = false` or `thinking_enabled = false`.
