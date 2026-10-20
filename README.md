# Jarvis

**A personal AI assistant for your Mac: type to it, talk to it, or message it from your phone.**

[IMAGE: hero screenshot or banner]

![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![macOS 26+ on Apple Silicon](https://img.shields.io/badge/macOS-26%2B%20%C2%B7%20Apple%20Silicon-lightgrey)
[![CI](https://github.com/sefkk/jarvis-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/sefkk/jarvis-assistant/actions/workflows/ci.yml)

## What is Jarvis?

Jarvis is an assistant that lives on your Mac. The thinking is done by Claude on the Anthropic API;
everything else (your calendar, files, reminders, voice, memory and logs) is handled on the Mac itself.
It speaks English and Turkish, remembers what matters to you, and asks before it does anything risky.

What it can do:

- 💬 Chat in the terminal, by voice ("Hey Jarvis") or from your phone through Telegram
- 📅 Read Apple and Google calendars, reminders, and your Gmail (drafts only, it never sends)
- 🌦️ Weather, web search, places and travel times
- ⏰ Timers, pomodoros and loud wake-up alarms with a phone backup
- ☀️ A morning brief, spoken at the Mac or sent to Telegram
- 🎵 Spotify, Apple Shortcuts, Google Sheets, a training log from Apple Health
- 🔒 Lock, sleep or check your Mac from the phone
- 📊 A local dashboard to watch costs and logs and change settings

[IMAGE: voice demo GIF]

## Quick start

You need a Mac with Apple Silicon (M1 or newer) on macOS 26 or newer, and an
[Anthropic API key](https://platform.claude.com/settings/keys).

1. Install the tools ([Homebrew](https://brew.sh) first, if you do not have it):
   ```
   brew install git uv python@3.12 ffmpeg
   ```
2. Download Jarvis. Keep it out of Documents, Desktop and Downloads: macOS privacy prompts there can
   block the background services.
   ```
   git clone https://github.com/sefkk/jarvis-assistant.git ~/jarvis-assistant && cd ~/jarvis-assistant
   ```
3. Install the `jarvis` command (drop `[voice]` for a text-only install):
   ```
   uv tool install --editable '.[voice]' --python 3.12
   ```
4. Run the setup wizard. It asks for your language, your name, your home town and your API key, then
   offers the optional features one by one:
   ```
   jarvis setup
   ```
5. Check everything and start chatting:
   ```
   jarvis doctor
   jarvis
   ```

Prefer pip? `python3.12 -m venv .venv && .venv/bin/pip install -r requirements-voice.txt`, then
`.venv/bin/jarvis setup`. Every step, with what you should see after it, is in
[MANUAL_STEPS.md](MANUAL_STEPS.md).

## Features

| Area | What you get | Guide |
|---|---|---|
| Voice | "Hey Jarvis" wake word, push-to-talk hotkey, on-device speech recognition, a natural voice with ElevenLabs or the Mac's own voice | [voice](docs/integrations/voice.md), [ElevenLabs](docs/integrations/elevenlabs.md) |
| Telegram | Your assistant on the phone, only for your account, with a reduced tool set and a `/stop` kill switch | [telegram](docs/integrations/telegram.md) |
| Calendar & Mail | Apple Calendar and Reminders, Google Calendar, Gmail search and drafts | [apple](docs/integrations/apple.md), [google](docs/integrations/google.md) |
| Spotify | Play songs, artists and playlists in the Spotify app | [spotify](docs/integrations/spotify.md) |
| Maps | Places nearby, opening hours, travel times, when to leave | [maps](docs/integrations/maps.md) |
| Sheets | Read and update Google Sheets; totals and filters are computed in code, not guessed | [google](docs/integrations/google.md) |
| Timers & Alarms | Timers that ring with the chat closed; alarms that ring until you stop them | [MANUAL_STEPS.md](MANUAL_STEPS.md) |
| Morning brief | Agenda, weather, mail, reminders and the match of the day, once a day | [MANUAL_STEPS.md](MANUAL_STEPS.md) |
| Home | Internet speed test, "am I home", "who is home" on your Wi-Fi | [presence](docs/integrations/presence.md) |
| Dashboard | `jarvis dashboard`: health, costs, logs, audit and settings in the browser, on this Mac only | [MANUAL_STEPS.md](MANUAL_STEPS.md) |

[IMAGE: dashboard screenshot]

[IMAGE: Telegram screenshot]

## English & Turkish

`jarvis setup` starts with "Press 1 for English / 2 için Türkçe". The main language picks Jarvis's
persona, notifications and voice defaults; Jarvis always answers in the language of your latest message.
Commands stay English in both: `jarvis doctor`, `/location`. Switch later with `jarvis setup --language`.

## Security & privacy

- **Local first.** Profile, memory, transcripts, logs, audio and speech recognition stay on the Mac.
  Only what a request needs goes to Anthropic (and to ElevenLabs, Google or Telegram if you set them up).
- **Asks first.** Running a command, writing a file, creating an event, a draft or a reminder all wait
  for your confirmation; the default answer is no.
- **Prompt-injection defenses.** Anything read from outside (email, web, calendar invites, files) is
  marked untrusted and can never trigger an action on its own.
- **Drafts only.** Jarvis has no permission to send email, in code and in Google.
- **Secrets in the Keychain.** Keys and tokens never touch a file or a log; the dashboard listens on
  127.0.0.1 only.

Details: [docs/privacy.md](docs/privacy.md) and [SECURITY.md](SECURITY.md).

## Costs

The Anthropic API is billed per use; a normal day is cents. The daily budget (`budget.daily_budget_usd`,
3 USD by default) is for your information: when today's cost reaches it, Jarvis tells you once and the
dashboard shows the cost in red, but it keeps answering. `jarvis usage` shows the spend. ElevenLabs and Google Maps are optional
and have their own plans. See [docs/costs.md](docs/costs.md).

## Documentation

- [HELPFILE.md](HELPFILE.md): every command, tool, Telegram command and voice phrase
- [MANUAL_STEPS.md](MANUAL_STEPS.md): the setup checklist, required steps first
- [docs/](docs/README.md): architecture, every configuration key, integrations, privacy, costs,
  troubleshooting
- [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md), [SECURITY.md](SECURITY.md)

To remove Jarvis: `jarvis uninstall` (asks before deleting keys and data), then
`uv tool uninstall jarvis` and delete the folder.

## License

TBD
