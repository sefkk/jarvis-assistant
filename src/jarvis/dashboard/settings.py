"""The Settings form, generated from the Pydantic config model: sections in a fixed order, one
field per scalar setting with a widget inferred from its type and constraints, English labels and help texts
from the metadata below, which services a change restarts, and which fields stay read-only (security settings,
allowlists, structured tables). Secrets are shown as "In Keychain" / "Missing" and never read into a page."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from jarvis.config import Config
from jarvis.dashboard.ui_text import UI
from jarvis.utils import secrets

VOICE, TELEGRAM, SCHEDULER = "voice", "telegram", "scheduler"
BOTH = (VOICE, TELEGRAM)

# Section key -> (title, services a change restarts). The scheduler reloads config on its own, so the sections only
# it uses apply immediately; the dashboard section applies at the next `jarvis dashboard`.
SECTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "models": ("Models", BOTH),
    "persona": ("Persona", BOTH),
    "voice": ("Voice", (VOICE,)),
    "voice.sounds": ("Voice sounds", (VOICE,)),
    "brief": ("Morning brief", ()),
    "brief.schedule": ("Morning brief schedule", ()),
    "calendar": ("Calendar", BOTH),
    "memory": ("Memory", BOTH),
    "budget": ("Budget", BOTH),
    "location": ("Location", BOTH),
    "weather": ("Weather", BOTH),
    "maps": ("Maps", BOTH),
    "telegram": ("Telegram", (TELEGRAM,)),
    "dashboard": ("Dashboard", ()),
    "general": ("General", BOTH),
    "loop": ("API loop", BOTH),
    "context": ("Context", BOTH),
    "logging": ("Logging", BOTH),
    "screen": ("Screen", BOTH),
    "training": ("Training", BOTH),
    "timers": ("Timers", BOTH),
    "alarm": ("Alarms", ()),
    "football": ("Football", BOTH),
    "sheets": ("Sheets", BOTH),
    "shortcuts": ("Shortcuts", BOTH),
    "network": ("Network", ()),
    "presence": ("Presence", ()),
    "security": ("Security (read-only)", ()),
}

# Fields the dashboard never edits: the security settings, allowlists and ids, and the structured
# tables that have their own commands.
READ_ONLY: frozenset[str] = frozenset(
    {
        "security.allowed_file_roots",
        "security.denied_paths",
        "security.shell_denylist",
        "security.shell_timeout_seconds",
        "telegram.allowed_user_id",
        "telegram.allowed_chat_id",
        "telegram.remote_allowed_tools",
        "presence.allow_remote",
        "presence.allow_remote_self",
        "presence.home_gateway_macs",
        "presence.people",
        "shortcuts.allowed",
        "calendar.filters",
        "budget.prices",
        "sheets.aliases",
        "general.exit_words",
        "voice.stt_hallucination_blocklist",
        "voice.stt_model_by_language",
        "location.ip_lookup_url",
        "football.provider",
    }
)

# Deprecated keys that are still read for old files but never offered in the form.
HIDDEN: frozenset[str] = frozenset({"general.language_default", "weather.default_location"})

# Label and help overrides; everything else gets a label from the key ("humor_level" -> "Humor level").
LABELS: dict[str, tuple[str, str]] = {
    "models.main_model": ("Main model", "The model that answers you."),
    "models.delegate_model": ("Delegate model", "The model the delegate tool uses for longer sub-tasks."),
    "models.main_thinking": ("Main model thinking", "Let the main model think before answering (slower, costs tokens)."),
    "models.main_effort": ("Main model effort", "How hard the main model works; with thinking off, at most high."),
    "models.delegate_thinking": ("Delegate thinking", "Let the delegate think before answering."),
    "models.delegate_effort": ("Delegate effort", "How hard the delegate works."),
    "models.max_output_tokens": ("Max output tokens", "The longest reply the main model may write."),
    "models.delegate_max_output_tokens": ("Delegate max output tokens", "The longest reply the delegate may write."),
    "persona.humor_level": ("Humor level", "0 none, 1 light, 2 playful, 3 very playful."),
    "general.language": (
        "Main language",
        "Jarvis replies in the language of your message; this one is the fallback and decides the persona, the notifications and the voice defaults.",
    ),
    "general.log_level": ("Log level", "How much goes into logs/jarvis.log."),
    "general.save_transcripts": ("Save transcripts", "Keep the full conversation in the database (the Logs page needs it)."),
    "general.use_voice": ("Voice enabled", "Allow the hands-free voice service to start."),
    "memory.auto": ("Automatic memory", "Jarvis saves durable facts about you on its own."),
    "memory.auto_notify": ("Notify on save", "Show a note when a fact was saved automatically."),
    "memory.end_of_session_pass": ("End-of-session pass", "One cheap call at the end of a session looks for missed facts."),
    "memory.idle_pass_min": ("Idle pass after (min)", "Run the pass after this many quiet minutes; 0 never."),
    "memory.max_facts_per_pass": ("Max facts per pass", "How many facts one pass may save."),
    "budget.daily_budget_usd": (
        "Daily budget (USD)",
        "For your information only: once today's cost reaches it, Jarvis tells you and the dashboard shows the cost in red;"
        " it never stops answering. 0 means no budget.",
    ),
    "budget.warn_at_fraction": ("Warn at", "Warn when this fraction of the daily budget is used."),
    "budget.cache_read_multiplier": ("Cache read price factor", "A cache read costs this fraction of an input token."),
    "budget.cache_write_multiplier": ("Cache write price factor", "A cache write costs this multiple of an input token."),
    "budget.elevenlabs_usd_per_1k_chars": ("ElevenLabs price per 1k characters", "Only for the cost charts."),
    "budget.maps_usd_per_call": ("Google Maps price per request", "Only for the cost charts."),
    "calendar.default_write_calendar": ("Default calendar for new events", ""),
    "calendar.apple_calendar_exclude": ("Apple calendars to hide", "Comma-separated calendar names."),
    "weather.location_source": ("Weather location", "current = where the Mac is, home = your profile, fixed = the place below."),
    "weather.fixed_location": ("Fixed weather location", ""),
    "voice.wake_word_model": ("Wake word model", ""),
    "voice.wake_word_threshold": ("Wake word threshold", "Lower triggers more easily (and more often by mistake)."),
    "voice.hotkey": ("Push-to-talk hotkey", ""),
    "voice.tts_engine": ("Speech engine", "ElevenLabs when a key is in the Keychain; say is always available."),
    "voice.tts_voice_tr": ("macOS voice (Turkish)", ""),
    "voice.tts_voice_en": ("macOS voice (English)", ""),
    "voice.tts_rate": ("macOS speech rate", "Words per minute for the say engine."),
    "voice.elevenlabs_voice_id": ("ElevenLabs voice id", ""),
    "voice.elevenlabs_model": ("ElevenLabs model", ""),
    "voice.elevenlabs_monthly_char_budget": ("ElevenLabs monthly characters", "Switches to say when used up; 0 disables the budget."),
    "voice.follow_up_mode": ("Follow-up listening", "Listen for a follow-up after a reply without the wake word."),
    "voice.follow_up_window_s": ("Follow-up window (s)", ""),
    "voice.confirm_phrases": ("Confirmation phrases", "Comma-separated words that approve a spoken confirmation."),
    "voice.pause_phrases": ("Pause phrases", "Comma-separated phrases that pause listening."),
    "voice.stt_engine": (
        "Speech recognizer",
        "apple: on-device, free. whisper: local model. elevenlabs: cloud, paid. hybrid: Apple, then the cloud when unsure. Whisper is always the fallback.",
    ),
    "voice.stt_hybrid_threshold": ("Hybrid confidence threshold", "Below this Apple confidence the hybrid recognizer asks the cloud."),
    "voice.preroll_ms": ("Pre-roll (ms)", "Audio kept from before the recording starts, so the first word is not cut off."),
    "voice.stt_language": (
        "Speech language",
        "tr or en: Whisper decodes in this language and skips its own detection; auto detects within Speech languages.",
    ),
    "voice.stt_languages": ("Speech languages", "Comma-separated language codes for speech to text (used by auto)."),
    "brief.include_travel": ("Include travel times", ""),
    "brief.include_mail": (
        "Include today's mail",
        "Sender and subject of what reached the inbox today; the brief names the important ones.",
    ),
    "brief.schedule.enabled": ("Automatic morning brief", "Deliver the brief once a day on its own."),
    "brief.schedule.weekday_time": ("Weekday time", "HH:MM"),
    "brief.schedule.weekend_time": ("Weekend time", "HH:MM"),
    "brief.schedule.channel": ("Delivery", "auto speaks it when you are at the Mac and sends it to Telegram otherwise."),
    "brief.schedule.late_until": ("Late until", "A missed brief is still delivered until this time."),
    "brief.schedule.active_within_min": ("Counts as present within (min)", "How recently you must have used the Mac for auto to speak."),
    "location.enabled": ("Current Location", "Detect where the Mac is for weather and routes."),
    "location.profile_precision": ("Precision in the profile", ""),
    "maps.open_with": ("Open routes in", ""),
    "maps.default_mode": ("Default travel mode", ""),
    "maps.leave_buffer_min": ("Leave buffer (min)", ""),
    "maps.daily_call_limit": ("Daily Google Maps requests", "0 disables the guard."),
    "telegram.enabled": ("Telegram enabled", ""),
    "telegram.confirm_timeout_min": ("Confirmation timeout (min)", ""),
    "telegram.max_messages_per_minute": ("Messages per minute", ""),
    "dashboard.port": ("Port", "Takes effect at the next jarvis dashboard."),
    "dashboard.idle_lock_min": ("Lock after (min)", "The page locks after this much time without clicks, typing or scrolling."),
    "dashboard.unlock": ("Unlock with", "click: one click. touch_id: the Mac's Touch ID or password."),
    "dashboard.doctor_refresh_s": ("Health refresh (s)", ""),
    "dashboard.backups_keep": ("Backups to keep", ""),
    "dashboard.correction_words": (
        "Correction words",
        "Comma-separated; a voice request starting with one of these counts as a correction in Analytics.",
    ),
    "alarm.phone_backup": ("Phone backup", ""),
    "alarm.volume": ("Alarm volume", "0 to 100."),
    "timers.notify_channels": ("Timer notifications", "Comma-separated: mac, telegram."),
}
TIME_FIELDS = frozenset({"brief.schedule.weekday_time", "brief.schedule.weekend_time", "brief.schedule.late_until"})
SLIDER_FIELDS = frozenset({"persona.humor_level"})
MODEL_FIELDS = frozenset({"models.main_model", "models.delegate_model"})
SECRETS: tuple[tuple[str, str], ...] = (
    (secrets.ANTHROPIC_API_KEY, "Anthropic API key"),
    (secrets.ELEVENLABS_API_KEY, "ElevenLabs API key"),
    (secrets.GOOGLE_MAPS_API_KEY, "Google Maps API key"),
    ("telegram_bot_token", "Telegram bot token"),
    ("spotify_client_id", "Spotify client id"),
    ("spotify_client_secret", "Spotify client secret"),
)


@dataclass
class FieldSpec:
    path: str  # "models.main_model"
    section: str  # "models" or "brief.schedule"
    key: str
    label: str
    help: str
    widget: str  # switch | number | slider | select | time | text | list | readonly
    value: Any  # current effective value
    choices: list[str] = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    read_only: bool = False
    kind: str = "str"  # bool | int | float | str | literal | list
    staged: Any = None
    has_staged: bool = False
    error: str | None = None

    @property
    def shown(self) -> Any:
        return self.staged if self.has_staged else self.value

    @property
    def shown_text(self) -> str:
        return display_value(self.shown)

    @property
    def restarts(self) -> tuple[str, ...]:
        return restarts_for(self.path)

    @property
    def slug(self) -> str:
        return self.path.replace(".", "-")


@dataclass
class Section:
    key: str
    title: str
    fields: list[FieldSpec]
    restarts: tuple[str, ...]

    @property
    def read_only(self) -> bool:
        return bool(self.fields) and all(f.read_only for f in self.fields)


def restarts_for(path: str) -> tuple[str, ...]:
    section = path.rsplit(".", 1)[0]
    while section:
        if section in SECTIONS:
            return SECTIONS[section][1]
        section = section.rsplit(".", 1)[0] if "." in section else ""
    return BOTH


def effect_text(restarts: tuple[str, ...]) -> str:
    if not restarts:
        return UI.APPLIES_IMMEDIATELY
    return UI.RESTARTS.format(service=", ".join(UI.SERVICE_LABEL.format(service=s) for s in restarts))


def label_for(path: str) -> tuple[str, str]:
    if path in LABELS:
        return LABELS[path]
    key = path.rsplit(".", 1)[-1]
    text = key.replace("_", " ")
    text = re.sub(
        r"\b(s|ms|min|px|usd|url|id|api|stt|tts|vad|ip)\b",
        lambda m: m.group(1).upper() if m.group(1) in ("usd", "url", "id", "api", "stt", "tts", "vad", "ip") else m.group(1),
        text,
    )
    return text[:1].upper() + text[1:], ""


def display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _constraints(info) -> tuple[float | None, float | None]:
    minimum = maximum = None
    for meta in info.metadata:
        name = type(meta).__name__
        if name == "Ge":
            minimum = meta.ge
        elif name == "Gt":
            minimum = meta.gt
        elif name == "Le":
            maximum = meta.le
        elif name == "Lt":
            maximum = meta.lt
    return minimum, maximum


def _kind(annotation) -> tuple[str, list[str]]:
    origin = get_origin(annotation)
    if annotation is bool:
        return "bool", []
    if annotation is int:
        return "int", []
    if annotation is float:
        return "float", []
    if annotation is str:
        return "str", []
    if origin is Literal:
        return "literal", [str(a) for a in get_args(annotation)]
    if origin is list:
        (inner,) = get_args(annotation) or (str,)
        if inner is str:
            return "list", []
        if get_origin(inner) is Literal:
            return "list", [str(a) for a in get_args(inner)]
    return "other", []


def build_sections(config: Config, staged: dict[str, Any] | None = None, errors: dict[str, str] | None = None) -> list[Section]:
    """Every section of the config model as form fields, with the current values and the staged ones."""
    staged = staged or {}
    errors = errors or {}
    sections: dict[str, Section] = {}

    def add_model(prefix: str, model: BaseModel) -> None:
        title, restarts = SECTIONS.get(prefix, (label_for(prefix)[0], BOTH))
        section = sections.setdefault(prefix, Section(prefix, title, [], restarts))
        for key, info in type(model).model_fields.items():
            path = f"{prefix}.{key}"
            if path in HIDDEN:
                continue
            value = getattr(model, key)
            if isinstance(value, BaseModel):
                add_model(path, value)
                continue
            kind, choices = _kind(info.annotation)
            label, help_text = label_for(path)
            minimum, maximum = _constraints(info)
            read_only = path in READ_ONLY or kind == "other" or prefix.startswith("security")
            spec = FieldSpec(
                path=path,
                section=prefix,
                key=key,
                label=label,
                help=help_text,
                widget="readonly",
                value=_plain(value),
                choices=choices,
                minimum=minimum,
                maximum=maximum,
                read_only=read_only,
                kind=kind,
            )
            if not read_only:
                spec.widget = _widget(path, kind, minimum, maximum, choices)
                if spec.widget == "slider":
                    spec.step = 1
                elif kind == "float":
                    spec.step = 0.01 if (maximum is not None and maximum <= 2) else 0.1
                elif kind == "int":
                    spec.step = 1
            if path in staged:
                spec.staged, spec.has_staged = staged[path], True
            spec.error = errors.get(path)
            section.fields.append(spec)

    for name in Config.model_fields:
        add_model(name, getattr(config, name))
    order = {key: n for n, key in enumerate(SECTIONS)}
    return sorted((s for s in sections.values() if s.fields), key=lambda s: (order.get(s.key, len(order)), s.key))


def _widget(path: str, kind: str, minimum, maximum, choices: list[str]) -> str:
    if path in TIME_FIELDS:
        return "time"
    if path in SLIDER_FIELDS:
        return "slider"
    if kind == "bool":
        return "switch"
    if kind == "literal":
        return "select"
    if kind in ("int", "float"):
        return "number"
    if kind == "list":
        return "list"
    return "text"


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def field_spec(config: Config, path: str) -> FieldSpec | None:
    for section in build_sections(config):
        for spec in section.fields:
            if spec.path == path:
                return spec
    return None


def coerce(spec: FieldSpec, raw: str | None) -> Any:
    """The form's text into the field's Python type. Raises ValueError with an English reason."""
    text = (raw or "").strip()
    if spec.kind == "bool":
        if text.lower() in ("on", "true", "1", "yes"):
            return True
        if text.lower() in ("off", "false", "0", "no", ""):
            return False
        raise ValueError("use on or off")
    if spec.kind == "int":
        try:
            value = int(text.replace(",", "."), 10) if "." not in text.replace(",", ".") else int(float(text.replace(",", ".")))
        except ValueError:
            raise ValueError("enter a whole number") from None
        _check_range(spec, value)
        return value
    if spec.kind == "float":
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            raise ValueError("enter a number") from None
        _check_range(spec, value)
        return value
    if spec.kind == "literal":
        if text not in spec.choices:
            raise ValueError("choose one of " + ", ".join(spec.choices))
        return text
    if spec.kind == "list":
        items = [part.strip() for part in re.split(r"[,\n]", text) if part.strip()]
        if spec.choices:
            bad = [i for i in items if i not in spec.choices]
            if bad:
                raise ValueError("allowed values: " + ", ".join(spec.choices))
        return items
    if spec.widget == "time" and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", text):
        raise ValueError("use HH:MM")
    return text


def _check_range(spec: FieldSpec, value: float) -> None:
    if spec.minimum is not None and value < spec.minimum:
        raise ValueError(f"must be at least {spec.minimum:g}")
    if spec.maximum is not None and value > spec.maximum:
        raise ValueError(f"must be at most {spec.maximum:g}")


def secret_status() -> list[dict[str, Any]]:
    """Name and presence of every secret; the value never leaves the Keychain."""
    out = []
    for name, label in SECRETS:
        try:
            present = bool(secrets.get_secret(name))
        except Exception:
            present = False
        out.append({"name": name, "label": label, "present": present, "status": UI.IN_KEYCHAIN if present else UI.MISSING})
    return out
