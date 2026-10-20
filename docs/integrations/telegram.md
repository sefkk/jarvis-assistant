# Telegram: Jarvis on your phone

A private Telegram bot lets you talk to Jarvis from anywhere: type or send voice messages, approve actions
with buttons, get timers, alarms and the morning brief on the phone. The bot runs on your Mac and fetches
messages from Telegram itself (long polling), so no port on the Mac is opened to the internet.

Safety built in: only one Telegram user in one private chat is answered (everyone else is ignored and
logged), the bot gets a reduced tool set (no shell, files, apps, screen, clipboard or Spotify), risky actions
ask with **Approve** / **Decline** buttons, messages are rate limited, and `/stop` switches the bot off from
any device.

## What you need

- A Telegram account on your phone.
- The Mac switched on and awake: when it sleeps, the bot cannot answer.
- For voice messages: the voice extra of the installation and `ffmpeg` (`brew install ffmpeg`).

Telegram itself is free. Everything you write to the bot passes through Telegram's servers (see
[privacy](../privacy.md)).

## Set it up

### 1. Create the bot with BotFather

1. In Telegram, open **@BotFather** (the official one has a blue check mark) and send `/newbot`.
2. Send a display name, for example `Jarvis`.
3. Send a username. It must end in `bot` (for example `ada_helper_bot`), 5 to 32 characters, letters,
   digits and underscores only. Usernames are public and cannot be changed later.
4. BotFather answers with the token, which looks like `123456789:AAH...`. Copy it. Anyone with the token
   controls the bot: never paste it into `config.toml`, a chat or a screenshot.
5. Recommended hardening, also in BotFather:
   - `/setjoingroups` > your bot > **Disable** (nobody can add the bot to a group).
   - `/setprivacy` > your bot > **Enable** (the bot would not read group chatter anyway).

### 2. Store the token

```
jarvis telegram set-token
```

Paste the token (nothing is shown). It is stored in the Keychain as `telegram_bot_token`.

### 3. Find your user id and chat id

```
jarvis telegram whoami
```

This waits up to two minutes for the first message. On the phone, open your bot (search its username), press
**Start** or send any text. The Mac prints:

```
First message from user id 123456789 (@ada) in chat 123456789 (private).
```

and the bot replies with the same two numbers. In a private chat both ids are the same number.

### 4. Turn the bot on in the config

In `config.toml` (or in `config.local.toml`, the personal overlay that is merged over it):

```toml
[telegram]
enabled = true
allowed_user_id = 123456789
allowed_chat_id = 123456789
```

The other keys (`confirm_timeout_min`, `max_messages_per_minute`, `remote_allowed_tools` ...) have safe
defaults; see [configuration](../configuration.md).

### 5. Test it in the foreground, then install the background service

```
jarvis telegram run          # Ctrl-C stops it
```

Send "hello" from the phone. When it answers, stop it with Ctrl-C and install the background service, which
starts at login and restarts after a crash:

```
jarvis telegram install
jarvis telegram status
```

Only one copy may poll a bot token at a time: do not run `jarvis telegram run` or `whoami` while the
background service runs (`jarvis telegram uninstall` stops it).

Optional: timer and pomodoro notifications on the phone too:

```toml
[timers]
notify_channels = ["mac", "telegram"]
```

## Using it

- Commands (English in every language): `/start`, `/help`, `/new` (fresh session), `/location` (share your
  location), `/mute [60m]` and `/unmute` (pause the Mac's voice listening), `/status`, `/lock`,
  `/sleep [seconds]` (Mac power), `/stop_alarm`, `/snooze [min]`, `/brief`, `/stop`.
- Voice messages are transcribed on the Mac; the bot first shows "🎙️ I heard: ..." and then answers. At most 10
  minutes or 20 MB.
- A forwarded message is treated as untrusted content: anything that would write or act after it asks for
  confirmation.
- From the phone Jarvis never uses the Mac's location. Share yours with `/location` for weather and travel
  times.

## The kill switch: "my phone was stolen"

1. From any device where you are logged into Telegram (another phone, Telegram Desktop, web.telegram.org),
   send `/stop` to the bot. It answers once and then ignores everything. The state survives restarts.
2. If you cannot reach Telegram: at the Mac run `jarvis telegram uninstall`, and in BotFather revoke the token
   (`/revoke`, or `/mybots` > your bot > API Token > Revoke current token). The old token stops working.
3. In Telegram on another device: Settings > Devices > terminate the lost device's session.
4. To switch the bot back on, both sides are needed, in this order: send `/start` to the bot from your account,
   then at the Mac run:

   ```
   jarvis telegram enable
   ```

   After a revoked token, store the new one first with `jarvis telegram set-token`.

## macOS permissions

The background service runs as the tool environment's Python. If you use calendars, reminders, Shortcuts or
location from the phone, that Python needs the same permissions your terminal has (see
[troubleshooting](../troubleshooting.md#which-app-gets-the-permission)).

## Check that it works

`jarvis doctor` shows `telegram enabled  enabled`, `telegram token  in Keychain`, `telegram ids  user and chat
id set`, `telegram agent` running, `telegram kill switch` not engaged and `telegram voice`. Then from the phone:

- `/help` lists the commands.
- "What's on my calendar tomorrow?"
- "Add a test event tomorrow at 15:00" shows Approve / Decline with the real arguments; press **Decline**.

## Common problems

| Problem | Fix |
|---|---|
| The bot does not answer at all | `jarvis telegram status`. Is the Mac awake? Is `enabled = true` and are both ids set? Messages from any other account are ignored on purpose (see `logs/audit.jsonl`). |
| `HTTP 409 ... Conflict: terminated by other getUpdates request` | Two copies poll the same token: a `jarvis telegram run` or `whoami` in a terminal next to the background service, or the bot running on a second Mac. Stop one. |
| `whoami` prints nothing | The background service took the message. `jarvis telegram uninstall`, run `whoami`, then `jarvis telegram install`. |
| The bot says it is stopped | The kill switch is engaged: `/start` from the phone, then `jarvis telegram enable`. |
| Voice messages: "need `uv sync --extra voice` and `brew install ffmpeg`" | Install `ffmpeg` and the voice extra ([voice](voice.md)). The first voice message downloads a speech model once. |
| "Access to Calendars was denied" only from the phone | Give the tool environment's Python the Calendars and Reminders permission, then `jarvis services reinstall`. |
| A tool is "off from the phone" | It is not in `telegram.remote_allowed_tools`. Add it only if you are sure it is safe remotely. |
