# Privacy

Jarvis runs on your Mac. The thinking happens at Anthropic, so what you ask and what Jarvis needs to answer
goes there; everything else stays on the Mac unless you turn on an integration that needs a provider. This page
lists what stays, what leaves and to whom, how long things are kept, and how to delete everything.

## What stays on the Mac

All of this lives in the Jarvis folder (`$JARVIS_HOME`, by default the folder you cloned), in folders only your
user can read (0700, files 0600):

| What | Where |
|---|---|
| Your profile (name, home, preferences) | `data/profile.md` |
| Long-term memory | `data/memory.md` |
| Transcripts, tool calls, costs, timers, alarms, workouts, network tests, presence history | `data/jarvis.db` (SQLite) |
| Application log, audit log of every tool call and confirmation, token usage | `logs/jarvis.log`, `logs/audit.jsonl`, `logs/usage.jsonl` |
| Settings | `config.toml`, `config.local.toml` |
| Vocabulary and speech corrections | `data/vocabulary.md`, `data/corrections.md` |
| Exact coordinates of your Current Location | `data/location_state.json` (and `data/location_history.jsonl` only with `location.keep_history = true`) |
| Backups made by the dashboard's save flow | `data/backups/` |
| API keys and tokens | the macOS Keychain (service `jarvis`), never a file |

Also local:

- **Audio.** The wake word, the recording and the speech recognition run on the Mac (Apple's on-device Speech
  framework, or the local Whisper model). Recordings are kept in memory only and never written to disk unless
  you set `voice.save_audio = true` (then in `data/voice_audio/`, for debugging).
- **Presence.** Router and phone addresses, who is home and the arrival/departure history stay in
  `config.local.toml` and `data/jarvis.db`. They are never sent anywhere, and the presence tools are blocked
  from Telegram unless you allow them.
- **Location.** Coordinates never go into `profile.md`, logs or the model's context; only the place name at
  `location.profile_precision` does. The logs record the town, never coordinates.
- **The dashboard** listens on 127.0.0.1 only, opens with a one-time link and locks itself when idle.
- **Logs** mask API keys, tokens and email addresses.

## What leaves the Mac, and to whom

| Provider | What it receives | When |
|---|---|---|
| **Anthropic** (Claude API) | Your messages; the system prompt with the persona, the rules, your `profile.md` (including the Current Location line) and your `memory.md`; the recent conversation; and every tool result the model reads: emails, calendar events, reminders, files, web pages, clipboard text, screenshots you ask about, sheet ranges, place details. Web searches run on Anthropic's servers. | Every request. Always on. |
| **ElevenLabs** | The text of every spoken reply (`voice.tts_engine = "elevenlabs"`). With `voice.elevenlabs_use_for_tainted = false`, replies in turns that read email or web content are spoken by the Mac's `say` instead. If you choose ElevenLabs Scribe for recognition (`voice.stt_engine = "elevenlabs"` or `"hybrid"`, or `jarvis voice test-stt --cloud`), the **audio** of your requests goes there too, including Telegram voice notes. | Only when set up. |
| **Google** | Calendar, Gmail and Sheets requests with your own OAuth client (reads, drafts, events, sheet edits). With a Maps key: place searches, place ids, routes with the start point (your Current Location's coordinates) and addresses to geocode. | Only when set up. |
| **Telegram** | Everything you write or say to the bot and its replies, approval buttons, voice notes, and a location you share. It passes through Telegram's servers like any Telegram chat. | Only when set up. |
| **Spotify** | Your search terms ("play some jazz"). Playback commands go to the local Spotify app. | Only when set up. |
| **Open-Meteo** | The coordinates or place name of a weather question. No account. | Weather questions. |
| **ipinfo.io** (or your `location.ip_lookup_url`) | Your public IP address, to guess the city. | Only with `location.ip_fallback = true` (off by default). |
| **API-Football** | Team and league queries. | Only with a `football_api_key`. |
| **Web pages** | The `fetch_page` tool downloads a page directly from your Mac, like a browser would. | When Jarvis reads a page. |
| **Hugging Face, GitHub** | Plain model downloads (Whisper, wake word) on the first voice start. No personal data. | Once. |
| **Apple** | The `networkQuality` speed test contacts Apple's test servers. | Internet tests. |

Nothing is sent to the authors of Jarvis: there is no telemetry.

Each provider handles the data under its own terms; read the privacy policy of the ones you turn on. Email is
read and drafted only: Jarvis has no permission to send mail.

## How long things are kept

| Setting | Default | Effect |
|---|---|---|
| `logging.app_log_retention_days` | 14 | `logs/jarvis.log` rotates daily; older files are deleted. |
| `logging.audit_log_retention_days` | 90 | Older lines of `logs/audit.jsonl` are removed at start. |
| `general.save_transcripts` | `true` | Full messages, including tool results, go into `data/jarvis.db`. With `false` only the request, the final reply and the tool-call records are kept. |
| `voice.save_audio` | `false` | `true` keeps every recording in `data/voice_audio/`. |
| `screen.store_images` | `false` | Screenshots are not stored in the transcript, only a reference. |
| `location.keep_history` | `false` | `true` keeps a trail of locations. |
| `[[presence.people]] track_history` | per person | Keep arrivals and departures, or only the current state. |

`data/jarvis.db` and `logs/usage.jsonl` have no automatic expiry. You can forget a single memory with
`jarvis memory forget <text>` or by saying "forget that"; you can edit `profile.md`, `memory.md`,
`vocabulary.md` and `corrections.md` by hand, or in the dashboard.

## Delete everything

```
jarvis uninstall
```

It removes the background services, then asks before deleting the Keychain entries and, separately, before
deleting your local data (`config.toml`, `config.local.toml`, `data/`, `logs/`). Then:

```
uv tool uninstall jarvis
```

and delete the Jarvis folder. Also revoke what you granted outside the Mac:

- Google: <https://myaccount.google.com/permissions> > Jarvis > remove access, and delete the Cloud project if
  you like.
- Telegram: delete the bot in @BotFather (`/deletebot`).
- Anthropic, ElevenLabs, Google Maps, Spotify: delete the API keys or the app in each provider's console.
- macOS: remove the entries you added under System Settings > Privacy & Security.

## People in your household

The presence feature recognizes phones on your Wi-Fi. Only add people who agreed, tell them that their
arrivals and departures are stored on this Mac (unless you set `track_history = false` for them), and remove
them when they ask. See [presence](integrations/presence.md).

See also [SECURITY.md](../SECURITY.md) for the security model (confirmations, untrusted content, guards).
