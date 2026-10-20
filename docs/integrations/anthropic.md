# Anthropic API (required)

Jarvis thinks with Claude, Anthropic's AI model, through the Anthropic API. Every answer, in the chat, by
voice or on Telegram, is one or more API calls, so Jarvis cannot work without an API key.

## What you need

- An account in the Claude Console (the Anthropic developer console) at <https://platform.claude.com>
  (the old address, console.anthropic.com, leads there too).
- A way to pay: the API is billed per use, separately from any Claude.ai chat subscription. See
  [costs](../costs.md) for what a normal day costs and how Jarvis caps it.

## Set it up

1. Open <https://platform.claude.com> and sign in or create an account.
2. Open the Billing page of the Console and add a payment method or buy credits. Without credits every
   request fails with an error about your credit balance.
3. Open **API keys** (<https://platform.claude.com/settings/keys>) and click **Create key**. Give it a name
   such as `jarvis`, choose an expiration and link it to yourself.
4. Copy the key. It starts with `sk-ant-` and is shown only once.
5. Store it in the macOS Keychain. `jarvis setup` asks for it; to set or replace it later:

   ```
   jarvis key set anthropic_api_key
   ```

   Nothing is shown while you paste; that is normal. The key goes to the Keychain (service `jarvis`), never
   into a file or a log.
6. Set a daily budget. `jarvis setup` asks for it (3 USD by default); it is `budget.daily_budget_usd` in
   `config.toml`.

### Pick the model (optional)

`config.toml`, section `[models]`:

```toml
[models]
main_model = "claude-sonnet-5-5"       # every answer
delegate_model = "claude-sonnet-5-5"   # the "think harder" helper for long tasks
```

`claude-haiku-5-5` is much cheaper and faster, `claude-opus-5-5` is the strongest and the most expensive.
Whatever you choose, its price must be listed under `[budget.prices]` (USD per million tokens), otherwise
Jarvis counts its cost as zero and the budget cannot protect you. The shipped prices were checked against
Anthropic's pricing page in October 2026; compare them with
<https://platform.claude.com/docs/en/about-claude/pricing> when Anthropic changes prices.

### Web search

Jarvis's `web_search` tool runs on Anthropic's servers and is billed per search on the same key. It is on
for every organization unless an administrator turned it off in the Console (Settings > Capabilities). If
web questions fail with "web search is not enabled", turn it back on there.

## macOS permissions

None.

## Check that it works

```
jarvis doctor
```

The second line must read `[ok  ] Anthropic API key  found` and `today's budget` shows the spend. Then run
`jarvis` and ask "what time is it?".

## Common problems

| Problem | Fix |
|---|---|
| `Anthropic API key  missing` | `jarvis key set anthropic_api_key`. For development only, an `ANTHROPIC_API_KEY=...` line in `.env` also works. |
| "authentication error" / 401 | The key was deleted, expired or mistyped. Create a new one and run `jarvis key set anthropic_api_key` again. |
| "credit balance is too low" | Add credits on the Console's Billing page. |
| 429 / "rate limit" or "overloaded" | Jarvis retries a few times by itself (`loop.api_max_attempts`). New accounts have low rate limits; they rise with usage. Wait a minute and try again. |
| The cost in `jarvis usage` is 0 | The model in `[models]` is missing from `[budget.prices]`. Add it. |
| Budget warnings | See [costs](../costs.md). |
