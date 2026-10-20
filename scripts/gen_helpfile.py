"""Generate HELPFILE.md, the complete command, tool and phrase reference, from the code.

    uv run python scripts/gen_helpfile.py           # write HELPFILE.md
    uv run python scripts/gen_helpfile.py --check   # exit 1 when HELPFILE.md is stale

Everything comes from the code: the CLI from `jarvis.main.build_parser()`, the tools from the `jarvis.tools`
modules (the registry's own discovery rules, every tool listed even when `available()` hides it), the
Telegram profile from `telegram.remote_allowed_tools` in config.example.toml (matched by the registry),
the Telegram commands from `jarvis.interfaces.telegram_bot.COMMANDS`, the voice and chat words from the
config defaults and the locale word lists, and the example phrases from `src/jarvis/locales/<lang>/examples.toml`.
Only the CLI examples, the Telegram command descriptions, the module headings and the setup notes live
in this file. The output is deterministic: no timestamps, no paths, nothing from the Keychain.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import io
import pkgutil
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "HELPFILE.md"
EXAMPLE_CONFIG = ROOT / "config.example.toml"

# --- hand-written parts (checked against the code when the file is generated) -----------------------------

# Example invocations for the commands where the bare `jarvis <command>` is not illustrative or does not run.
# Every line must parse with build_parser(); every command and positional choice needs at least one example.
EXAMPLES: dict[str, list[str]] = {
    "": ["jarvis", "jarvis --debug", "jarvis --version"],
    "setup": ["jarvis setup", "jarvis setup --language", "jarvis setup --answers answers.toml --no-doctor"],
    "stats": ["jarvis stats", "jarvis stats --days 30"],
    "usage": ["jarvis usage", "jarvis usage --days 30"],
    "export": ["jarvis sessions", "jarvis export --session SESSION_ID --out session.md"],
    "sessions": ["jarvis sessions --limit 5"],
    "memory forget": ["jarvis memory forget old phone number"],
    "google login": ["jarvis google login --client-secret ~/Downloads/client_secret.json"],
    "health import": ["jarvis health import ~/Downloads/export.zip"],
    "scheduler run": ["jarvis scheduler run", "jarvis scheduler run --once"],
    "alarms cancel": ["jarvis alarms cancel 3", "jarvis alarms cancel all"],
    "alarms snooze": ["jarvis alarms snooze", "jarvis alarms snooze 15"],
    "alarms test": ["jarvis alarms test --seconds 10"],
    "brief": ["jarvis brief", "jarvis brief --date tomorrow", "jarvis brief --raw", "jarvis brief --send telegram"],
    "brief schedule": ["jarvis brief schedule", "jarvis brief schedule on", "jarvis brief schedule off"],
    "voice": [
        "jarvis voice",
        "jarvis voice status",
        "jarvis voice run --foreground",
        "jarvis voice install",
        "jarvis voice uninstall",
        "jarvis voice pause --for 1h",
        "jarvis voice resume",
        "jarvis voice calibrate --seconds 20",
        "jarvis voice debug-record --out ~/Desktop/jarvis-debug",
        "jarvis voice test-stt",
        "jarvis voice test-stt --compare data/stt_test/SESSION --verbose",
        "jarvis voice list-voices",
        "jarvis voice set-elevenlabs-key",
        'jarvis voice say "The meeting starts at 14:30" --dry-run',
        'jarvis voice say "Merhaba Ada" --language tr',
    ],
    "dashboard": ["jarvis dashboard", "jarvis dashboard --no-open --port 8765"],
    "network test": ["jarvis network test", "jarvis network test --quick"],
    "network history": ["jarvis network history", "jarvis network history --range month"],
    "calendar filters": ["jarvis calendar filters", "jarvis calendar filters --days 14"],
    "location": ["jarvis location", "jarvis location show", "jarvis location refresh"],
    "completion": ["jarvis completion zsh", "jarvis completion bash"],
    "key set": ["jarvis key set anthropic_api_key", "jarvis key set elevenlabs_api_key"],
    "key delete": ["jarvis key delete football_api_key"],
}

# Descriptions for the arguments that have no `help=` in build_parser(), keyed "<command> <option or name>".
# An entry whose argument gets its own help text in the code is reported as stale.
ARG_HELP: dict[str, str] = {}

# One line per Telegram command: (arguments, description). The command set itself comes from the code.
TELEGRAM: dict[str, tuple[str, str]] = {
    "start": ("", "Say hello; after /stop it is the first step to turn the bot back on (then `jarvis telegram enable` on the Mac)."),
    "help": ("", "What Jarvis can do from the phone, and these commands."),
    "new": ("", "Start a new session (Jarvis forgets the running conversation, not its memory)."),
    "location": ("", "Show the share-location button: remote weather and travel times then start from where you are."),
    "mute": ("[60m]", "Pause voice listening on the Mac, for a while or until /unmute."),
    "unmute": ("", "Resume voice listening on the Mac."),
    "status": ("", "The Mac's state: battery, idle time, screen lock (the `mac_status` tool)."),
    "lock": ("", "Lock the Mac's screen (the `lock_screen` tool)."),
    "sleep": ("[seconds]", "Put the Mac to sleep after a countdown of 3-120 s, with Approve / Decline buttons (the `sleep_mac` tool)."),
    "stop_alarm": ("", "Stop the alarm ringing on the Mac."),
    "snooze": ("[min]", "Snooze the ringing alarm (1-60 minutes)."),
    "brief": ("", "Send today's brief now."),
    "stop": ("", "Emergency off: the bot stops answering until /start here and `jarvis telegram enable` on the Mac."),
}

# Readable headings for the tool modules (`jarvis.tools.<module>`). A new module needs an entry here.
MODULE_TITLES: dict[str, str] = {
    "alarms": "Alarms",
    "apple_calendar": "Calendar: Apple",
    "apple_reminders": "Reminders (Apple)",
    "brief": "Daily brief",
    "calendar_merged": "Calendar: both calendars",
    "clipboard": "Clipboard",
    "delegate": "Delegate (a second model)",
    "dev": "Developer",
    "files": "Files",
    "football": "Football (API-Football)",
    "gmail": "Mail (Gmail)",
    "google_calendar": "Calendar: Google",
    "google_sheets": "Google Sheets",
    "listening": "Voice listening",
    "maps": "Maps (Google Maps)",
    "memory_tools": "Memory",
    "network": "Internet",
    "power": "Mac power",
    "presence": "Presence (who is home)",
    "screen": "Screen",
    "shell": "Shell",
    "shortcuts": "Apple Shortcuts",
    "spotify": "Spotify",
    "system": "System",
    "timers": "Timers and pomodoro",
    "training": "Training log",
    "weather": "Weather",
    "web": "Web",
}

# What a module needs before its tools work. Modules whose tools hide themselves (`available()`) must be here.
SETUP: dict[str, str] = {
    "football": "An API-Football key (`jarvis key set football_api_key`). Without it these tools are hidden and `web_search` answers.",
    "gmail": "Google sign-in (`jarvis google login`). Read and drafts only: Jarvis never sends email.",
    "google_calendar": "Google sign-in (`jarvis google login`).",
    "google_sheets": "Google sign-in (`jarvis google login`).",
    "maps": "A Google Maps API key (`jarvis key set google_maps_api_key`).",
    "presence": "`jarvis presence setup`, at home.",
    "shortcuts": "At least one Shortcut under `[[shortcuts.allowed]]` in config.toml; hidden while the list is empty.",
    "spotify": "The Spotify app; `search_and_play` also needs `spotify_client_id` and `spotify_client_secret` (`jarvis key set`).",
}

# How pynput key names read for people.
KEY_NAMES = {"alt": "Option", "cmd": "Command", "ctrl": "Control", "shift": "Shift", "space": "Space"}
RISK_LABELS = {"SAFE": "Safe", "CONFIRM": "Confirm", "BLOCKED": "Blocked"}


# --- helpers -------------------------------------------------------------------------------------------------


def cell(text: str) -> str:
    """Text for a Markdown table cell: one line, pipes escaped, angle brackets escaped outside code spans."""
    parts = " ".join(str(text).split()).split("`")
    for i in range(0, len(parts), 2):  # even parts are outside code spans
        parts[i] = parts[i].replace("<", "&lt;").replace(">", "&gt;")
    return "`".join(parts).replace("|", "\\|")


def code_list(words: list[str]) -> str:
    return ", ".join(f"`{w}`" for w in words)


def first_sentences(text: str, min_length: int = 60, max_length: int = 200) -> str:
    """The first sentence of a description, plus the second when the first is very short, cut at its colon when long."""
    sentences: list[str] = []
    depth, start = 0, 0
    text = " ".join(text.split())
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch in ".!?" and depth == 0 and text[i + 1 : i + 2] == " " and text[i + 2 : i + 3][:1].isupper():
            sentences.append(text[start : i + 1].strip())
            start = i + 2
    sentences.append(text[start:].strip())
    out = sentences[0]
    if len(out) > max_length:  # a long first sentence: keep its lead ("Read a range as a table: ...")
        depth = 0
        for i, ch in enumerate(out):
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if ch == ":" and depth == 0 and out[i + 1 : i + 2] == " ":
                return out[:i] + "."
    if len(out) < min_length and len(sentences) > 1:
        out = f"{out} {sentences[1]}"
    return out


def sentence(text: str) -> str:
    """argparse help as a sentence: capital first letter, final full stop."""
    text = " ".join(text.split())
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."


def hotkey_label(combo: str) -> str:
    keys = [k.strip("<>") for k in combo.split("+")]
    return " + ".join(KEY_NAMES.get(k, k.upper() if len(k) == 1 else k.capitalize()) for k in keys)


def load_toml(path: Path) -> dict:
    import tomllib

    with path.open("rb") as fh:
        return tomllib.load(fh)


# --- CLI -----------------------------------------------------------------------------------------------------


@dataclass
class Command:
    path: tuple[str, ...]
    help: str
    parser: argparse.ArgumentParser
    children: list["Command"] = field(default_factory=list)

    @property
    def key(self) -> str:
        return " ".join(self.path)

    @property
    def line(self) -> str:
        return " ".join(("jarvis", *self.path))


def walk(parser: argparse.ArgumentParser, path: tuple[str, ...] = (), help_text: str = "") -> Command:
    node = Command(path, help_text, parser)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {choice.dest: choice.help or "" for choice in action._choices_actions}
            for name, sub in action.choices.items():
                node.children.append(walk(sub, (*path, name), helps.get(name, "")))
    return node


def flatten(node: Command) -> list[Command]:
    out = [node]
    for child in node.children:
        out.extend(flatten(child))
    return out


def command_paths(node: Command) -> set[str]:
    """Every command path, positional choices included (`voice run`, `location refresh`)."""
    paths = set()
    for cmd in flatten(node):
        if cmd.path:
            paths.add(cmd.key)
        for action in cmd.parser._actions:
            if not action.option_strings and action.choices and not isinstance(action, argparse._SubParsersAction):
                paths.update(" ".join((*cmd.path, str(c))) for c in action.choices)
    return paths


def parses(line: str) -> bool:
    from jarvis.main import build_parser

    argv = shlex.split(line)[1:]
    if "--version" in argv:  # prints machine-specific values and exits; the flag itself is known
        return argv == ["--version"]
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            build_parser().parse_args(argv)
    except SystemExit:
        return False
    return True


def arg_metavar(action: argparse.Action) -> str:
    if action.metavar:
        return str(action.metavar)
    if action.choices:
        return "{" + ",".join(str(c) for c in action.choices) + "}"
    if action.option_strings:
        return action.dest.upper()
    return "command" if action.dest.endswith("_command") else action.dest


def usage(cmd: Command) -> str:
    parts = [cmd.line]
    for action in cmd.parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            names = "<command>" if len(action.choices) > 8 else "{" + ",".join(action.choices) + "}"
            parts.append(names if action.required else f"[{names}]")
            continue
        if action.option_strings:
            flag = action.option_strings[-1] if len(action.option_strings) > 1 else action.option_strings[0]
            text = flag if action.nargs == 0 else f"{flag} {arg_metavar(action)}"
            parts.append(text if action.required else f"[{text}]")
            continue
        name = arg_metavar(action)
        if action.choices and action.dest.endswith("_command"):
            name = "{" + ",".join(str(c) for c in action.choices) + "}"
        parts.append({"?": f"[{name}]", "*": f"[{name} ...]", "+": f"{name} [{name} ...]"}.get(str(action.nargs), name))
    return " ".join(parts)


def default_text(action: argparse.Action) -> str:
    if action.nargs == 0:
        return "off"
    if action.default is None or action.default is argparse.SUPPRESS:
        return ""
    return f"`{action.default}`"


def arg_key(cmd: Command, action: argparse.Action) -> str:
    """The ARG_HELP key of an argument: "brief --date", "key delete name"."""
    return " ".join((*cmd.path, action.option_strings[0] if action.option_strings else action.dest))


def argument_rows(cmd: Command) -> list[str]:
    choices = {
        str(c)
        for a in cmd.parser._actions
        if not a.option_strings and a.choices and not isinstance(a, argparse._SubParsersAction)
        for c in a.choices
    }
    rows = []
    for action in cmd.parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.option_strings:
            name = ", ".join(action.option_strings)
            if action.nargs != 0:
                name += f" {arg_metavar(action)}"
        else:
            name = f"<{'command' if action.dest.endswith('_command') else action.dest}>"
        help_text = " ".join((action.help or ARG_HELP.get(arg_key(cmd, action), "")).split())
        if "%(" in help_text:
            help_text = help_text % {"default": action.default, "prog": cmd.line}
        prefix = next((c for c in sorted(choices, key=len, reverse=True) if help_text.startswith(f"{c}: ")), None)
        if prefix:  # "say: the text to speak" -> "`say`: the text to speak."
            rest = help_text[len(prefix) + 2 :]
            description = f"`{prefix}`: {rest if rest.endswith('.') else rest + '.'}"
        else:
            description = sentence(help_text)
        if action.choices:
            description = f"{description} One of: {code_list([str(c) for c in action.choices])}.".strip()
        required = action.required if action.option_strings else action.nargs not in ("?", "*")
        if required:
            description = f"Required. {description}".strip()
        rows.append(f"| {cell(f'`{name}`')} | {cell(default_text(action))} | {cell(description)} |")
    return rows


def examples_for(cmd: Command) -> list[str]:
    lines = EXAMPLES.get(cmd.key)
    if lines is None:
        lines = [cmd.line] if parses(cmd.line) else []
    return lines


def render_cli(root: Command) -> list[str]:
    commands = flatten(root)
    out = ["## Command line", ""]
    out.append(
        "Every command is English in every language. Run `jarvis <command> --help` for the same text in the terminal. "
        "`jarvis` alone starts the chat."
    )
    out += ["", "| Command | What it does |", "|---|---|"]
    for cmd in commands[1:]:
        out.append(f"| `{cmd.line}` | {cell(sentence(cmd.help))} |")
    for cmd in commands:
        level = "###" if len(cmd.path) <= 1 else "####"
        out += ["", f"{level} `{cmd.line}`", ""]
        out.append(sentence(cmd.help) if cmd.path else "Without a command: the interactive chat. These options work before any command.")
        out += ["", "```", usage(cmd), "```"]
        rows = argument_rows(cmd)
        if rows:
            out += ["", "| Argument | Default | Description |", "|---|---|---|", *rows]
        lines = examples_for(cmd)
        if lines:
            out += ["", "Examples:" if len(lines) > 1 else "Example:", "", "```", *lines, "```"]
    return out


def check_cli(root: Command) -> None:
    """Every example parses; every command and positional choice has one; no stale example keys."""
    paths = command_paths(root)
    keys = {cmd.key for cmd in flatten(root)}
    stale = sorted(set(EXAMPLES) - keys)
    if stale:
        raise SystemExit(f"EXAMPLES names commands that do not exist: {stale}")
    arguments = {arg_key(cmd, a): a for cmd in flatten(root) for a in cmd.parser._actions}
    stale = sorted(key for key in ARG_HELP if key not in arguments or arguments[key].help)
    if stale:
        raise SystemExit(f"ARG_HELP entries for unknown arguments or ones with their own help text: {stale}")
    lines = [line for cmd in flatten(root) for line in examples_for(cmd)]
    bad = [line for line in lines if not parses(line)]
    if bad:
        raise SystemExit(f"examples that do not parse: {bad}")
    words = [shlex.split(line)[1:] for line in lines]
    missing = sorted(p for p in paths if not any(w[: len(p.split())] == p.split() for w in words))
    if missing:
        raise SystemExit(f"commands without an example: {missing}")
    for cmd in flatten(root)[1:]:
        if not cmd.help:
            raise SystemExit(f"`{cmd.line}` has no help text in build_parser()")


# --- tools ---------------------------------------------------------------------------------------------------


@dataclass
class ToolInfo:
    name: str
    module: str
    description: str
    risk: str
    taints: str
    telegram: bool
    hides_itself: bool


def discover_tool_classes() -> list[tuple[str, type]]:
    """(module, class) of every tool, the registry's rules without the `available()` filter."""
    from jarvis.tools.base import Tool
    from jarvis.tools.registry import SKIP_MODULES

    pkg = importlib.import_module("jarvis.tools")
    found = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name in SKIP_MODULES or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"jarvis.tools.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Tool or not issubclass(obj, Tool) or inspect.isabstract(obj):
                continue
            if obj.__module__ != module.__name__ or not getattr(obj, "name", None):
                continue
            found.append((info.name, obj))
    return found


