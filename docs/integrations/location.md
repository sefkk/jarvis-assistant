# Current Location

Jarvis keeps two places apart: your **home** (the `Location` line of `data/profile.md`, which you write) and
your **Current Location** (where the Mac is right now, which Jarvis detects). Weather, "nearby" searches and
routes use the Current Location first, so the answers fit where you actually are.

## How Jarvis finds it

At the start of every chat and voice session Jarvis looks the location up in the background (the prompt never
waits), and again before a weather or maps question when the last value is older than `max_age_min`. It tries,
in this order:

1. **The Shortcut "Jarvis Location"** (recommended): Apple's own location through the Shortcuts app.
2. **CoreLocationCLI** (optional): a small command-line tool, if installed.
3. **Your IP address** (off by default): a city-level guess from a web service, often wrong behind a VPN.

Jarvis then rewrites one marked line in `data/profile.md`:

```
<!-- jarvis:auto current_location START -->
- Current Location: Alfama, Lisbon (updated 2026-10-20 09:15, source: shortcut)
<!-- jarvis:auto current_location END -->
```

Only that block changes; the rest of the file is yours. `profile.md` is sent to the model with every request,
so it gets only the place name at the precision you choose. Exact coordinates stay on the Mac in
`data/location_state.json` for the weather and maps tools, and the logs record only the town.

On Telegram, Jarvis never uses the Mac's location (the Mac may be at home while you are out): share yours with
`/location`.

## What you need

- The Shortcuts app and Location Services on the Mac. No account, no cost.

## Set it up

### 1. Build the Shortcut

1. Open the Shortcuts app, click **+** to make a new Shortcut and name it exactly **Jarvis Location**.
2. Add the action **Get Current Location**.
3. Add a **Text** action with these six lines. For each value, insert the *Current Location* variable and click
   it to pick the detail named in brackets:

   ```
   lat: Current Location (Latitude)
   lon: Current Location (Longitude)
   street: Current Location (Street)
   neighbourhood: Current Location (District)
   city: Current Location (City)
   region: Current Location (Region)
   ```

   A `country: Current Location (Country)` line is optional. The labels on the left are what Jarvis reads;
   "neighborhood" and a decimal comma (`38,7139`) are fine too.
4. Add **Stop and Output** and choose the Text from step 3.
5. Run it once in the Shortcuts app with ▶. macOS asks "Shortcuts would like to use your current location":
   click **Allow**. The first run from the terminal would otherwise hang on that invisible dialog.
6. Test it from the terminal:

   ```
   shortcuts run "Jarvis Location" --output-path /tmp/location.txt && cat /tmp/location.txt
   ```

   You should see the six lines with your values.

### 2. Allowlist it

`config.example.toml` already contains this block, so a fresh `config.toml` has it. If yours does not, add it:

```toml
[[shortcuts.allowed]]
name = "Jarvis Location"
description = "Returns the Mac's current location (latitude, longitude, address) for Jarvis's Current Location"
risk = "safe"
remote = false
taints = false
```

Keep `remote = false`: the phone must never use the Mac's location.

### 3. Refresh

```
jarvis location refresh   # look it up now
jarvis location           # show it, with its source and age
```

Then `jarvis services reinstall`, so the voice service uses it too.

### Optional: CoreLocationCLI

A fallback for when the Shortcut is missing or fails:

```
brew install --cask corelocationcli
CoreLocationCLI -once -json
```

Allow the Location Services prompt on the first run.

### Optional: the IP fallback

Off by default. When on, Jarvis asks a web service (ipinfo.io by default) for the city of your public IP
address. It is approximate, marked as such, and shows the VPN's city behind a VPN.

```toml
[location]
ip_fallback = true
ip_lookup_url = "https://ipinfo.io/json"
```

### Other settings

```toml
[location]
enabled = true
profile_precision = "neighborhood"   # "city" | "neighborhood" | "street": what goes into profile.md
max_age_min = 60                     # older than this: looked up again before a weather or maps question
timeout_s = 5                        # per source
keep_history = false                 # true appends every new location to data/location_history.jsonl

[weather]
location_source = "current"          # "current" (Current Location, then home, then fixed_location) | "home" | "fixed"
```

## macOS permissions

| Permission | Where | Who |
|---|---|---|
| Location Services | System Settings > Privacy & Security > Location Services: Location Services on, and **Shortcuts** on | the Shortcuts app (it covers lookups from the chat and the voice service) |
| Location Services | same list | CoreLocationCLI, only if you use it |

## Check that it works

`jarvis doctor` shows `location shortcut  'Jarvis Location' in the Shortcuts app`, `location CoreLocationCLI`
and `current location  <town>, source shortcut, <age>`. `jarvis location` prints the source. In the chat: "What's
the weather like?" answers for where you are.

## Common problems

| Problem | Fix |
|---|---|
| `'Jarvis Location' is allowlisted but not in the Shortcuts app` | Build the Shortcut with exactly that name, or remove the block from `config.toml` if you do not want it. |
| `returned no usable coordinates` | The Text action lacks the `lat:` / `lon:` lines, or a variable shows the whole location instead of one detail. Run the terminal test and compare. |
| The Shortcut hangs or times out | It waits for the Location Services prompt: run it once in the Shortcuts app and click Allow. Check that Shortcuts is on under Location Services. |
| `current location  not detected yet` | Nothing worked yet (with the IP fallback off that is expected until the Shortcut exists). Run `jarvis location refresh` and read the error it prints. |
| The city is wrong | An IP-based location (the profile line says "approximate"). Build the Shortcut, or turn `ip_fallback` off. |
| The district and city look swapped | Shortcuts fills the details differently by country. Swap the details you pick in the Text action until `jarvis location` reads right. |
