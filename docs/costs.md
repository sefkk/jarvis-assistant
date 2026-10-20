# Costs

Jarvis itself is free. What costs money are the services it calls: always the Anthropic API, and, if you
turn them on, ElevenLabs, Google Maps and API-Football. This page says what each one charges, how Jarvis
caps the spending, and where to see it.

Prices below were checked on the providers' pricing pages in October 2026. Prices change: the provider's page
is always right, this page may not be.

## What costs money

### Anthropic API (required)

Billed per token (roughly, per piece of a word) that the model reads and writes, per model:

| Model | Input, per million tokens | Output, per million tokens |
|---|---|---|
| `claude-haiku-5-5` (prompts up to 100,000 tokens) | $0.10 | $0.50 |
| `claude-sonnet-5-5` (the default) | $2 | $10 |
| `claude-opus-5-5` | $4 | $20 |

- Jarvis sends its instructions, tool descriptions, your profile and memory with every request (about 30,000
  tokens before your conversation). They are **cached**: a cache write costs 1.25x the input price, and
  re-reading the cache costs 0.1x (0.05x on Sonnet 5.5 and Opus 5.5). The cache lives about five minutes, so
  the first request after a pause costs more than the next ones.
- One question can be several API calls: each tool Jarvis uses (calendar, weather, mail ...) adds a round.
- **Web search** costs $10 per 1,000 searches on top of the tokens, and the search results are read as input
  tokens.
- Current prices: <https://platform.claude.com/docs/en/about-claude/pricing>.

### ElevenLabs (optional)

A monthly plan with an allowance of characters (the free plan has a small one). Every character Jarvis speaks
through ElevenLabs counts; the Flash/Turbo models (Jarvis's default) use the allowance at half the rate of the
Multilingual models. Choosing ElevenLabs Scribe for speech recognition is billed separately. Plans:
<https://elevenlabs.io/pricing/api>.

### Google Maps Platform (optional)

Billed per request, with a free number of requests per month **per product**. Calendar, Gmail and Sheets are
free; only Maps costs money.

| Jarvis feature | Google product (SKU) | Free per month | Then, per 1,000 |
|---|---|---|---|
| "find a pharmacy nearby" (`search_places`) | Text Search Enterprise | 1,000 | $35 |
| opening hours, phone, rating (`place_details`) | Place Details Enterprise | 1,000 | $20 |
| travel times (`travel_time`, driving with traffic or transit) | Compute Routes Pro | 5,000 | $10 |
| addresses to coordinates (`geocode`) | Geocoding | 10,000 | $5 |

Searches and details use the Enterprise level because Jarvis asks for ratings and opening hours. Geocoding and
place details are cached for 7 days. Prices: <https://developers.google.com/maps/billing-and-pricing/pricing>.

### API-Football (optional)

Only if you store a `football_api_key`; football questions are answered with web search without it. It has a
free plan with a daily request limit; check the plans on <https://www.api-football.com>.

### Free

Open-Meteo (weather), Telegram, Apple services, Spotify's search API, speech recognition on the Mac.

## Limits and the daily budget

| Setting | Default | What it does |
|---|---|---|
| `budget.daily_budget_usd` | 3.00 | Today's estimated Anthropic cost against this budget. It is informational, never a stop: when it is reached, Jarvis tells you once, the dashboard shows today's cost in red and `jarvis doctor` shows `today's budget` as a warning; everything keeps working. `0` means no budget. |
| `budget.warn_at_fraction` | 0.8 | A one-time warning when 80% of the budget is used. |
| `[budget.prices]` | Haiku, Sonnet, Opus 5.5 | USD per million tokens, used for every cost estimate. They **must match** the current Anthropic prices, and the model you use must be listed, or its cost counts as zero. |
| `budget.cache_read_multiplier` / `cache_write_multiplier` | 0.1 / 1.25 | Cache pricing. For Sonnet 5.5 and Opus 5.5 the real cache-read factor is 0.05, so the estimate is slightly high. |
| `maps.daily_call_limit` | 100 | Google Maps calls per day, all products together (cached answers do not count). When reached, Maps tools refuse until tomorrow. `0` turns it off. |
| `voice.elevenlabs_monthly_char_budget` | 10000 | Characters per month sent to ElevenLabs. When reached, Jarvis speaks with the Mac's `say` voice until the next month. Set it to your plan's allowance. |
| `budget.elevenlabs_usd_per_1k_chars`, `budget.maps_usd_per_call` | 0.22, 0.005 | Only for the dashboard's cost charts. Adjust them to your plans. |

Jarvis's estimate does not include web-search fees. For a hard limit, use the providers' own tools as well:
your Anthropic Console's usage and limit settings, a credit limit on the ElevenLabs key, and a budget alert under
Billing in Google Cloud. With `maps.daily_call_limit = 100`, a month of heavy place searching can go past the
1,000 free Enterprise requests; set it to about 30 to stay inside the free allowance.

## Where to see it

```
jarvis usage            # tokens and cost per day and model (last 7 days), plus ElevenLabs and Google Maps sections
jarvis usage --days 30
jarvis doctor           # "today's budget", "ElevenLabs usage", "Google Maps usage"
jarvis dashboard        # the Analytics page: daily cost per provider against the budget, tokens, cache use, prompt composition
```

Every API call is also a line in `logs/usage.jsonl`. The providers' own consoles are the bill that counts.

## Rough examples

Rough figures for orientation, not quotes; your real numbers are in `jarvis usage`.

- **A short question on Sonnet 5.5** ("what time is it?"), with a warm cache: about 1 cent. The first request
  after a few quiet minutes writes the cache again: about 7 to 8 cents.
- **A question with tools** ("what's on my calendar tomorrow and what's the weather?"): 2 to 3 API calls,
  a few cents.
- **A day of normal use on Sonnet 5.5** (20 to 40 requests, spread over the day): roughly 1 to 3 USD, which
  is why the default budget is 3 USD. On Haiku 5.5 the same day costs about a twentieth of that.
- **A web search**: 1 cent for the search, plus the tokens of the results.
- **ElevenLabs**: a typical spoken reply is 100 to 300 characters, so 10,000 characters last for 30 to 100
  replies.
- **Google Maps**: a "pharmacy nearby" search costs nothing inside the 1,000 free per month, then 3.5 cents.