def overrides(cls: type, method: str) -> bool:
    """True when a tool class defines `method` itself instead of inheriting Tool's."""
    from jarvis.tools.base import Tool

    for klass in cls.__mro__:
        if klass is Tool:
            return False
        if method in vars(klass):
            return True
    return False


def collect_tools(config, paths) -> list[ToolInfo]:
    from jarvis.tools.base import ToolContext
    from jarvis.tools.registry import ToolRegistry

    ctx = ToolContext.build(config, paths)
    registry = ToolRegistry()
    classes = discover_tool_classes()
    for module, cls in classes:
        registry.register(cls(ctx), module=module)
    remote = set(registry.subset_by_patterns(list(config.telegram.remote_allowed_tools)).names())
    tools = []
    for module, cls in classes:
        risk = RISK_LABELS[cls.risk.value]
        if overrides(cls, "risk_for"):
            risk += " or Confirm (per entry)" if cls.risk.value == "SAFE" else " (per entry)"
        if cls.confirm_when_tainted or overrides(cls, "tainted_confirm_reason"):
            risk += "; asks after untrusted content"
        taints = "Per call" if overrides(cls, "taints_for") else ("Yes" if cls.taints else "No")
        tools.append(
            ToolInfo(
                name=cls.name,
                module=module,
                description=first_sentences(cls.description),
                risk=risk,
                taints=taints,
                telegram=cls.name in remote,
                hides_itself=overrides(cls, "available"),
            )
        )
    return sorted(tools, key=lambda t: (MODULE_TITLES.get(t.module, t.module), t.name))


