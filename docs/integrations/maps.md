# Google Maps: places and travel times

With a Google Maps Platform key, Jarvis finds places ("is there a pharmacy open nearby?"), gives details
(address, opening hours, phone, rating), works out travel times and when to leave, and opens a route in Apple
Maps or Google Maps. Routes start from your Current Location ([location](location.md)), not from home, unless
you say "from home".

## What you need

- A Google Cloud project (the one from [google](google.md) is fine, or a new one) with a **billing account**
  linked. Google Maps Platform does not work without billing, even inside the free allowance.
- Three APIs enabled: **Places API (New)**, **Routes API**, **Geocoding API**.
- Cost: each Google Maps product has a free number of requests per month; above it Google charges per 1,000
  requests. Personal use usually stays inside the free allowance, and Jarvis has its own daily cap
  (`maps.daily_call_limit`, 100 calls by default). Details in [costs](../costs.md).

## Set it up

1. Open <https://console.cloud.google.com> and select (or create) your project.
2. Billing: Menu > **Billing**. Link a billing account to the project (create one with a payment card if you
   have none). While you are there, **Budgets & alerts** > **Create budget** with a small amount (for example 5
   USD) sends you an email before anything gets expensive.
3. Enable the APIs: Menu > **APIs & Services** > **Library**, search and **Enable** each of:
   - **Places API (New)** (not the legacy "Places API")
   - **Routes API**
   - **Geocoding API**
4. Create the key: Menu > **APIs & Services** > **Credentials** > **Create credentials** > **API key**
   (the Google Maps Platform section of the console has the same under **Keys & Credentials**). Copy it.
5. Restrict the key: click the new key to edit it.
   - **Application restrictions**: **None**. The requests come from your Mac, and an IP restriction breaks
     whenever your home IP changes.
   - **API restrictions**: **Restrict key**, then select exactly **Places API (New)**, **Routes API** and
     **Geocoding API**. **Save**. A leaked key can then be used for nothing else.
6. Store the key (`jarvis setup` offers the same):

   ```
   jarvis key set google_maps_api_key
   ```
7. Optional settings in `config.toml`:

   ```toml
   [maps]
   open_with = "apple"          # "apple" | "google": which app "show it on the map" opens
   default_mode = "transit"     # "transit" | "driving" | "walking" | "bicycling"
   leave_buffer_min = 10        # "when should I leave": minutes added to the travel time
   daily_call_limit = 100       # Maps calls per day, all APIs together; 0 turns the guard off
   ```

Write your home town or district on the `Location` line of `data/profile.md`: Jarvis uses it for "from home".

## macOS permissions

None for the key. Opening a route uses Apple Maps or your browser. For "nearby" and routes from where you are,
set up [location](location.md).

## Check that it works

`jarvis doctor` shows `Google Maps key  found in the Keychain (...)` and `Google Maps usage  N network call(s) of
100 today`. In the chat:

- "Is there a pharmacy open near me?"
- "How long does it take to get to Lisbon airport by public transport?"
- "When should I leave for my 3 pm meeting at Alexanderplatz?"
- "Show the route to the central station on the map."

`jarvis usage` lists Maps calls per day and per API.

## Common problems

| Problem | Fix |
|---|---|
| `no Google Maps API key` | `jarvis key set google_maps_api_key`. |
| `the Maps key is restricted or the API is not enabled` | One of the three APIs is not enabled, or the key's API restrictions miss it. Check both (Places API **(New)**, not the legacy one), wait a few minutes, try again. |
| "billing" / `REQUEST_DENIED` errors | No billing account is linked to the project. |
| `daily Google Maps call limit (100) reached` | Jarvis's own guard. Wait until tomorrow or raise `maps.daily_call_limit`. |
| Routes start from the wrong place | Jarvis did not know your Current Location and asked, or used "home". Set up [location](location.md), or name the start: "from Kadıköy to ...". |
| Results in the wrong city | Same cause: without a current location searches are biased to your profile's home `Location`. |
