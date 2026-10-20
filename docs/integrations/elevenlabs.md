# ElevenLabs: a natural voice

With ElevenLabs, Jarvis speaks its replies in a natural, expressive voice instead of the Mac's built-in
`say` voice. It is optional: without it (and whenever ElevenLabs fails, is too slow or the monthly budget is
used up) Jarvis falls back to `say`, so it never goes silent.

ElevenLabs can also recognize speech ("Scribe"); that is a separate, optional choice described in
[voice](voice.md).

## What you need

- The voice service set up first ([voice](voice.md)); ElevenLabs only replaces the speaking part.
- An ElevenLabs account and an API key: <https://elevenlabs.io>. There is a free plan with a small monthly
  character allowance; paid plans include more. Compare them on <https://elevenlabs.io/pricing/api>. The
  Flash and Turbo models (Jarvis's default is `eleven_flash_v2_5`) cost half as much per character as the
  Multilingual models, so a plan covers about twice as many characters with them.
- Privacy: the text of every spoken reply is sent to ElevenLabs. See [privacy](../privacy.md) and
  `elevenlabs_use_for_tainted` below.

## Set it up

1. Create an account at <https://elevenlabs.io> and log in.
2. Open your API keys: <https://elevenlabs.io/app/settings/api-keys> (or click your profile and look for
   **API Keys**). Click **Create API key**, name it `jarvis`.
3. Restricting the key is a good idea. Give it at least:
   - **Text to Speech**: access (speaking),
   - **Voices**: read (`jarvis voice list-voices`),
   - **User**: read (`jarvis doctor` shows your remaining quota with it; optional).
   A credit limit on the key is a second safety net.
4. Copy the key (it starts with `sk_`) and store it:

   ```
   jarvis voice set-elevenlabs-key
   ```

   `jarvis setup` offers the same step and switches `voice.tts_engine` to `"elevenlabs"` for you.
5. Pick a voice. In the ElevenLabs web app, add the voices you like to **My Voices** (the Voice Library has
   many in English and Turkish). Then list them with their ids:

   ```
   jarvis voice list-voices
   ```
6. Put the id and the engine into `config.toml`:

   ```toml
   [voice]
   tts_engine = "elevenlabs"
   elevenlabs_voice_id = "<voice_id from list-voices>"
   elevenlabs_model = "eleven_flash_v2_5"            # lowest latency, English and Turkish
   elevenlabs_monthly_char_budget = 10000            # your plan's monthly characters
   elevenlabs_use_for_tainted = true                 # false: replies that may quote email/web pages use `say`
   ```

   Set `elevenlabs_monthly_char_budget` to your plan's monthly characters for the model you use (or a little
   less). Jarvis counts every character it sends and switches to `say` for the rest of the month when the
   budget is reached, before ElevenLabs refuses. `0` turns the guard off.
7. Restart the voice service so it reads the new settings:

   ```
   jarvis services reinstall
   ```

Try a sentence without starting the service:

```
jarvis voice say "Hello, the meeting starts at 9:30."
```

`--dry-run` only prints the text after the spoken-form normalization (numbers, times and dates as words).

## macOS permissions

None beyond those of the voice service.

## Check that it works

`jarvis doctor` shows `ElevenLabs key  set in the Keychain`, `ElevenLabs voice id`, `ElevenLabs model`,
`ElevenLabs quota` (your plan's usage, when the key may read the user) and `ElevenLabs usage` (this month's
characters against your budget). In `jarvis voice run --foreground` every sentence prints `[elevenlabs]`;
`[say]` means it fell back. `jarvis usage` has an ElevenLabs section.

## Common problems

| Problem | Fix |
|---|---|
| Jarvis speaks with the Mac voice | Check `tts_engine = "elevenlabs"`, the key and `elevenlabs_voice_id` in `jarvis doctor`, then `jarvis services reinstall`. `logs/jarvis.log` and the `ElevenLabs usage` doctor line name the last fallback reason. |
| `BUDGET REACHED` / "monthly limit is reached" | Your `elevenlabs_monthly_char_budget` is used up: `say` until the next month, or raise the budget if your plan allows. |
| `ElevenLabs quota  could not fetch /v1/user/subscription` | The key may not read the user (restricted key) or the request timed out. Speaking still works; give the key **User: read** to see the quota. |
| HTTP 401 | Wrong or deleted key: create a new one and run `jarvis voice set-elevenlabs-key`. |
| Long pauses, then `say` | No audio arrived within `elevenlabs_first_chunk_timeout_s` (3 s). Slow network; after three failures in a row Jarvis keeps `say` for five minutes. |
| `list-voices` shows nothing | Add voices to **My Voices** in the ElevenLabs web app. |
| Numbers are read oddly | Check `jarvis voice say --dry-run "..."`. `elevenlabs_text_normalization` (`"on"`, `"auto"`, `"off"`) controls ElevenLabs' own number reading. |
