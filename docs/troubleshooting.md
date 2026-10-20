# Troubleshooting

## Start with `jarvis doctor`

```
jarvis doctor
```

It checks everything Jarvis can check without changing anything: the config, the API key, the database,
every integration, every permission it can query, the background services. Each line is one of:

- `[ok  ]` fine,
- `[warn]` works, but something is missing or worth a look (for example a cue sound that falls back to a
  macOS sound),
- `[FAIL]` broken, with the fix in the same line.

Some lines fail simply because you have not set that feature up, and you can ignore them if you do not want
it: `Google OAuth client`, `Google token`, `Google Sheets` (no Google login), `Spotify credentials`,
`Health inbox` (no Health folder), `Shortcut 'Jarvis Location'` (the location Shortcut is not built yet),
`Timer scheduler (launchd)` (the scheduler is not installed: timers, alarms and the morning brief need it).

`doctor` runs as your terminal app. The background services run as a different program (see the next
section), so a line can be `ok` in the terminal while a service still lacks the same permission.

## Which app gets the permission

macOS grants privacy permissions (Microphone, Calendars, Automation ...) to the program that asks, not to
"Jarvis":

| You use Jarvis through | The permission belongs to |
|---|---|
| `jarvis` in a terminal (chat, `jarvis voice run --foreground`, `debug-record`) | the terminal app: Terminal, iTerm, Ghostty ... |
| the background services (scheduler, Telegram, voice), which launchd starts | the Python of Jarvis's tool environment |

Find that Python with:

```
realpath "$(head -1 "$(realpath "$(which jarvis)")" | cut -c3-)"
```

(for a pip install, replace `$(which jarvis)` with the path of `.venv/bin/jarvis`). It prints something like
`/opt/homebrew/Cellar/python@3.12/3.12.x/Frameworks/Python.framework/Versions/3.12/bin/python3.12`;
`jarvis voice install` prints the same path. To add it in System Settings > Privacy & Security > (section):
click **+**, press Cmd+Shift+G, paste the path, **Open**, switch it on. Then:

```
jarvis services reinstall
```

When Homebrew updates Python 3.12 the path changes, and the permissions must be given to the new one.

## Problems and fixes

### `command not found: jarvis`

- **Cause:** uv's tool folder (`~/.local/bin`) is not on your `PATH`, or the install did not finish.
- **Fix:** `uv tool update-shell`, then open a new terminal window. Check with `jarvis --version`. If it is still
  missing, run the install again from the Jarvis folder:
  `uv tool install --editable '.[voice]' --python 3.12`. With pip, use `.venv/bin/jarvis`.

### "Jarvis needs macOS 26 or newer" / "Apple Silicon (M1 or newer)" / "macOS only"

- **Cause:** `jarvis setup` and `jarvis doctor` stop on unsupported computers. Jarvis relies on features of
  macOS 26 on Apple Silicon (EventKit full access, on-device speech, `networkQuality`, MLX speech models).
- **Fix:** none on that Mac. On a development machine that only runs the tests,
  `JARVIS_ALLOW_UNSUPPORTED_PLATFORM=1` skips the check.

### A permission is missing or was refused

- **Cause:** you clicked "Don't Allow" once, or the program that asked is not the one you expected (see the
  table above).
- **Fix:** System Settings > Privacy & Security > the section named in the error (Calendars, Reminders,
  Microphone, Input Monitoring, Automation, Screen & System Audio Recording, Location Services, Local Network,
  Speech Recognition). Turn on the right app or Python, use **+** if it is not listed, then quit and reopen the
  terminal (macOS applies some permissions only at launch) or run `jarvis services reinstall`.

### A background service is not running, or runs but does nothing

- **Check:** `jarvis services status` (installed? running? which binary?) and `logs/<service>.err.log`
  (`scheduler`, `telegram`, `voice`).
