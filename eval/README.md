# Evals

The evals check how the model uses the tools: for a request, the right tools are called, the wrong ones
are not, confirmations appear where they should, and the reply says what it must. They run the real
`Assistant` with fixture-backed tools, in a fresh temporary home for every case, so your own `data/` and
`logs/` are never touched.

## Files

- `cases.yaml`: the cases. The schema is documented at the top of the file: `id`, `input`, `fixtures`,
  `expected_tools`, `forbidden_tools`, `expect_confirmation`, reply checks (`reply_must_contain`,
  `reply_must_not_contain`, `reply_max_chars`, `reply_max_sentences`), `follow_ups` for multi-turn cases,
  `channel`, `fail_tools`, `judge` questions, and memory checks.
- `run_eval.py`: the runner. It loads the complete test configuration (`tests/fixtures/config.test.toml`)
  and swaps the tools that read outside data for fixture classes (`FIXTURE_CLASSES`).
- `fixtures/`: the data the fake tools serve (emails, calendars, a web page, a sheet). All of it is
  synthetic; never put real mail, calendar or sheet contents here.
- `recordings/`: saved API responses for replay. Ignored by git: they are your own runs and can quote
  your prompts.

## Running

```bash
uv run python eval/run_eval.py                       # replay (default): no network, no cost
uv run python eval/run_eval.py --live                # real API calls; costs money
uv run python eval/run_eval.py --record              # real API calls, saved for later replays
uv run python eval/run_eval.py --live --case agenda_tomorrow --show
uv run python eval/run_eval.py --live --model claude-haiku-5-5 --model claude-sonnet-5-5
```

Replay uses `eval/recordings/<model>/<case>.json` and skips cases without a recording, so a fresh clone
replays nothing until you have run `--record` once. `--live` and `--record` need the Anthropic API key
(`jarvis key set anthropic_api_key`). Confirmations are answered No unless `--approve` is given.

## Adding a case

1. Add an entry to `cases.yaml` with a unique `id` and the tools it must and must not call.
2. If the case needs data, add a synthetic file to `fixtures/` and list it under `fixtures`.
3. A new tool that reads outside data needs a fixture class in `FIXTURE_CLASSES` in `run_eval.py`.
4. Run it with `--record` once, then with the default replay.

## Tests

`tests/unit/test_eval_runner.py` checks the runner and the case file without calling the API.