def examples(language: str) -> dict[str, list[str]]:
    from jarvis.i18n import LOCALES_DIR

    return load_toml(LOCALES_DIR / language / "examples.toml").get("tools", {})


def check_tools(tools: list[ToolInfo]) -> None:
    modules = {t.module for t in tools}
    names = {t.name for t in tools}
    if missing := sorted(modules - set(MODULE_TITLES)):
        raise SystemExit(f"tool modules without a heading in MODULE_TITLES: {missing}")
    if stale := sorted((set(MODULE_TITLES) | set(SETUP)) - modules):
        raise SystemExit(f"MODULE_TITLES / SETUP name modules that have no tools: {stale}")
    if hidden := sorted({t.module for t in tools if t.hides_itself} - set(SETUP)):
        raise SystemExit(f"modules whose tools hide themselves need a SETUP note: {hidden}")
    for language in ("en", "tr"):
        phrases = examples(language)
        if missing := sorted(names - set(phrases)):
            raise SystemExit(f"locales/{language}/examples.toml has no phrases for: {missing}")
        if stale := sorted(set(phrases) - names):
            raise SystemExit(f"locales/{language}/examples.toml names tools that do not exist: {stale}")


def render_tools(tools: list[ToolInfo]) -> list[str]:
    en, tr = examples("en"), examples("tr")
    out = ["## Tools", ""]
    out.append(
        "What Jarvis can do for you. You never call a tool by name: ask in your own words (English or Turkish) "
        "and the model picks the tool. **Risk**: Safe runs directly, Confirm asks you first (a dialog, a spoken "
        '"confirm" or the Telegram buttons), Blocked never runs. **Taints**: the tool returns outside content '
        "(mail, web pages, calendars), which is marked untrusted, and later actions in that turn may need your "
        "confirmation. **Telegram**: offered from the phone by default (`telegram.remote_allowed_tools`)."
    )
    out += ["", f"{len(tools)} tools in {len({t.module for t in tools})} groups.", ""]
    by_title: dict[str, list[ToolInfo]] = {}
    for tool in tools:
        by_title.setdefault(MODULE_TITLES[tool.module], []).append(tool)
    for title, group in by_title.items():
        out += ["", f"### {title}", ""]
        module = group[0].module
        if module in SETUP:
            out += [f"Needs: {SETUP[module]}", ""]
        out += ["| Tool | What it does | Risk | Taints | Telegram |", "|---|---|---|---|---|"]
        for tool in group:
            out.append(
                f"| `{tool.name}` | {cell(tool.description)} | {cell(tool.risk)} | {tool.taints} | {'Yes' if tool.telegram else 'No'} |"
            )
        out += ["", "| Tool | Say (English) | Söyle (Türkçe) |", "|---|---|---|"]
        for tool in group:
            say_en = "<br>".join(cell(f"“{p}”") for p in en[tool.name])
            say_tr = "<br>".join(cell(f"“{p}”") for p in tr[tool.name])
            out.append(f"| `{tool.name}` | {say_en} | {say_tr} |")
    return out


