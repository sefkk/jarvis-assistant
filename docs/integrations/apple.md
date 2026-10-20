# Apple Calendar and Reminders

Jarvis reads and creates events in the macOS Calendar app and reads, creates and completes reminders in the
Reminders app. Everything iCloud syncs there (your iPhone's calendars and lists, subscribed calendars, even a
Google account added in System Settings > Internet Accounts) becomes available to Jarvis, and nothing
leaves the Mac except what you ask about.

## What you need

- Nothing to install and no account: Jarvis talks to Calendar and Reminders through Apple's EventKit
  framework on the Mac.
- The calendars and lists you want, set up in the Calendar and Reminders apps.

## Set it up

1. Run `jarvis` in your usual terminal app and ask "What's on my calendar today?".
2. macOS asks whether your terminal app (Terminal, iTerm, Ghostty ...) may access your calendars. Click
   **Allow Full Access**. ("Add events only" is not enough: Jarvis must read them.)
3. Ask "List my reminders." and allow Reminders the same way.

The permission belongs to the **app that runs Jarvis**, not to Jarvis itself. Start Jarvis from the same
terminal app every time, or allow each one you use.

### The background services need it too

The Telegram bot, the scheduler (alarms, morning brief) and the voice service run in the background under
launchd, as the Python of Jarvis's tool environment. macOS cannot show them a prompt, so give that Python
the permission yourself if you use calendars or reminders from the phone, by voice or in the morning brief:

1. Find the Python (details in [troubleshooting](../troubleshooting.md#which-app-gets-the-permission)):

   ```
   realpath "$(head -1 "$(realpath "$(which jarvis)")" | cut -c3-)"
   ```

   It prints something like `/opt/homebrew/Cellar/python@3.12/3.12.x/Frameworks/Python.framework/Versions/3.12/bin/python3.12`.
2. System Settings > Privacy & Security > **Calendars**: click **+**, press Cmd+Shift+G, paste the path, add
   it and set it to **Full Access**. Do the same under **Reminders**.
3. `jarvis services reinstall`.

### Optional settings

```toml
[calendar]
default_write_calendar = "apple"     # new events go to Apple Calendar ("google" by default)
apple_calendar_exclude = ["Work (Google)"]   # Apple calendars to skip, e.g. a Google account synced into Calendar

# Show only some events of a busy calendar (agenda, brief and list tools).
[[calendar.filters]]
calendar = "Sports"
mode = "include_only"
keywords = ["Lisbon FC", "LFC"]
```

`config.example.toml` ships one filter for a calendar called "Futbol" (football fixtures, only the
configured team's matches stay visible). It does nothing if you have no calendar with that name; change or
delete it as you like. `jarvis calendar filters` lists every filter and what it hides this week.

Alarms also create a backup reminder in a list called "Jarvis Alarm" (`[alarm] reminder_list`), so the
alarm reaches your iPhone through iCloud.

## macOS permissions

| Permission | Where | For whom |
|---|---|---|
| Calendars (Full Access) | System Settings > Privacy & Security > Calendars | the terminal app; the tool environment's Python for the background services |
| Reminders (Full Access) | System Settings > Privacy & Security > Reminders | the same |

## Check that it works

`jarvis doctor` shows `[ok  ] Apple Calendar access  full access granted` and the same for
`Apple Reminders access`. In the chat:

- "What's on my calendar tomorrow?"
- "Add a dentist appointment on Friday at 10." (asks for confirmation first)
- "Remind me to call Ada tomorrow at 9." then "List my reminders."

## Common problems

| Problem | Fix |
|---|---|
| `not granted yet: macOS will ask the first time ...` | Normal before the first use. Ask a calendar question in the chat. |
| `Access to Calendars was denied` | System Settings > Privacy & Security > Calendars: turn on your terminal app with Full Access (use **+** if it is not listed), then restart the terminal and Jarvis. |
| `write-only access` | In System Settings > Privacy & Security > Calendars (or Reminders), change the app's access to **Full Access**. |
| Works in the chat but not from Telegram, voice or the morning brief | The background Python has no permission. See "The background services need it too". |
| No prompt ever appears | Quit and reopen the terminal app. If it still does not ask, add it with **+** in System Settings. |
| Events appear twice | A Google calendar is synced into Calendar and also read through Google: add its name to `apple_calendar_exclude`. |
| An event is missing from the agenda | A `[[calendar.filters]]` entry hides it; check `jarvis calendar filters`. |
