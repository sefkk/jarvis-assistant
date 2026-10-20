# Prompts

The text the model reads at the start of every request. `context.load_prompts` picks the files for the
main language (`general.language`) and `context.build_system` puts them into the system prompt as
`<persona>` and `<rules>`, followed by `<user_profile>` and `<memory>`
(see [docs/architecture.md](../../../docs/architecture.md)).

## Files

- `persona.<language>.md` (`persona.en.md`, `persona.tr.md`): who Jarvis is and how a reply is built,
  with example exchanges in that language.
- `rules.md`: the behavior rules, shared by every language and written in English (tool use, honesty,
  security, calendars, memory, voice, ...).
- `rules.<language>.md` (`rules.en.md`, `rules.tr.md`): how users say the same things in that language.
  It is appended after `rules.md`.
- `data/persona.md` (outside the package, not in git): your own persona. When it exists it replaces the
  shipped persona for every language. The dashboard's Persona editor writes it; delete it to go back.

Jarvis replies in the language of the user's latest message whatever the main language is; the main
language only picks these files and the fallback language named in the profile block.

## Markers

Replaced in code before the text is sent:

| Marker | Where | Replaced with |
| --- | --- | --- |
| `[[HUMOR]]` | persona | the block for `persona.humor_level` (0 to 3, `context.HUMOR_LEVELS`); a persona without the marker gets the block appended |
| `[[TEAM]]` | persona and rules | `football.team` |
| `[[MEMORY_AUTO]]` | rules | whether the model saves facts on its own (`memory.auto`, `context.MEMORY_AUTO_ON` / `MEMORY_AUTO_OFF`) |

## Changing a prompt

- When a tool's contract changes (new arguments, a new confirmation, a new result shape), update
  `rules.md` in the same change.
- Keep `rules.md` in English and put language-specific phrasing into every `rules.<language>.md`.
- Edit `persona.en.md` and `persona.tr.md` together; both keep the `[[HUMOR]]` marker.
- The prompt is cached: a stable text keeps requests cheap, so avoid content that changes per request.
- Check the effect with the evals (see [eval/README.md](../../../eval/README.md)).

## Tests

`tests/unit/test_context.py` (markers, the shipped personas, language selection, `data/persona.md` wins)
and `tests/unit/test_memory_auto.py`.