- **Cause 1: the Jarvis folder is inside Documents, Desktop or Downloads.** macOS protects these folders. The
  background Python must ask for access, the prompt may never be shown, and the service hangs silently at start
  (nothing in the logs).
  **Fix:** move the folder out, for example to `~/jarvis-assistant`, then from the new place
  `uv tool install --editable '.[voice]' --python 3.12 --reinstall` and `jarvis services reinstall`. If it must
  stay, give the Python access under Privacy & Security > **Files & Folders** (Documents Folder) or
  **Full Disk Access**, then `jarvis services reinstall`.
- **Cause 2: the service runs an old binary** (after a reinstall or a moved folder). `jarvis doctor` shows a
  `launchd: ...` FAIL line. **Fix:** `jarvis services reinstall`.
- **Cause 3: the service was never installed.** **Fix:** `jarvis scheduler install`, `jarvis telegram install`,
  `jarvis voice install`.
- **Cause 4: a config change.** The voice and Telegram services read `config.toml` only when they start (the
  scheduler re-reads it by itself). **Fix:** `jarvis services reinstall`.

### Google says the login expired

- **Cause:** while your Google OAuth app is in Testing, Google ends the login after 7 days ("Google refused to
  refresh the token", `invalid_grant`).
- **Fix:** `jarvis google login` (no file needed the second time). See [google](integrations/google.md).

### Timers or alarms do not ring

- **Scheduler not installed or not running:** timers, pomodoros, alarms and the morning brief fire from the
  scheduler service, not from the chat. `jarvis scheduler install`; check with `jarvis scheduler status`.
- **No notifications:** timer notifications are shown through AppleScript, so macOS lists them under
  **Script Editor**. Allow it in System Settings > **Notifications** > Script Editor (it appears after the first
  notification).
- **Do Not Disturb or a Focus is on:** it hides those notifications. Add Script Editor to the Focus's allowed
  apps (System Settings > Focus > the Focus > Allowed Notifications), or add `"telegram"` to
  `[timers] notify_channels`. Alarms play their own sound and ring through Do Not Disturb.
- **The Mac was asleep, the lid closed or the battery empty:** nothing can ring. An alarm that came due during
  sleep is reported as missed when the Mac wakes. Setting an alarm warns when this is likely; keep the iPhone's
  own alarm for important mornings.
- **Test:** `jarvis alarms test` rings the alarm with its dialog and stops by itself after 20 seconds.

### "Daily budget exceeded"

- **Cause:** today's estimated Anthropic cost reached `budget.daily_budget_usd`.
- **Effect:** none besides the notice and the red cost in the dashboard: the budget is informational and Jarvis
  keeps answering.
- **Fix:** see what used it with `jarvis usage`; raise the budget in `config.toml` (or in `jarvis setup`) if it
  was expected, or switch `[models] main_model` to a cheaper model. Details in [costs](costs.md).

### Telegram: `409 Conflict: terminated by other getUpdates request`

- **Cause:** two programs poll the same bot token: `jarvis telegram run` or `whoami` in a terminal while the
  background service runs, or Jarvis on a second Mac with the same token.
- **Fix:** stop one of them (`jarvis telegram uninstall` stops the service; Ctrl-C a terminal copy). See
  [telegram](integrations/telegram.md).

### After `git pull`

Jarvis is installed in editable mode, so most updates need only:

```
jarvis services reinstall
```

When the update changed the dependencies (`pyproject.toml`), or a command fails with an import error, reinstall
the tool from the Jarvis folder first, always with the voice extra and Python 3.12:

```
uv tool install --editable '.[voice]' --python 3.12 --reinstall
jarvis services reinstall
```

Without `[voice]` the voice service stops with "missing voice packages"; without `--python 3.12` the environment
may land on another Python, which loses the permissions you granted. A reinstall also deletes the downloaded
wake-word models (they come back on the next voice start, about 20 seconds) and can bring back the Files & Folders
prompt described above. Then run `jarvis doctor`.

### macOS asks whether "python" may use the Keychain

Jarvis keeps its keys in the Keychain. If macOS asks whether Python may access the `jarvis` items, enter your
login password and choose **Always Allow**, otherwise a background service waits for an answer nobody sees.

### The config does not load

`jarvis` and `jarvis doctor` print `Config error:` with the key and the reason (a typo, a wrong type). Fix the
line in `config.toml` or `config.local.toml`; `jarvis setup` adds missing keys from `config.example.toml` and keeps
a backup in `data/backups/`. A running scheduler keeps its last valid config and notifies you once.

## Voice

| Problem | Cause | Fix |
|---|---|---|
| Jarvis does not react to "Hey Jarvis" | Listening is paused | `jarvis voice` shows the state; `jarvis voice resume`, `/unmute` on Telegram, or hold the hotkey and confirm. |
| | The service is not running | `jarvis voice` / `jarvis services status`; `jarvis voice install`. It refuses to start without `use_voice = true` under `[general]`. |
| | Microphone permission missing | `jarvis doctor`: `voice microphone  DENIED`. Allow the terminal app (foreground) or the tool environment's Python (background) under Privacy & Security > Microphone, then `jarvis services reinstall`. |
| | Wrong input device | The service uses the system default input (System Settings > Sound > Input). Prefer the built-in microphone; Bluetooth headsets switch to a low-quality mode. |
| | The threshold does not fit your voice or room | `jarvis voice calibrate`, then set `voice.wake_word_threshold` a bit below your typical peak. |
| The hotkey (⌥ Space) does nothing | Input Monitoring missing | Privacy & Security > Input Monitoring: the terminal app or the tool environment's Python. The wake word works without it. |
| It hears me but understands badly | Device, distance, names | `jarvis voice debug-record` shows the levels and the transcript; add names to `data/vocabulary.md`; correct with "no, I said ..."; compare engines with `jarvis voice test-stt`. |
| `swiftc missing` / Apple recognition not used | No Xcode Command Line Tools | `xcode-select --install`; Whisper is used until then. |
| Apple recognition times out | A pending Speech Recognition prompt | Run `jarvis voice debug-record` in the terminal and allow Speech Recognition. |
| "Another `jarvis voice` is already running" | A second copy | Only one service may run: `jarvis voice uninstall` before `jarvis voice run --foreground`, or Ctrl-C the other one. |
| "Missing voice packages" | Installed without the voice extra | `uv tool install --editable '.[voice]' --python 3.12 --reinstall`, then `jarvis services reinstall` (development checkout: `uv sync --extra voice`). |
| ElevenLabs falls back to the Mac voice (`[say]` in the foreground output) | No key or voice id, the monthly character budget is used up, the quota is exhausted, a slow network (no audio within 3 s), three failures in a row (then `say` for five minutes), or a tainted turn with `elevenlabs_use_for_tainted = false` | `jarvis doctor` shows the ElevenLabs lines and the last fallback reason; see [elevenlabs](integrations/elevenlabs.md). |
| The `say` voice sounds robotic | Only the standard voice is installed | System Settings > Accessibility > Spoken Content > System Voice > Manage Voices: download the Enhanced or Premium version. |
| Jarvis wakes itself up from its own speech | Loud speakers near the microphone | Keep `voice.wake_word_barge_in = false` (the default) or use headphones; lower the volume. |
| The first word is cut off | Speaking before the recording starts | Already covered by `voice.preroll_ms` (1000); raise it slightly if needed. |

More in [voice](integrations/voice.md).

## Reporting a bug

1. Run `jarvis --version` and `jarvis doctor`.
2. Look at `logs/jarvis.log` around the time of the problem (`jarvis --debug` writes more detail).
3. Open an issue with the **Bug report** template (see [CONTRIBUTING.md](../CONTRIBUTING.md)): what happened,
   the steps, `jarvis --version`, your macOS version, the interface (chat, voice, Telegram, scheduler, dashboard)
   and the relevant `doctor` lines.
4. Remove anything personal from logs first (names, addresses, email or calendar content), and never paste an
   API key or token. Security problems go to [SECURITY.md](../SECURITY.md), not to an issue.
