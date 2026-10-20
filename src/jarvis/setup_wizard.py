"""`jarvis setup`: the first-run wizard, safe to run again.

Order: platform check, language, the files every install needs, name and home (only while profile.md is
new), the Anthropic API key (hidden prompt, stored in the Keychain), the daily budget, the optional
integrations one by one (each can be skipped), then `jarvis doctor`.

Files are created from the templates and never overwritten:
- config.toml from config.example.toml (comments kept). An existing config.toml stays; it only receives the
  keys it lacks, after a backup under data/backups/<stamp>/ (the dashboard's History lists and restores it).
- data/profile.md from the template of the chosen language, filled with the answers.
- data/memory.md empty, data/vocabulary.md and data/corrections.md with a short comment header only: the
  sample lines of the .example templates are documentation and never reach a real user's files.
- data/, logs/ and data/backups/ are created owner-only (0700), the personal files 0600.
- `.env` is never created: secrets go to the Keychain.

`jarvis setup --language` only switches the main language. `jarvis setup --answers FILE` runs without
questions (tests, scripted installs): a TOML file with `language`, `name`, `address_as`, `home_location`
and optionally `daily_budget_usd`; it stores no secret and skips every integration.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import tomlkit
from rich.console import Console
from rich.prompt import Confirm, Prompt

from jarvis import i18n
from jarvis.i18n import t
from jarvis.utils import platform_check, secrets
from jarvis.utils.timeutil import now_iso, now_local

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
EXAMPLE_CONFIG_NAME = "config.example.toml"
DIR_MODE = 0o700
FILE_MODE = 0o600

LANGUAGE_PROMPT = "Choose your language / Dilinizi seçin:\nPress 1 for English\n2 için Türkçe"
LANGUAGE_CHOICES = {"1": "en", "2": "tr", "en": "en", "tr": "tr", "english": "en", "türkçe": "tr", "turkce": "tr"}

# The football calendar the default [[calendar.filters]] entry narrows to the configured team.
FOOTBALL_CALENDAR = {"en": "Football", "tr": "Futbol"}

# Words that end the chat: "exit", "quit" and "bye" in every language, plus the main language's own.
EXIT_WORDS = {"en": ["exit", "quit", "bye"], "tr": ["exit", "quit", "bye", "çık", "çıkış"]}

INTEGRATIONS = ("google", "apple", "spotify", "telegram", "elevenlabs", "maps", "voice", "shortcuts")

# Every Keychain entry Jarvis may create (`jarvis uninstall` offers to remove them).
SECRET_NAMES = (
    secrets.ANTHROPIC_API_KEY,
    secrets.ELEVENLABS_API_KEY,
    secrets.GOOGLE_MAPS_API_KEY,
    "spotify_client_id",
    "spotify_client_secret",
    "telegram_bot_token",
    "football_api_key",
    "google_client_secret",
    "google_token",
)


@dataclass
class FileResult:
    path: Path
    status: str  # created | kept | updated


# --- prompts -------------------------------------------------------------------------------------------------


class Prompter:
    """Questions on the terminal (rich prompts, getpass for secrets)."""

    interactive = True

    def __init__(self, console: Console) -> None:
        self.console = console

    def ask(self, question: str, default: str = "") -> str:
        return (Prompt.ask(question, default=default or None, console=self.console) or "").strip()

    def confirm(self, question: str, default: bool = False) -> bool:
        return bool(Confirm.ask(question, default=default, console=self.console))

    def secret(self, question: str) -> str:
        return getpass.getpass(f"{question}: ").strip()


class AnswersPrompter(Prompter):
    """No questions: the answers file decides, everything else keeps its default, no secret, no integration."""

    interactive = False

    def __init__(self, console: Console, answers: dict[str, Any]) -> None:
        super().__init__(console)
        self.answers = answers

    def ask(self, question: str, default: str = "") -> str:
        return default

    def confirm(self, question: str, default: bool = False) -> bool:
        return False

    def secret(self, question: str) -> str:
        return ""


def read_answers(path: Path) -> dict[str, Any]:
    with Path(path).expanduser().open("rb") as fh:
        data = tomllib.load(fh)
    language = data.get("language", i18n.DEFAULT_LANGUAGE)
    if language not in i18n.LANGUAGES:
        raise ValueError(f"answers file: language must be one of {', '.join(i18n.LANGUAGES)}")
    return data


# --- files ---------------------------------------------------------------------------------------------------


def _make_dir(path: Path) -> bool:
    """Create `path` owner-only; an existing folder keeps its mode. True when it was created."""
    if path.is_dir():
        return False
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(DIR_MODE)
    return True


def _write_new(path: Path, text: str) -> None:
    """Write a file that does not exist yet, owner-only from the start (never overwrites)."""
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(text)
    path.chmod(FILE_MODE)


def example_config_path(home: Path) -> Path:
    """config.example.toml in the home, else in the repository of the editable install (a JARVIS_HOME
    outside the repository has no copy of its own)."""
    from jarvis.config import package_repo_root

    for base in (Path(home), package_repo_root()):
        if base is not None and (base / EXAMPLE_CONFIG_NAME).is_file():
            return base / EXAMPLE_CONFIG_NAME
    return Path(home) / EXAMPLE_CONFIG_NAME


def missing_keys(example: Any, user: Any, prefix: str = "") -> list[str]:
    """Dotted keys of `example` that `user` lacks (tables recursively; arrays of tables are left alone)."""
    out: list[str] = []
    for key, value in example.items():
        name = f"{prefix}{key}"
        if key not in user:
            out.append(name)
        elif isinstance(value, dict) and isinstance(user[key], dict):
            out += missing_keys(value, user[key], name + ".")
    return out


def fill_missing_keys(example_text: str, user_text: str) -> tuple[str, list[str]]:
    """The user's config with every key of the example it lacks added (with the example's comments), and
    the dotted names of the added keys. Existing values, comments and order stay as they are."""
    example = tomlkit.parse(example_text)
    user = tomlkit.parse(user_text)
    added = missing_keys(example, user)

    def merge(src: Any, dst: Any) -> None:
        for key, value in src.items():
            if key not in dst:
                dst[key] = value
            elif isinstance(value, dict) and isinstance(dst[key], dict):
                merge(value, dst[key])

    if added:
        merge(example, user)
    return tomlkit.dumps(user), added


def backup_files(paths: Any, files: list[Path], label: str) -> Path:
    """Copy `files` into data/backups/<stamp>/ with the manifest the dashboard's History reads."""
    _make_dir(paths.backups_dir)
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    folder = paths.backups_dir / stamp
    n = 1
    while folder.exists():
        folder = paths.backups_dir / f"{stamp}-{n}"
        n += 1
    folder.mkdir()
    folder.chmod(DIR_MODE)
    copied = []
    for source in files:
        if source.is_file():
            shutil.copy2(source, folder / source.name)
            copied.append(source.name)
    items = [{"area": "setup", "label": label, "section": "", "file": name, "old": "", "new": ""} for name in copied]
    (folder / "manifest.json").write_text(json.dumps({"at": now_iso(), "files": copied, "items": items}, indent=2), encoding="utf-8")
    return folder


