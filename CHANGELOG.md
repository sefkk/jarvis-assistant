# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

## 1.0.0

The first public release.

- Chat in the terminal, hands-free voice ("Hey Jarvis", push-to-talk, on-device speech recognition, ElevenLabs
  or macOS speech) and a Telegram bot, all driving the same assistant.
- English and Turkish: a language choice in `jarvis setup`, persona and rules per language, every message
  localized, replies in the language of the user's latest message.
- `jarvis setup`: a first-run wizard that creates every file from templates and never overwrites; `jarvis doctor`
  health check; `jarvis uninstall`.
- Tools: Apple and Google calendars, Apple Reminders, Gmail (read and drafts only), Google Sheets, Google Maps,
  weather, web search, files, shell, apps, screen and clipboard, Spotify, Apple Shortcuts, timers, pomodoro,
  alarms, the morning brief, Mac power, internet test, home presence, a training log, memory and a delegate.
- Security: risk levels and confirmations, taint tracking of untrusted content, path, shell, URL and
  AppleScript guards, secrets in the macOS Keychain.
- A local dashboard (`jarvis dashboard`) for health, costs, logs, the audit trail and settings.
- pip requirements generated from `uv.lock`, ruff, a test suite that needs no network, keys or permissions.
