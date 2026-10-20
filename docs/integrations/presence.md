# Home network: internet test, "am I home", "who is home"

Jarvis can test your internet connection ("why is the internet slow?") and tell whether you, or other people
in your household, are at home, by noticing their phones on the home Wi-Fi. Everything runs on the Mac
without extra hardware, accounts or `sudo`, and the presence data never leaves the Mac.

## What you need

- Nothing for the internet test: it uses macOS's own `networkQuality` speed test and simple checks of your
  router, DNS and a few public hosts.
- For presence: the Mac on your home network, the scheduler service installed (it runs the scans), and each
  tracked phone set to a **Fixed** private Wi-Fi address.
- The **consent** of every person whose phone you add (see below).

## Internet test

Ask "Is the internet OK?" or "Why is the internet slow?" in the chat, or from the terminal:

```
jarvis network test           # diagnostics plus a speed test (about 20 s; uses some data)
jarvis network test --quick   # diagnostics only
jarvis network history        # averages, worst times, failures
```

Optional periodic checks, run by the scheduler service:

```toml
[network]
periodic_check_min = 10         # quick check every 10 minutes; notifies only when the internet drops and returns (0 = off)
periodic_speedtest_hours = 0    # a full speed test every N hours (uses data; 0 = never)
```

To see the Wi-Fi name in the results, allow Location Services for your terminal app (System Settings > Privacy
& Security > Location Services); everything else works without it.

## "Am I home?" and "Who is home?"

### Set it up

1. Install the scheduler service if you have not yet (it also runs timers and alarms):

   ```
   jarvis scheduler install
   ```
2. On every iPhone you want to track: Settings > Wi-Fi > tap **(i)** next to your home network > **Private
   Wi-Fi Address** > **Fixed** (not Rotating; Off also works). With Rotating the phone's address changes and
   Jarvis loses it. If you change the setting, the phone reconnects with a new, stable address. Android phones
   have a similar "MAC address type" setting; choose the device MAC or a fixed random one.
3. At home, on the home Wi-Fi, with the phones awake and connected, run:

   ```
   jarvis presence setup
   ```

   It shows your router (the "gateway") and asks whether this is your home network; the router's address is
   how Jarvis recognizes home, not the Wi-Fi name. Then it lists the devices it finds with their maker. Pick
   your phone by number and type your name; then add other people's devices if you want. An iPhone may show
   as Apple or as a "Private/randomized address".
4. The setup writes the results into `config.local.toml` (personal, ignored by git, merged over
   `config.toml`):

   ```toml
   [presence]
   home_gateway_macs = ["aa:bb:cc:dd:ee:01"]

   [[presence.people]]
   name = "Ada"
   devices = ["aa:bb:cc:dd:ee:02"]
   is_me = true
   track_history = true
   ```
5. Reload the services: `jarvis services reinstall`.

Run the setup again when the router or a phone changes.

### Tell your household

Detecting someone's phone on your Wi-Fi tells Jarvis when they come and go. Add only people who agreed, and
tell them what is stored: with `track_history = true` their arrivals and departures are kept in
`data/jarvis.db` on this Mac. Set `track_history = false` for anyone who wants only a current "home / away"
state, and remove people from `config.local.toml` when they ask.

### Options

```toml
[presence]
scan_interval_s = 120          # how often the scheduler scans while the Mac is at home
away_after_min = 10            # unseen this long = away (phones sleep their Wi-Fi)
allow_remote = false           # presence tools from Telegram: off, so a stolen phone cannot learn the house is empty
allow_remote_self = false      # only "am I home" from Telegram
on_arrive_greet = false        # greet you by voice with a short brief when you arrive
on_leave_pause_voice = false   # pause voice listening while the Mac is away from home
notify_person_changes = false  # a notification when someone arrives or leaves
```

## macOS permissions

| Permission | Where | Who |
|---|---|---|
| Local Network | System Settings > Privacy & Security > Local Network | the scheduler service's Python (it scans in the background). The terminal app is exempt. macOS asks once after `jarvis scheduler install`; allow it. |
| Location Services (optional) | System Settings > Privacy & Security > Location Services | only to show the Wi-Fi name in the internet test |

Which Python: [troubleshooting](../troubleshooting.md#which-app-gets-the-permission).

## Check that it works

- `jarvis doctor`: `presence home network  configured (1 gateway MAC(s))`, `presence people  1 configured`,
  `presence last scan`, `networkQuality`, `network history`.
- `jarvis presence status`: the home network, every person with home/away and when they were last seen, and
  the last scan. `jarvis presence scan` runs one scan right away.
- In the chat: "Am I home?", "Who is home?", "How fast is the internet?".

## Common problems

| Problem | Fix |
|---|---|
| You show as away while at home | The phone's Private Wi-Fi Address is Rotating, or it changed after you switched it to Fixed: set Fixed and run `jarvis presence setup` again. A sleeping phone can also drop off Wi-Fi briefly; `away_after_min` hides that. |
| `Last scan: none yet` | The scheduler is not installed or not running: `jarvis scheduler install`, `jarvis services status`. |
| The scheduler sees nothing but `jarvis presence scan` in the terminal works | The scheduler's Python has no Local Network permission. Turn it on, then `jarvis services reinstall`. |
| `No default gateway found` during setup | The Mac is not on a network. Connect to the home Wi-Fi and run it again. |
| "not available from Telegram" | On purpose. `allow_remote_self = true` opens "am I home" only; `allow_remote = true` opens everything. |
| The speed test fails | `networkQuality` needs a working connection to Apple's test servers; `--quick` still runs the local checks. |