def set_config_values(paths: Any, changes: dict[str, Any], *, backup_label: str | None = None) -> None:
    """Apply `dotted.key -> value` edits to config.toml with tomlkit (comments kept); a key that
    config.local.toml already overrides is written there instead, so the change takes effect."""
    from jarvis.dashboard import tomlwrite

    local_data: dict | None = None
    if paths.local_config_file.is_file():
        with paths.local_config_file.open("rb") as fh:
            local_data = tomllib.load(fh)
    by_file: dict[Path, dict[str, Any]] = {}
    for key, value in changes.items():
        by_file.setdefault(tomlwrite.target_file(paths.config_file, local_data, key), {})[key] = value
    if backup_label:
        backup_files(paths, list(by_file), backup_label)
    for target, edits in by_file.items():
        text = target.read_text(encoding="utf-8") if target.is_file() else ""
        target.write_text(tomlwrite.render_with(text, edits), encoding="utf-8")


def configured_language(config_file: Path) -> str | None:
    """`general.language` as written in config.toml (or its overlay), None when it is not set yet."""
    from jarvis.config import local_config_path

    language = None
    for path in (config_file, local_config_path(config_file)):
        try:
            with path.open("rb") as fh:
                value = tomllib.load(fh).get("general", {}).get("language")
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if value in i18n.LANGUAGES:
            language = value
    return language


def ensure_config(paths: Any, language: str) -> FileResult:
    """config.toml from the example, or the missing keys added to an existing one (after a backup)."""
    example = example_config_path(paths.home)
    example_text = example.read_text(encoding="utf-8")
    if not paths.config_file.exists():
        document = tomlkit.parse(example_text)
        document["general"]["language"] = language
        document["general"]["exit_words"] = EXIT_WORDS[language]
        if "voice" in document:
            document["voice"]["stt_language"] = language
        for flt in document.get("calendar", {}).get("filters", []):
            flt["calendar"] = FOOTBALL_CALENDAR[language]
        _write_new(paths.config_file, tomlkit.dumps(document))
        return FileResult(paths.config_file, "created")
    text, added = fill_missing_keys(example_text, paths.config_file.read_text(encoding="utf-8"))
    if not added:
        return FileResult(paths.config_file, "kept")
    backup_files(paths, [paths.config_file], "jarvis setup: added missing settings")
    paths.config_file.write_text(text, encoding="utf-8")
    return FileResult(paths.config_file, "updated")