# --- Telegram and voice --------------------------------------------------------------------------------------


def render_telegram() -> list[str]:
    from jarvis import i18n
    from jarvis.interfaces.telegram_bot import COMMANDS

    if set(TELEGRAM) != set(COMMANDS):
        raise SystemExit(f"TELEGRAM must describe exactly telegram_bot.COMMANDS: {sorted(set(TELEGRAM) ^ set(COMMANDS))}")
    help_text = i18n.t("telegram.help.text", "en", team="")
    # The order of the help text's command list: the last mention of each command (unmentioned ones last, by name).
    order = {name: max((m.start() for m in re.finditer(rf"/{name}\b", help_text)), default=-1) for name in COMMANDS}
    ordered = sorted(COMMANDS, key=lambda n: (order[n] < 0, order[n], n))
    out = ["## Telegram commands", ""]
    out.append(
        "Send these to your bot from the phone (see docs/integrations/telegram.md). They are English in every language; "
        "anything else you type or say is a normal request."
    )
    out += ["", "| Command | What it does |", "|---|---|"]
    for name in ordered:
        args, description = TELEGRAM[name]
        shown = f"/{name} {args}".strip()
        out.append(f"| `{cell(shown)}` | {cell(description)} |")
    return out


def render_voice(config) -> list[str]:
    from jarvis import i18n
    from jarvis.config import Config
    from jarvis.setup_wizard import EXIT_WORDS

    voice_defaults = Config.model_fields["voice"].get_default(call_default_factory=True)  # the code's defaults
    wake = voice_defaults.wake_word_model.replace("_", " ").title()
    hotkey = voice_defaults.hotkey

    def words(key: str, language: str) -> str:
        return code_list(i18n.t_list(key, language))

    rows = [
        ("Wake word", f"“{wake}”", f"“{wake}”"),
        ("Push to talk (hold)", f"{hotkey_label(hotkey)} (`{hotkey}`)", f"{hotkey_label(hotkey)} (`{hotkey}`)"),
        ("End the chat (whole input)", code_list(EXIT_WORDS["en"]), code_list(EXIT_WORDS["tr"])),
        ("Refuse a confirmation", words("confirm.words.negation", "en"), words("confirm.words.negation", "tr")),
        ("Not enough to confirm (asked again)", words("confirm.words.vague", "en"), words("confirm.words.vague", "tr")),
        ("Stop a ringing alarm", words("alarm.words.stop", "en"), words("alarm.words.stop", "tr")),
        ("Snooze a ringing alarm", words("alarm.words.snooze", "en"), words("alarm.words.snooze", "tr")),
        ("Cancel a Mac sleep countdown", words("power.words.cancel", "en"), words("power.words.cancel", "tr")),
    ]
    out = ["## Voice and chat phrases", ""]
    out.append(
        "Fixed words Jarvis listens for, by language. The alarm, sleep and confirmation words of every language work "
        "at any time. The exit words are what `jarvis setup` writes to `general.exit_words` for the chosen language."
    )
    out += ["", "| Phrase | English | Türkçe |", "|---|---|---|"]
    out += [f"| {cell(name)} | {cell(en)} | {cell(tr)} |" for name, en, tr in rows]
    out += ["", "These lists are settings in config.toml and are heard in any language (defaults shown):", ""]
    out += ["| Phrase | Setting | Default |", "|---|---|---|"]
    out.append(f"| Approve a spoken confirmation | `voice.confirm_phrases` | {cell(code_list(voice_defaults.confirm_phrases))} |")
    out.append(f"| Pause listening | `voice.pause_phrases` | {cell(code_list(voice_defaults.pause_phrases))} |")
    out.append(f"| Wake word model | `voice.wake_word_model` | `{voice_defaults.wake_word_model}` |")
    out.append(f"| Push-to-talk key | `voice.hotkey` | `{hotkey}` |")
    return out


