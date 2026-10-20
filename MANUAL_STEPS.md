# Manual steps

Everything here needs you: accounts, keys and macOS permissions that Jarvis cannot set up on its own.
Do the **required** steps once, in order; then pick the optional sections you want, in any order. Tick
the boxes as you go. At any point, `jarvis doctor` lists what works and what is still missing.

Each step says what and why, what to click or type, how to check it worked, and what usually goes wrong.
The longer guides for each integration are in [docs/integrations/](docs/integrations/), and
[docs/troubleshooting.md](docs/troubleshooting.md) collects the common problems.

---

## Required

### 1. Check your Mac

- [ ] **Apple Silicon and macOS 26 or newer.** Jarvis uses macOS features that only exist there.
  Click the Apple menu  > About This Mac. "Chip" must say Apple M1 or newer, "macOS" 26 or newer.
  - Verify: in a terminal, `sw_vers -productVersion` prints 26 or higher and `uname -m` prints `arm64`.
  - Problem: on an Intel Mac or an older macOS, `jarvis setup` stops with a message saying so.

### 2. Install the tools

- [ ] **Open the terminal.** Press Cmd-Space, type `Terminal`, press Enter. You type commands there and
  press Enter to run them.
- [ ] **Install Homebrew** (installs other tools). Paste this, press Enter and follow the questions; it asks
  for your Mac password once:
  ```
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```
  At the end it may print two lines under "Next steps" (starting with `echo` and `eval`): run them, then
  open a new terminal window.
  - Verify: `brew --version` prints a version.
- [ ] **Install git, uv, Python 3.12 and ffmpeg:**
  ```
  brew install git uv python@3.12 ffmpeg
  ```
  - Verify: `uv --version` and `python3.12 --version` both print a version.

### 3. Download and install Jarvis

- [ ] **Download it into your home folder.** Not into Documents, Desktop or Downloads: macOS protects those
  folders and can silently block Jarvis's background services there.
  ```
  git clone https://github.com/sefkk/jarvis-assistant.git ~/jarvis-assistant
  cd ~/jarvis-assistant
  ```