def data_file_headers(language: str | None = None) -> dict[str, str]:
    """What each data file `jarvis setup` creates starts with. memory.md is truly empty: every line of it is a
    note (the <memory> section, `jarvis memory`, the dashboard), so even a comment would show up as one."""
    from jarvis.voice import corrections, vocabulary

    return {"memory.md": "", "vocabulary.md": vocabulary.file_header(language), "corrections.md": corrections.file_header(language)}


def ensure_data_files(paths: Any, language: str | None = None) -> list[FileResult]:
    """data/, logs/, data/backups/ and the empty data files; existing ones are kept untouched."""
    results: list[FileResult] = []
    for folder in (paths.data_dir, paths.logs_dir, paths.backups_dir):
        results.append(FileResult(folder, "created" if _make_dir(folder) else "kept"))
    for name, header in data_file_headers(language).items():
        target = paths.data_dir / name
        if target.exists():
            results.append(FileResult(target, "kept"))
        else:
            _write_new(target, header)
            results.append(FileResult(target, "created"))
    sounds = paths.sounds_dir
    if not sounds.is_dir():
        sounds.mkdir(parents=True, exist_ok=True)
        results.append(FileResult(sounds, "created"))
    return results


def profile_text(language: str, *, name: str, address_as: str, location: str) -> str:
    template = (TEMPLATES_DIR / f"profile.example.{language}.md").read_text(encoding="utf-8")
    return template.replace("{name}", name).replace("{address_as}", address_as or name).replace("{location}", location)


# --- the wizard ----------------------------------------------------------------------------------------------


def choose_language(prompter: Prompter, current: str | None) -> str:
    console = prompter.console
    while True:
        console.print(LANGUAGE_PROMPT)
        answer = prompter.ask(">", default={"en": "1", "tr": "2"}.get(current or "", "")).strip().casefold()
        if answer in LANGUAGE_CHOICES:
            return LANGUAGE_CHOICES[answer]
        console.print("1 / 2")


def ask_budget(prompter: Prompter, current: float) -> float:
    while True:
        answer = prompter.ask(t("setup.budget.question"), default=f"{current:g}")
        try:
            value = float(answer.replace(",", ".").lstrip("$"))
        except ValueError:
            prompter.console.print(t("setup.budget.not_a_number"))
            continue
        if value < 0:
            prompter.console.print(t("setup.budget.negative"))
            continue
        return value


def _print_files(console: Console, results: list[FileResult], home: Path) -> None:
    for result in results:
        if result.status != "kept":
            try:
                shown = result.path.relative_to(home)
            except ValueError:
                shown = result.path
            console.print(f"  {t('setup.files.' + result.status)}: {shown}")


