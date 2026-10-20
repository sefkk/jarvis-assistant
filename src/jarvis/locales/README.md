# Locales

Every string a user reads or hears (CLI lines, notifications, Telegram messages, spoken sentences, dialog
buttons, the setup wizard) lives here, one folder per language: `en/` and `tr/`. Each folder has the same
files (`alarm.toml`, `cli.toml`, `telegram.toml`, `voice.toml`, ...). English is the reference.

`examples.toml` is special: the example phrases per tool that `scripts/gen_helpfile.py` puts into
`HELPFILE.md`.

## How it works

`jarvis.i18n` loads the files and flattens them into keys `<file stem>.<table>.<key>`:

```python
from jarvis.i18n import t, t_all, t_list

t("telegram.bot.stopped")  # the main language (general.language)
t("telegram.bot.turn_error", "en", error="timeout")  # a given language, {placeholders} filled
t_list("confirm.terminal.yes")  # a word list in the main language
t_all("power.words.cancel")  # the word list of every language (for parsing input)
```

Values are strings or lists of strings. A key missing in one language falls back to English. Parsers of
what the user says (stop words, confirmations) usually accept every language at once with `t_all`.

Not translated, ever: CLI subcommands (`jarvis doctor`), Telegram slash commands (`/stop`, `/help`, ...),
config keys, log lines, tool results for the model, the dashboard and `jarvis doctor` output. They are
English in every language.

## Adding a key

1. Add it to the right file in `en/` and in `tr/`, with the same `{placeholders}` and the same type.
2. Use it through `t(...)`, never a literal string in the code.

## Adding a language

1. Copy `en/` to a folder named after the language code and translate every value (keep the keys and
   placeholders).
2. Add the code to `LANGUAGES` and `LANGUAGE_NAMES` in `src/jarvis/i18n.py`, and to `LANGUAGES` and the
   `GeneralConfig.language` choices in `src/jarvis/config.py`.
3. Add `prompts/persona.<code>.md`, `prompts/rules.<code>.md` and `templates/profile.example.<code>.md`.
4. Update the language question of the setup wizard (`LANGUAGE_PROMPT` in `src/jarvis/setup_wizard.py`)
   and check the voice settings (`voice.stt_language`, `voice.tts_voice_*`).

## Tests

`tests/unit/test_i18n.py` fails when the catalogs differ in keys, value types or placeholders, when a
value is empty or when the folders do not hold the same files. `tests/unit/test_commands_are_english.py`
keeps the commands English; `tests/unit/test_helpfile.py` needs example phrases for every tool in every
language.