# --- the file ------------------------------------------------------------------------------------------------


def render() -> str:
    from jarvis import __version__, i18n
    from jarvis.config import Paths, load_config
    from jarvis.main import build_parser

    before = i18n.current_language()
    i18n.set_language("en")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            shutil.copy(EXAMPLE_CONFIG, home / "config.toml")
            paths = Paths(home)
            config = load_config(paths.config_file)
            tools = collect_tools(config, paths)
            root = walk(build_parser())
            check_cli(root)
            check_tools(tools)
            body = [
                "# Jarvis help",
                "",
                "<!-- Generated by scripts/gen_helpfile.py from the code. Do not edit by hand: run "
                "`uv run python scripts/gen_helpfile.py`. -->",
                "",
                f"The complete reference for Jarvis {__version__}: every command, every tool the model can use, the "
                "Telegram commands and the words Jarvis listens for. It is generated from the code, so it matches "
                "this version exactly. To change it, change the code (or `scripts/gen_helpfile.py`) and run "
                "`uv run python scripts/gen_helpfile.py`.",
                "",
                "Start with [README.md](README.md); the steps you do by hand are in [MANUAL_STEPS.md](MANUAL_STEPS.md), "
                "and the guides are in [docs/](docs/README.md).",
                "",
                "Contents: [Command line](#command-line) · [Tools](#tools) · [Telegram commands](#telegram-commands) · "
                "[Voice and chat phrases](#voice-and-chat-phrases)",
                "",
                *render_cli(root),
                "",
                *render_tools(tools),
                "",
                *render_telegram(),
                "",
                *render_voice(config),
            ]
    finally:
        i18n.set_language(before)
    text = "\n".join(body)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate HELPFILE.md from the code.")
    parser.add_argument("--check", action="store_true", help="exit 1 when HELPFILE.md is not up to date")
    args = parser.parse_args(argv)
    text = render()
    current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
    if args.check:
        if current != text:
            print("HELPFILE.md is stale: run `uv run python scripts/gen_helpfile.py`.", file=sys.stderr)
            return 1
        print("HELPFILE.md is up to date.")
        return 0
    if current != text:
        OUTPUT.write_text(text, encoding="utf-8")
        print("HELPFILE.md written.")
    else:
        print("HELPFILE.md is already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