def run_setup(
    home: Path,
    console: Console,
    *,
    language_only: bool = False,
    answers: dict[str, Any] | None = None,
    run_doctor: Callable[[], int] | None = None,
) -> int:
    """The whole wizard. Returns the exit code."""
    from jarvis.config import Paths, load_config
    from jarvis.exceptions import ConfigError

    reason = platform_check.unsupported_reason()
    if reason:
        console.print(platform_check.unsupported_message(reason))
        return 1
    paths = Paths(Path(home))
    prompter: Prompter = AnswersPrompter(console, answers) if answers is not None else Prompter(console)
    if not example_config_path(paths.home).is_file():
        console.print(f"{EXAMPLE_CONFIG_NAME} not found in {paths.home}")
        return 1

    current = configured_language(paths.config_file)
    if answers is not None:
        language = answers.get("language", current or i18n.DEFAULT_LANGUAGE)
    elif language_only or current is None:
        language = choose_language(prompter, current)
    else:
        language = current
    i18n.set_language(language)

    if language_only:
        if not paths.config_file.exists():
            console.print(t("cli.first_run.missing", what="config.toml") + " " + t("cli.first_run.hint"))
            return 1
        if language != current:
            set_config_values(paths, {"general.language": language}, backup_label="jarvis setup --language")
        console.print(t("setup.language.saved", language=i18n.LANGUAGE_NAMES[language]))
        return 0

    console.print(f"[bold]{t('setup.title')}[/]\n{t('setup.intro')}\n")
    results = [*ensure_data_files(paths, language), ensure_config(paths, language)]
    if results[-1].status != "created" and current != language:
        set_config_values(paths, {"general.language": language})

    profile_new = not paths.profile_file.exists()
    changes: dict[str, Any] = {}
    if profile_new:
        if prompter.interactive:
            console.print(f"\n[bold]{t('setup.profile.heading')}[/]")
        name = answers.get("name", "") if answers is not None else prompter.ask(t("setup.profile.name"))
        address_as = (
            answers.get("address_as", "")
            if answers is not None
            else prompter.ask(t("setup.profile.address_as"), default=name.split(" ")[0] if name else "")
        )
        location = answers.get("home_location", "") if answers is not None else prompter.ask(t("setup.profile.location"))
        _write_new(paths.profile_file, profile_text(language, name=name, address_as=address_as, location=location))
        results.append(FileResult(paths.profile_file, "created"))
        if location:
            changes["weather.fixed_location"] = location
    else:
        results.append(FileResult(paths.profile_file, "kept"))
        console.print(t("setup.profile.kept", path="data/profile.md"))
    console.print(f"\n[bold]{t('setup.files.heading')}[/]")
    _print_files(console, results, paths.home)

    try:
        current_budget = load_config(paths.config_file).budget.daily_budget_usd
    except ConfigError:
        current_budget = 3.0
    if answers is not None and "daily_budget_usd" in answers:
        changes["budget.daily_budget_usd"] = float(answers["daily_budget_usd"])
    elif prompter.interactive:
        console.print(f"\n[bold]{t('setup.key.heading')}[/]")
        _ask_api_key(prompter)
        budget = ask_budget(prompter, current_budget)
        if budget != current_budget:
            changes["budget.daily_budget_usd"] = budget
    if changes:
        set_config_values(paths, changes)

    if prompter.interactive:
        console.print(f"\n[bold]{t('setup.integrations.heading')}[/]\n{t('setup.integrations.intro')}")
        for name in INTEGRATIONS:
            _integration(name, prompter, paths)

    try:
        load_config(paths.config_file)
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/] {exc}")
        return 1
    console.print(f"\n{t('setup.done')}")
    if run_doctor is not None and (answers is None or answers.get("run_doctor", True)):
        console.print(f"\n[bold]{t('setup.doctor')}[/]")
        run_doctor()
    return 0


def _ask_api_key(prompter: Prompter) -> None:
    console = prompter.console
    if secrets.get_secret(secrets.ANTHROPIC_API_KEY):
        console.print(t("setup.key.present"))
        if not prompter.confirm(t("setup.key.replace"), default=False):
            return
    console.print(t("setup.key.where"))
    value = prompter.secret(t("setup.key.prompt"))
    if not value:
        console.print(t("setup.key.skipped"))
        return
    secrets.set_secret(secrets.ANTHROPIC_API_KEY, value)
    console.print(t("setup.key.stored"))


def _store_secret(prompter: Prompter, name: str, label: str) -> bool:
    value = prompter.secret(label)
    if not value:
        prompter.console.print(t("setup.integrations.nothing_stored"))
        return False
    secrets.set_secret(name, value)
    prompter.console.print(t("setup.integrations.stored", name=name))
    return True


def _integration(name: str, prompter: Prompter, paths: Any) -> None:
    """Explain one optional integration and set it up when the user says yes (default: skip)."""
    console = prompter.console
    console.print(f"\n[bold]{t(f'setup.integrations.{name}.title')}[/]  {t(f'setup.integrations.{name}.what')}")
    if not prompter.confirm(t("setup.integrations.ask"), default=False):
        return
    try:
        if name == "google":
            path = prompter.ask(t("setup.integrations.google.client_secret"))
            if path:
                from jarvis.utils import google_auth

                google_auth.login(Path(path).expanduser())
                console.print(t("setup.integrations.google.done"))
        elif name == "spotify":
            if _store_secret(prompter, "spotify_client_id", t("setup.integrations.spotify.client_id")):
                _store_secret(prompter, "spotify_client_secret", t("setup.integrations.spotify.client_secret"))
        elif name == "telegram":
            _store_secret(prompter, "telegram_bot_token", t("setup.integrations.telegram.token"))
        elif name == "elevenlabs":
            if _store_secret(prompter, secrets.ELEVENLABS_API_KEY, t("setup.integrations.elevenlabs.key")):
                set_config_values(paths, {"voice.tts_engine": "elevenlabs"}, backup_label="jarvis setup: ElevenLabs")
        elif name == "maps":
            _store_secret(prompter, secrets.GOOGLE_MAPS_API_KEY, t("setup.integrations.maps.key"))
        elif name == "voice":
            if importlib.util.find_spec("mlx_whisper") is None:
                console.print(t("setup.integrations.voice.missing"))
                return
            set_config_values(paths, {"general.use_voice": True}, backup_label="jarvis setup: voice")
        console.print(t(f"setup.integrations.{name}.next"))
    except Exception as exc:  # one integration failing must not end the wizard
        console.print(f"[red]{t('setup.integrations.failed', error=f'{type(exc).__name__}: {exc}')}[/]")
