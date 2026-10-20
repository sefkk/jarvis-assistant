"""Current Location awareness.

Two different places exist and must never be confused:

- **Location** (home): the `Location` line the user wrote in `data/profile.md`. Permanent. Jarvis
  never changes it.
- **Current Location** (now): where the user is at the moment. Detected by Jarvis's own code at
  the start of every session (Shortcut "Jarvis Location" > CoreLocationCLI > IP geolocation), written
  into ONE marked, auto-managed block of profile.md at a configurable precision (never exact
  coordinates, because profile.md is sent to the API on every request), and kept exactly in a
  local state file for the tools (weather, maps). From Telegram the Mac's location is never taken
  as the user's: only a location the user shared in the chat counts there.

Modules: `model` (the dataclass, parsing and precision), `sources` (the three lookups), `store`
(the state file and the optional history), `service` (refresh, lazy refresh by age, what the tools
ask for, the CLI lines and the doctor checks).
"""
