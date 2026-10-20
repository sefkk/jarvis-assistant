# Documentation

Start with the [README](../README.md) to install Jarvis, then come back here for the details.

## Using Jarvis

- [HELPFILE.md](../HELPFILE.md): every command, tool and example phrase (generated from the code).
- [MANUAL_STEPS.md](../MANUAL_STEPS.md): the steps only you can do by hand: accounts, keys, macOS permissions.
- [configuration.md](configuration.md): every setting in `config.toml`, with its default (generated from the code).
- [troubleshooting.md](troubleshooting.md): what to do when something does not work.
- [costs.md](costs.md): what Jarvis costs to run and how the budget works.
- [privacy.md](privacy.md): what stays on your Mac and what leaves it.

## Integrations

| Guide | Covers |
| --- | --- |
| [anthropic.md](integrations/anthropic.md) | the Anthropic API key (required) |
| [google.md](integrations/google.md) | Google Calendar, Gmail (read and drafts only), Google Sheets |
| [apple.md](integrations/apple.md) | Apple Calendar and Reminders |
| [spotify.md](integrations/spotify.md) | Spotify |
| [telegram.md](integrations/telegram.md) | the Telegram bot |
| [elevenlabs.md](integrations/elevenlabs.md) | ElevenLabs voices |
| [maps.md](integrations/maps.md) | Google Maps |
| [shortcuts.md](integrations/shortcuts.md) | Apple Shortcuts |
| [health.md](integrations/health.md) | Apple Health workouts |
| [voice.md](integrations/voice.md) | the hands-free voice service |
| [presence.md](integrations/presence.md) | the internet test, "am I home" and "who is home" |
| [location.md](integrations/location.md) | the Current Location |

## How it works

- [architecture.md](architecture.md): the parts of Jarvis and how one request flows through them.
- [SECURITY.md](../SECURITY.md): the security model and how to report a problem.
- The folders of the code have their own READMEs: [src/jarvis/](../src/jarvis/README.md),
  [tools](../src/jarvis/tools/README.md), [security](../src/jarvis/security/README.md),
  [observability](../src/jarvis/observability/README.md), [io](../src/jarvis/io/README.md),
  [interfaces](../src/jarvis/interfaces/README.md), [dashboard](../src/jarvis/dashboard/README.md),
  [prompts](../src/jarvis/prompts/README.md), [locales](../src/jarvis/locales/README.md),
  [eval](../eval/README.md), [tests](../tests/README.md), [scripts](../scripts/README.md).

## Contributing

- [CONTRIBUTING.md](../CONTRIBUTING.md): development setup, tests and conventions.
- [CHANGELOG.md](../CHANGELOG.md): what changed in each release.
- [CLAUDE.md](../CLAUDE.md): working notes for AI coding assistants.
- `images/`: screenshots and diagrams used by these pages.
