# Apple Shortcuts

Every Shortcut you build in the Shortcuts app can become a Jarvis ability without new code: Focus modes,
Notes, Apple Music, HomeKit scenes, anything Shortcuts can do. Jarvis runs them with macOS's built-in
`shortcuts` command, and only the Shortcuts you list in `config.toml` can ever run.

Examples once the allowlist is filled:

- "Turn on Do Not Disturb" runs "Do Not Disturb On" (*Set Focus* > Do Not Disturb > On).
- "Add to my notes: buy milk" runs "Add to Notes" with "buy milk" as its input.
- "Turn off the living room lights" runs "Living Room Lights Off" (*Control Home*).

Jarvis also uses one Shortcut itself, "Jarvis Location", to learn where you are; building it is described in
[location](location.md).

## What you need

- The Shortcuts app (part of macOS). Nothing to install, no account.

## Set it up

### 1. Build a Jarvis-friendly Shortcut

1. **Name it clearly.** The name is what Jarvis runs and what you copy into the allowlist: short and unique
   ("Do Not Disturb On", "Add to Notes"). Names starting with `-` are refused.
2. **Text input (optional).** If the Shortcut should take something from you (a note, a search term), use the
   **Shortcut Input** variable in its actions, for example *Append to Note* with Shortcut Input as the text.
   In the Shortcut's details (the (i) button), make sure it can receive input. Jarvis passes your text as a
   plain text file.
3. **Text output (optional).** End with **Stop and Output** (or let the last action produce text). Jarvis reads
   that text and uses it in the answer; without output it reports "done". Keep it short.
4. **No questions if possible.** A Shortcut that asks something (*Ask for Input*, *Choose from Menu*) waits
   for an answer in the Shortcuts app; Jarvis gives up after `shortcuts.timeout_seconds` (30 s). Pass choices
   as text input instead.
5. **Test it in the terminal first.** If this works, it works from Jarvis:

   ```
   shortcuts run "Do Not Disturb On"
   echo "buy milk" > /tmp/in.txt && shortcuts run "Add to Notes" --input-path /tmp/in.txt --output-path /tmp/out.txt && cat /tmp/out.txt
   ```

   The first run of a Shortcut may ask for permissions (Notes, Home, Location ...). Run it once from the
   Shortcuts app with the ▶ button and click **Allow** so that later runs do not hang on a dialog.

### 2. Add it to the allowlist

List every Shortcut on this Mac, with the allowlisted ones marked:

```
jarvis shortcuts list
```

Then add one `[[shortcuts.allowed]]` block per Shortcut to `config.toml`:

```toml
[shortcuts]
timeout_seconds = 30

[[shortcuts.allowed]]
name = "Do Not Disturb On"                       # exactly as in the Shortcuts app
description = "Turns on Do Not Disturb focus"     # what the model matches your request against
risk = "safe"                                    # "safe" runs directly, "confirm" asks first
remote = true                                    # true: may also run from Telegram
taints = false                                   # true: the output is untrusted (web pages, mail, feeds)

[[shortcuts.allowed]]
name = "Add to Notes"
description = "Appends the given text to a note; pass the text as input"
risk = "confirm"
```

The fields:

- `name`: must match the Shortcuts app (case and extra spaces are forgiven). Duplicates are a config error.
- `description`: what it does and whether it takes input. The model picks Shortcuts by this text.
- `risk`: `safe` runs without asking; `confirm` (the default) shows the name and the exact input and waits for
  your approval. Use `confirm` for anything that sends, deletes, buys or is hard to undo.
- `remote`: only entries with `remote = true` can run from Telegram. Default `false`.
- `taints`: set it for Shortcuts whose output comes from outside (a web page, a feed, mail). Jarvis then treats
  the output as untrusted, and later actions in that turn ask first. Default `false`.

Rules that always apply:

- In a turn where Jarvis has already read untrusted content (email, a web page, the clipboard, a file),
  **every** Shortcut asks for confirmation, even `safe` ones. By voice only the on-screen button counts then.
- With an empty allowlist the Shortcut tools are hidden from the model.
- A Shortcut that was renamed or deleted shows up as missing in `jarvis doctor`, and Jarvis says so instead of
  failing silently.

### 3. Reload

The chat reads the config at start: restart `jarvis`. The background services (voice, Telegram) read it when
they start too:

```
jarvis services reinstall
```

## macOS permissions

Whatever the Shortcut's own actions need (Notes, Home, Location Services, Focus). macOS asks the first time a
Shortcut runs; answer in the Shortcuts app. Shortcuts started by the voice service or Telegram run as the tool
environment's Python, which may ask once more (see
[troubleshooting](../troubleshooting.md#which-app-gets-the-permission)).

## Check that it works

```
jarvis shortcuts list    # ✓ = allowlisted, ✗ = allowlisted but missing in the Shortcuts app
jarvis doctor            # "Shortcuts CLI  N Shortcut(s) on this Mac, M allowlisted" plus one line per entry
```

Then in the chat: "Turn on Do Not Disturb". A Shortcut that is not allowlisted ("run the Shortcut X") is
refused with "not in the allowlist".

## Common problems

| Problem | Fix |
|---|---|
| `Shortcut 'X'  missing: not in the Shortcuts app` | The name differs from the app. Copy it from `jarvis shortcuts list`. |
| `Shortcut 'Jarvis Location' missing` | `config.example.toml` allowlists it for [location](location.md). Build it, or delete that block if you do not want it. |
| Timeout after 30 s | The Shortcut waits for a dialog or a permission prompt. Run it once in the Shortcuts app and answer it; raise `timeout_seconds` for slow Shortcuts. |
| "not allowed from Telegram" | Set `remote = true` on that entry (only if it is safe from a phone). |
| It always asks for confirmation | The turn was tainted (Jarvis read an email, a page, the clipboard ...), or the entry has `risk = "confirm"`. |
| Changes to the allowlist are ignored | Restart the chat; `jarvis services reinstall` for the voice and Telegram services. |
| "N bytes of non-text output" | The Shortcut's last action returns a file or an image. End it with *Stop and Output* on a Text action. |