- [ ] **Install the `jarvis` command.** With the voice extra (wake word, speech recognition), as below; for
  text only, leave out `[voice]`. It takes a few minutes the first time.
  ```
  uv tool install --editable '.[voice]' --python 3.12
  ```
  - Verify: `jarvis --version` prints `jarvis 1.0.0` and a `home:` line with the folder.
  - Problem: `command not found: jarvis` → run `uv tool update-shell`, close the terminal, open a new one.
  - Alternative without uv: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements-voice.txt`
    (or `requirements.txt` for text only), then use `.venv/bin/jarvis` instead of `jarvis`.

### 4. Get an Anthropic API key

- [ ] **Create the key.** Jarvis's thinking runs on Claude, which is billed per use (usually cents a day).
  Go to https://platform.claude.com, sign up, add a payment method under Billing, then open
  https://platform.claude.com/settings/keys and create a key. Copy the key (it starts with `sk-ant-`); you will paste it in the next step.
  - Problem: the key only works after billing is set up; a key without credit makes every answer fail.
  - Details: [docs/integrations/anthropic.md](docs/integrations/anthropic.md).

### 5. Run the setup wizard

- [ ] **`jarvis setup`.** It asks, in this order: your language (1 for English, 2 for Turkish), your name
  and how Jarvis should address you, where you live, your Anthropic API key (nothing shows while you paste;
  that is normal, and the key goes into the macOS Keychain, never into a file), the daily budget in USD,
  and then each optional feature, which you can skip with Enter.
  ```
  jarvis setup
  ```
  It creates `config.toml`, `data/profile.md` and the other files Jarvis needs; running it again never
  overwrites them. Change the language later with `jarvis setup --language`.
  - Verify: the wizard ends with "Setup finished" and runs the health check.

### 6. Health check and first chat

- [ ] **`jarvis doctor`.** `[ok  ] Anthropic API key  found` must be there. `[warn]` and `[FAIL]` lines for
  features you have not set up yet are fine.
- [ ] **Talk to Jarvis.** Run `jarvis`, wait for the greeting with your name and the `you ›` prompt, and type
  `what time is it?`. Leave with `exit` (or Ctrl-D).
  - Problem: "Cannot start without an API key" → `jarvis key set anthropic_api_key` and paste the key.

---

## Optional

Every section below is independent. Each one is off until you do it.

### Background scheduler: timers, pomodoro, alarms, the morning brief

- [ ] **Install the scheduler.** Timers and alarms ring from a small background service, so they work with the
  chat closed and after a restart. `jarvis scheduler install`.
  - Verify: `jarvis scheduler status` says running; "remind me in 1 minute" in the chat shows a notification
    a minute later.
- [ ] **Allow the notifications.** The first notification makes macOS ask whether "Script Editor" may notify
  you (Jarvis sends notifications through AppleScript): click Allow, or System Settings > Notifications >
  Script Editor.
- [ ] **Let them through Do Not Disturb (optional).** System Settings > Focus > Do Not Disturb > Allowed
  Notifications > Apps > + > Script Editor.
- [ ] **An alarm sound (optional).** Put a 2–10 s loopable sound at `sounds/alarm.m4a`; without it a macOS
  sound loops. Try it with `jarvis alarms test`. See [sounds/README.md](sounds/README.md).
- [ ] **The automatic morning brief (optional).** Set `enabled = true` under `[brief.schedule]` in
  `config.toml` (or say "send me the morning brief at 8:30"). It is spoken when you are at the Mac and sent
  to Telegram otherwise. `jarvis brief schedule` shows the next run; `jarvis brief` prints today's brief now.
  - Problem: the brief is spoken during Do Not Disturb → the scheduler's Python needs Full Disk Access to read
    the Focus state (System Settings > Privacy & Security > Full Disk Access, add the Python that
    `jarvis services status` shows).

### Apple Calendar and Reminders

- [ ] **Allow access.** In the chat ask "what's on my calendar today" and "list my reminders"; macOS asks once
  per app: click **Allow Full Access**. If nothing appears: System Settings > Privacy & Security > Calendars
  (and Reminders), switch on your terminal app.
  - Verify: `jarvis doctor` shows both as full access granted.
  - Problem: background services (Telegram, scheduler) need the same permission for their own Python; add it
    with the + button. Details: [docs/integrations/apple.md](docs/integrations/apple.md).

### Google: Calendar, Gmail drafts, Sheets

- [ ] **Create your own Google OAuth client** (every user creates their own; Jarvis ships none) and log in with
  `jarvis google login --client-secret ~/Downloads/client_secret_….json`.
  Step by step: [docs/integrations/google.md](docs/integrations/google.md).
  - Verify: `jarvis doctor` lists the Google token and its scopes; "what's on my Google calendar today" works.
  - Problem: while the consent screen is in Testing, Google expires the login after 7 days → `jarvis google login`.

### Spotify

- [ ] **Create a Spotify app** at https://developer.spotify.com/dashboard and store its two keys:
  `jarvis key set spotify_client_id` and `jarvis key set spotify_client_secret`. The Spotify app must be
  installed; allow the terminal to control it when macOS asks.
  - Verify: "play some jazz". Details: [docs/integrations/spotify.md](docs/integrations/spotify.md).

### Telegram (Jarvis on your phone)

- [ ] **Create a bot with @BotFather**, then `jarvis telegram set-token`, `jarvis telegram whoami`, put both ids
  and `enabled = true` under `[telegram]` in `config.toml`, and `jarvis telegram install`.
  Step by step: [docs/integrations/telegram.md](docs/integrations/telegram.md).
  - Verify: send `/help` to your bot.
  - If your phone is lost: send `/stop` to the bot from any device, or run `jarvis telegram uninstall` on the Mac.

### Voice: "Hey Jarvis"

- [ ] **Turn voice on.** Needs the voice extra from step 3. Set `use_voice = true` under `[general]` in
  `config.toml` (or say yes to Voice in `jarvis setup`), then try it in the terminal: `jarvis voice run --foreground`.
- [ ] **Microphone permission.** Click Allow when macOS asks. Background service: `jarvis voice install`
  prints the Python that needs Microphone (and Input Monitoring for the ⌥ Space push-to-talk hotkey) under
  System Settings > Privacy & Security.
  - Verify: say "Hey Jarvis", wait for the sound, ask something; `jarvis voice status` shows the service.
  - Problem: nothing happens → `jarvis voice status` (paused?), then [docs/troubleshooting.md](docs/troubleshooting.md).
  - Details: [docs/integrations/voice.md](docs/integrations/voice.md).
- [ ] **Your own cue sounds (optional).** Put them in `sounds/`; until then macOS system sounds play.
  See [sounds/README.md](sounds/README.md).

### ElevenLabs (a natural voice)

- [ ] **Store a key** with `jarvis voice set-elevenlabs-key`, pick a voice with `jarvis voice list-voices`, put
  its id into `elevenlabs_voice_id` and `tts_engine = "elevenlabs"` under `[voice]`, then
  `jarvis services reinstall`. Without ElevenLabs Jarvis uses the Mac's own voice.
  - Verify: `jarvis voice say "Hello, I am Jarvis."`. Details: [docs/integrations/elevenlabs.md](docs/integrations/elevenlabs.md).

### Google Maps (places and travel times)

- [ ] **Create a Maps Platform key** with Places API (New), Routes API and Geocoding API enabled and billing
  linked, restrict it to those APIs, then `jarvis key set google_maps_api_key`.
  - Verify: "is there a pharmacy open nearby". Details: [docs/integrations/maps.md](docs/integrations/maps.md).

### Apple Shortcuts

- [ ] **Allowlist your Shortcuts.** Jarvis only runs Shortcuts listed under `[[shortcuts.allowed]]` in
  `config.toml`. `jarvis shortcuts list` shows the ones on this Mac.
  Details: [docs/integrations/shortcuts.md](docs/integrations/shortcuts.md).

### Current Location

- [ ] **Build the "Jarvis Location" Shortcut** so weather and routes start where you are now (otherwise your
  home town is used). Details: [docs/integrations/location.md](docs/integrations/location.md).
  - Verify: `jarvis location refresh` names the Shortcut as the source.

### Training log from Apple Health

- [ ] **Build the iPhone Shortcut** that drops your workouts into an iCloud folder, or import a full Health
  export once with `jarvis health import ~/Downloads/export.zip`.
  Details: [docs/integrations/health.md](docs/integrations/health.md).

### Internet test and "who is home"

- [ ] **At home, run `jarvis presence setup`** and pick your phone. Set your iPhone's Private Wi-Fi Address to
  Fixed for the home network, allow Local Network for the scheduler's Python, and tell anyone you add.
  Details: [docs/integrations/presence.md](docs/integrations/presence.md).
  - Verify: `jarvis presence status`; "why is my internet slow" runs a speed test.

### Screenshots ("what's on my screen")

- [ ] **Screen recording permission.** The first "explain what's on my screen" makes macOS ask; otherwise System
  Settings > Privacy & Security > Screen & System Audio Recording, switch on your terminal app and restart it.
  - Verify: `jarvis doctor` shows Screen Recording granted.

### Dashboard

- [ ] **Open it once:** `jarvis dashboard` opens a local page (127.0.0.1 only) with a one-time link: health,
  services, costs, logs, the audit trail and your settings. Ctrl-C stops it. Nothing to set up.

---

## macOS permissions at a glance

| Permission | Needed for | Who gets it |
|---|---|---|
| Calendars, Reminders | calendar and reminder tools, alarm backups | the terminal app; for background services also their Python |
| Microphone | the voice service (Telegram voice notes do not need it) | the terminal app (`voice run`) or the voice service's Python |
| Input Monitoring | the push-to-talk hotkey | same as Microphone |
| Screen & System Audio Recording | screenshots | the terminal app |
| Automation | Spotify, Shortcuts, Notes and other apps | the terminal app or the service's Python |
| Location Services | the Wi-Fi name in the internet test; the Shortcuts app for "Jarvis Location" | the terminal app / Shortcuts |
| Local Network | presence scans and the router check | the scheduler's Python |
| Notifications | timers, alarms, reminders | Script Editor |
| Full Disk Access (optional) | respecting Do Not Disturb for the spoken brief | the scheduler's Python |

`jarvis services status` shows which Python the background services run. After reinstalling the tool, macOS
may ask again for that new binary.

## Updating Jarvis

```
cd ~/jarvis-assistant && git pull
uv tool install --editable '.[voice]' --python 3.12 --reinstall
jarvis services reinstall
```
Keep `[voice]` and `--python 3.12` (without them the voice service stops or loses its permissions). The
wake-word model is downloaded again on the next start.

## Removing Jarvis

`jarvis uninstall` removes the background services and asks before it deletes the Keychain entries and your
local data. Then `uv tool uninstall jarvis` and delete `~/jarvis-assistant`.
