# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private reporting instead:
the repository's **Security** tab > **Report a vulnerability**. Describe what an attacker can do, the steps
to reproduce it, and the version (`jarvis --version`). You get an answer within a week; a fix is released
as soon as it is ready, and you are credited in the changelog unless you prefer not to be.

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | yes |
| older | no |

## Threat model

Jarvis acts for one person on their own Mac, with real permissions: calendars, mail drafts, files, shell
commands, apps. The main risk is that text Jarvis reads makes it act. The defenses are in code, not only in
the prompt.

**Untrusted input.** Email bodies, web pages, calendar invites, files, clipboard and screen text, shell output,
Shortcut output, a delegate's answer and forwarded Telegram messages can be written by anyone. Each such
tool result is sanitized (control characters, hidden HTML) and wrapped in `<untrusted source="...">`; the
rules tell the model that it is data, never instructions.

**Taint tracking.** As soon as untrusted content enters a turn, the turn is tainted. In a tainted turn every
action that changes something needs an explicit confirmation from the user, even ones that are normally
allowed (`security/confirm.py`). A voice turn that is tainted can only be confirmed with the on-screen
button, not by voice.

**Confirmations.** Every tool has a risk level: SAFE runs, CONFIRM asks, BLOCKED never runs. Running a shell
command, writing a file, creating an event, a reminder, a draft or a sheet row, sleeping the Mac and every
Shortcut marked "confirm" show the exact action and wait; the default answer is no. A declined action is not
retried another way.

**Guards.** `security/guards.py` refuses before anything runs: paths outside `security.allowed_file_roots` or
inside `security.denied_paths` (SSH keys, cloud credentials, the Keychain folder), shell commands on
`security.shell_denylist`, URLs that point into the local network (SSRF), unsafe AppleScript arguments.
Subprocesses always get an argument list, never a shell string.

**No sending.** Gmail is read plus drafts only; the OAuth scopes do not include sending. There is no shutdown
or restart tool.

**Remote access.** The Telegram bot answers one allowlisted user id and chat id, has a smaller tool set (no
shell, files, screen, clipboard or presence by default), rate-limits messages and has a `/stop` kill switch
that only the Mac can lift. The Mac's location is never used for a phone turn.

**Local-only dashboard.** `jarvis dashboard` listens on 127.0.0.1 only, opens with a one-time link that becomes
an HttpOnly session cookie, checks the Host header and a CSRF token, sends a self-only Content Security
Policy, locks itself after a few idle minutes and shows secrets only as "In Keychain" / "Missing".

**Secrets.** API keys and tokens live in the macOS Keychain (service `jarvis`), never in config files; log
channels mask anything that looks like a key or token. Each user creates their own Google OAuth client; the
repository contains no credentials.

**Local data.** Transcripts, memory, the profile, logs and the audit trail stay in `data/` and `logs/`
(owner-only permissions for a new install). See [docs/privacy.md](docs/privacy.md) for what goes to which
provider.

**Out of scope.** Someone with access to your unlocked Mac account, a malicious Python package installed into
Jarvis's environment, and the providers' own security (Anthropic, Google, Telegram, ElevenLabs).
