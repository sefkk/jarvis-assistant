"""Load and validate config.toml into a Pydantic model."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jarvis.exceptions import ConfigError


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


LANGUAGES = ("en", "tr")


class GeneralConfig(_Section):
    # The main language: which persona and rules files are sent to the model, the language of notifications,
    # the setup wizard and the voice defaults. Replies still follow the language of the user's latest message;
    # this one is the fallback. `jarvis setup` asks for it; `jarvis setup --language` changes it.
    language: Literal["en", "tr"] = "en"
    language_default: str = ""  # the key before `language`; still read (as `language`) and flagged by `jarvis doctor`
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG"
    save_transcripts: bool = True
    use_voice: bool = False
    # The CLI exits when the whole input is exactly one of these (case-insensitive); Ctrl-D too.
    exit_words: list[str] = Field(default_factory=lambda: ["exit", "quit", "bye"], min_length=1)

    @model_validator(mode="after")
    def _migrate_language_default(self) -> "GeneralConfig":
        if "language" not in self.model_fields_set and self.language_default.strip() in LANGUAGES:
            self.language = self.language_default.strip()  # type: ignore[assignment]
        return self

    @property
    def deprecated_keys(self) -> list[str]:
        return ["general.language_default (use general.language)"] if self.language_default else []

    @model_validator(mode="after")
    def _exit_words_are_single_words(self) -> "GeneralConfig":
        cleaned = []
        for word in self.exit_words:
            word = word.strip().lower()
            if not word or any(ch.isspace() for ch in word):
                raise ValueError(f"exit_words entries must be single words (got {word!r})")
            cleaned.append(word)
        self.exit_words = cleaned
        return self


class PersonaConfig(_Section):
    """How Jarvis talks (prompts/persona.<language>.md, or your own data/persona.md). `humor_level` picks the frequency-and-style block that
    the persona's [[HUMOR]] marker expands to: 0 = none, 1 = light (now and then), 2 = playful (a quip
    in most casual replies, the default), 3 = very playful (every casual reply, bolder teasing). The
    persona's own exceptions (failures, serious topics, confirmations, hurry) hold at every level."""

    humor_level: int = Field(default=2, ge=0, le=3)


Effort = Literal["low", "medium", "high", "xhigh", "max"]

# Effort levels that the API accepts together with thinking turned off.
EFFORT_ALLOWED_WITH_THINKING_OFF = {"low", "medium", "high"}


class MemoryConfig(_Section):
    """Long-term memory (data/memory.md) beyond an explicit "remember this"."""

    auto: bool = True  # the model calls `remember` on its own for durable facts the user states about themselves
    auto_notify: bool = True  # "📝 Not aldım: …" in the terminal and on Telegram when something is saved automatically (voice: logged only)
    end_of_session_pass: bool = True  # one cheap main-model call when a session ends proposes facts the conversation missed
    idle_pass_min: int = Field(default=30, ge=0)  # the pass also runs for the turns before a gap of this many quiet minutes (0 = never)
    max_facts_per_pass: int = Field(default=5, ge=1, le=20)


class ModelsConfig(_Section):
    main_model: str
    delegate_model: str
    main_thinking: bool = False
    main_effort: Effort = "medium"
    delegate_thinking: bool = True
    delegate_effort: Effort = "high"
    max_output_tokens: int = Field(default=1024, ge=1)
    delegate_max_output_tokens: int = Field(default=4096, ge=1)

    @model_validator(mode="after")
    def _effort_matches_thinking(self) -> "ModelsConfig":
        for label, thinking, effort in (
            ("main", self.main_thinking, self.main_effort),
            ("delegate", self.delegate_thinking, self.delegate_effort),
        ):
            if not thinking and effort not in EFFORT_ALLOWED_WITH_THINKING_OFF:
                raise ValueError(f"{label}_thinking = false requires {label}_effort of low, medium or high (got {effort!r})")
        return self


class LoopConfig(_Section):
    """The Claude API loop. The timeouts are granular, never one short total limit: the SDK default of 5 s to
    connect (TCP + TLS) is too short on a slow network, while a long answer must never be cut. `api_connect_timeout_s` covers the TCP connect and the
    TLS handshake; `api_read_timeout_s` is the longest silence allowed between two chunks of the stream
    (a stalled stream), so a healthy long answer is never cut while a dead connection is noticed."""

    max_tool_iterations: int = Field(default=10, ge=1)
    api_max_attempts: int = Field(default=3, ge=1)
    api_backoff_base_seconds: float = Field(default=1.0, gt=0)
    api_connect_timeout_s: float = Field(default=10.0, gt=0, le=120)
    api_read_timeout_s: float = Field(default=60.0, gt=0, le=600)  # max silence between stream events
    api_write_timeout_s: float = Field(default=60.0, gt=0, le=600)  # sending the request body


class ContextConfig(_Section):
    compact_after_tokens: int = Field(default=30000, ge=1)  # conversation tokens (the system prompt and the tool schemas do not count)
    keep_recent_turns: int = Field(default=6, ge=0)  # the last turn is always kept verbatim, even at 0
    tool_result_max_chars: int = Field(default=12000, ge=100)
    # A new session (a restart, a daily rollover, another interface) replays the last `keep_recent_turns` turns
    # of the previous minutes as plain text, so a question Jarvis just asked stays open. 0 = never.
    resume_after_min: int = Field(default=15, ge=0)


class ModelPrice(_Section):
    """USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)


class BudgetConfig(_Section):
    daily_budget_usd: float = Field(default=3.0, ge=0)
    warn_at_fraction: float = Field(default=0.8, ge=0, le=1)
    # Cache pricing relative to the input price (Anthropic: reads 0.1x, 5-minute writes 1.25x).
    cache_read_multiplier: float = Field(default=0.1, ge=0)
    cache_write_multiplier: float = Field(default=1.25, ge=0)
    prices: dict[str, ModelPrice] = Field(default_factory=dict)
    # Estimated prices of the other paid services, for the dashboard's cost charts.
    elevenlabs_usd_per_1k_chars: float = Field(default=0.22, ge=0)  # Creator plan: $22 per 100k characters
    maps_usd_per_call: float = Field(default=0.005, ge=0)  # Places / Directions / Distance Matrix: about $5 per 1000


class CalendarFilter(_Section):
    """One `[[calendar.filters]]` entry: in the calendar named `calendar`, show only the events whose
    title contains one of `keywords` (case- and Turkish-accent-insensitive; a keyword of up to three
    characters such as "FB" only as a whole word). With `only_matches` (default) the filter applies
    only to fixture-shaped titles ("A - B", "A vs B"); other entries in that calendar stay visible."""

    calendar: str = Field(min_length=1)
    mode: Literal["include_only"] = "include_only"
    keywords: list[str] = Field(min_length=1)
    only_matches: bool = True

    @model_validator(mode="after")
    def _keywords_not_blank(self) -> "CalendarFilter":
        cleaned = [k.strip() for k in self.keywords if k.strip()]
        if not cleaned:
            raise ValueError(f"calendar.filters: {self.calendar!r} needs at least one non-empty keyword")
        self.keywords = cleaned
        return self


class CalendarConfig(_Section):
    default_write_calendar: Literal["google", "apple"] = "google"
    apple_calendar_exclude: list[str] = Field(default_factory=list)
    filters: list[CalendarFilter] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_filter_per_calendar(self) -> "CalendarConfig":
        seen: dict[str, str] = {}
        for flt in self.filters:
            key = flt.calendar.strip().casefold()
            if key in seen:
                raise ValueError(f"calendar.filters: calendar {flt.calendar!r} is listed twice")
            seen[key] = flt.calendar
        return self


class WeatherConfig(_Section):
    """Where "what's the weather" is for when no place is named. `current`: the Current
    Location, falling back to the home Location of profile.md and then to `fixed_location`;
    `home`: always the profile's Location; `fixed`: always `fixed_location`. `default_location`
    is the older name of `fixed_location`: it is still read as `fixed_location` (`jarvis doctor` warns)."""

    location_source: Literal["current", "home", "fixed"] = "current"
    fixed_location: str = ""
    default_location: str = ""  # deprecated

    @model_validator(mode="after")
    def _migrate_default_location(self) -> "WeatherConfig":
        self.fixed_location = self.fixed_location.strip()
        self.default_location = self.default_location.strip()
        if self.default_location and not self.fixed_location:
            self.fixed_location = self.default_location
        return self

    @property
    def deprecated_keys(self) -> list[str]:
        return ["weather.default_location (use weather.fixed_location and weather.location_source)"] if self.default_location else []


class LocationConfig(_Section):
    """Current Location detection. Sources in order: the allowlisted Shortcut
    `shortcut_name` ("Get Current Location"), CoreLocationCLI when installed, IP geolocation
    (city level, approximate). The lookup runs in the background at session start and lazily
    when a location-dependent tool finds the stored location older than `max_age_min`."""

    enabled: bool = True
    refresh_on_start: bool = True  # background refresh at the start of every CLI / voice session
    shortcut_name: str = "Jarvis Location"  # must also be under [[shortcuts.allowed]] (risk safe, remote false)
    timeout_s: float = Field(default=5.0, gt=0, le=60)  # per source
    max_age_min: int = Field(default=60, ge=1)  # older than this: refresh before a weather/maps tool runs
    profile_precision: Literal["city", "neighborhood", "street"] = "neighborhood"  # what enters profile.md
    keep_history: bool = False  # true appends every new location to data/location_history.jsonl
    ip_fallback: bool = True  # last resort; wrong behind a VPN, marked approximate
    ip_lookup_url: str = "https://ipinfo.io/json"  # JSON with city/region/country and loc or latitude/longitude

    @model_validator(mode="after")
    def _plain_values(self) -> "LocationConfig":
        self.shortcut_name = " ".join(self.shortcut_name.split())
        if any(ch in self.shortcut_name for ch in "\r\n\x00") or self.shortcut_name.startswith("-"):
            raise ValueError("location.shortcut_name must be a single line that does not start with '-'")
        url = self.ip_lookup_url.strip()
        if not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?(/[^\s]*)?", url):
            raise ValueError("location.ip_lookup_url must be an https:// URL")
        self.ip_lookup_url = url
        return self


class SecurityConfig(_Section):
    allowed_file_roots: list[str] = Field(min_length=1)
    denied_paths: list[str]
    shell_denylist: list[str]
    shell_timeout_seconds: int = Field(default=60, ge=1)


class LoggingConfig(_Section):
    app_log_retention_days: int = Field(default=14, ge=1)
    audit_log_retention_days: int = Field(default=90, ge=1)


class ScreenConfig(_Section):
    max_image_side_px: int = Field(default=1568, ge=256, le=8000)
    store_images: bool = False  # keep screenshot bytes in the SQLite transcript (default: reference only)


class TrainingConfig(_Section):
    health_inbox: str = "~/Library/Mobile Documents/com~apple~CloudDocs/Jarvis/health_inbox"
    sync_on_startup: bool = True


class TimersConfig(_Section):
    notification_sound: str = "Glass"
    notify_channels: list[Literal["mac", "telegram"]] = Field(default_factory=lambda: ["mac"])
    pomodoro_work_min: int = Field(default=25, ge=1)
    pomodoro_break_min: int = Field(default=5, ge=1)
    pomodoro_long_break_min: int = Field(default=15, ge=1)
    pomodoro_cycles: int = Field(default=4, ge=1)


class AlarmConfig(_Section):
    """Alarms (tools/alarms.py): a loud, looping sound on the Mac at a clock time, stopped or snoozed
    from a dialog, the push-to-talk hotkey, a spoken word or Telegram, with a phone backup so a
    wake-up alarm also reaches the iPhone. Timers (`[timers]`) are the "N minutes from now" notifications."""

    sound: str = "alarm"  # sounds/alarm.m4a|.mp3|.wav|.aiff (first found); a macOS system sound when missing
    sound_volume: float = Field(default=1.0, ge=0.0, le=2.0)  # `afplay -v` gain on top of the system volume
    volume: int = Field(default=70, ge=0, le=100)  # the system output volume while ringing; restored afterwards
    max_ring_min: int = Field(default=10, ge=1, le=60)  # the alarm stops by itself after this
    snooze_min: int = Field(default=9, ge=1, le=60)  # "Ertele" and `snooze_alarm` without minutes
    keep_awake_hours: int = Field(default=12, ge=0, le=48)  # `caffeinate -i` while an alarm is due within this window (0 = never)
    low_battery_percent: int = Field(default=20, ge=0, le=100)  # on battery below this, setting an alarm warns that it may not ring
    # Where the alarm also goes so the phone gets it: an Apple Reminder (EventKit, synced through iCloud),
    # a Telegram message at the alarm time, both, or nothing. A reminder is a normal timed reminder: current
    # EventKit has no way to make the alarm-style "Urgent" reminder of iOS 26.2, so it does not ring like an alarm.
    phone_backup: Literal["reminder", "telegram", "both", "off"] = "both"
    reminder_list: str = "Jarvis Alarm"  # the Reminders list that holds the backups (created when missing)

    @model_validator(mode="after")
    def _plain_values(self) -> "AlarmConfig":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.sound):
            raise ValueError("alarm.sound must be a plain file name without extension or path (letters, digits, - and _)")
        self.reminder_list = " ".join(self.reminder_list.split())
        if not self.reminder_list or len(self.reminder_list) > 64:
            raise ValueError("alarm.reminder_list must be a short, non-empty list name")
        return self


class FootballConfig(_Section):
    team: str = "Fenerbahçe"
    league: str = "Süper Lig"
    provider: str = "api-football"
    cache_ttl_fixtures_min: int = Field(default=360, ge=1)
    cache_ttl_standings_min: int = Field(default=60, ge=1)
    cache_ttl_live_min: int = Field(default=1, ge=1)


class TelegramConfig(_Section):
    enabled: bool = False
    allowed_user_id: int = 0
    allowed_chat_id: int = 0
    confirm_timeout_min: int = Field(default=5, ge=1)
    max_messages_per_minute: int = Field(default=20, ge=1)
    poll_timeout_s: int = Field(default=30, ge=1, le=50)
    # "module.tool" or "module.*" (module = the file in tools/, tool = the registered name)
    remote_allowed_tools: list[str] = Field(default_factory=list)


class VoiceSoundsConfig(_Section):
    """Cue sounds under `<JARVIS_HOME>/sounds/` (`[voice.sounds]`). `heard` plays once a request has
    been recorded (non-blocking); `ready` plays before the follow-up window opens (the microphone
    waits for it). Names are file base names: `sounds/<name>.m4a|.mp3|.wav|.aiff`, first found
    wins; a macOS system sound plays when the file is missing."""

    heard: str = "voice_heard"
    ready: str = "voice_ready"
    volume: float = Field(default=1.0, ge=0.0, le=2.0)  # `afplay -v`; 1.0 = the file's own level
    heard_enabled: bool = True
    ready_enabled: bool = True
    # The thinking sound: `sounds/thinking_sound.*` loops quietly while Jarvis works on a reply (after
    # the heard cue, once `thinking_delay_ms` passed without a spoken answer) and stops the moment the
    # reply's first audio, a confirmation, an error, a barge-in or a pause arrives.
    thinking: str = "thinking_sound"
    thinking_enabled: bool = True
    thinking_volume: float = Field(default=0.4, ge=0.0, le=2.0)
    thinking_delay_ms: int = Field(default=700, ge=0, le=10_000)  # fast replies never hear it
    thinking_gap_ms: int = Field(default=0, ge=0, le=10_000)  # silence between two loops of the file

    @model_validator(mode="after")
    def _names_are_plain(self) -> "VoiceSoundsConfig":
        for field_name in ("heard", "ready", "thinking"):
            value = getattr(self, field_name)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                raise ValueError(
                    f"voice.sounds.{field_name} must be a plain file name without extension or path (letters, digits, - and _)"
                )
        return self


class VoiceConfig(_Section):
    """Hands-free voice service. `general.use_voice` must be true to start it."""

    wake_word_model: str = "hey_jarvis"  # openWakeWord pretrained model name (file hey_jarvis_v0.1.onnx)
    wake_word_threshold: float = Field(default=0.5, ge=0, le=1)
    wake_word_cooldown_s: float = Field(default=2.0, ge=0)
    wake_word_barge_in: bool = False  # true: the wake word also interrupts Jarvis while it speaks (headphones)
    post_speech_cooldown_s: float = Field(default=1.0, ge=0)  # ignore the wake word this long after Jarvis stops talking
    hotkey: str = "<alt>+<space>"  # pynput combo; hold to talk, press while speaking to interrupt
    listen_sound: str = "/System/Library/Sounds/Tink.aiff"
    no_speech_sound: str = "/System/Library/Sounds/Pop.aiff"
    vad_silence_ms: int = Field(default=700, ge=100)
    vad_threshold: float = Field(default=0.5, ge=0, le=1)
    max_record_s: int = Field(default=30, ge=1)
    no_speech_timeout_s: int = Field(default=5, ge=1)
    stt_model: str = "mlx-community/whisper-small-mlx"
    # The language Whisper decodes in. A fixed code ("tr", "en") skips Whisper's per-utterance language
    # detection, which mistook one short Turkish request in four for English or another language; "auto"
    # detects and falls back to the first entry of `stt_languages` outside that list. A Literal, so the
    # dashboard offers it as a dropdown (Settings > Voice > Speech language).
    stt_language: Literal["tr", "en", "auto"] = "tr"
    # A different Whisper model per fixed language, e.g. {"en": "mlx-community/whisper-small.en-mlx"}: used
    # instead of `stt_model` when `stt_language` has an entry here.
    stt_model_by_language: dict[str, str] = Field(default_factory=dict)
    stt_languages: list[str] = Field(default_factory=lambda: ["tr", "en"], min_length=1)
    # The recognizer: Apple's on-device Speech framework, local mlx-whisper, ElevenLabs Scribe (cloud, paid)
    # or hybrid (Apple first, Scribe when Apple's confidence is below `stt_hybrid_threshold`). Whisper is
    # the automatic fallback of every other engine. `jarvis voice test-stt` compares them.
    stt_engine: Literal["apple", "whisper", "elevenlabs", "hybrid"] = "whisper"
    stt_hybrid_threshold: float = Field(default=0.6, ge=0, le=1)
    # Audio kept from before the recording starts (after the wake word, at the start of a follow-up window),
    # so the first word is not cut off.
    preroll_ms: int = Field(default=1000, ge=0, le=2000)
    stt_hallucination_blocklist: list[str] = Field(default_factory=lambda: ["Altyazı M.K.", "Thanks for watching!", "Thank you."])
    tts_engine: Literal["say", "elevenlabs"] = "say"
    tts_voice_tr: str = "Yelda"
    tts_voice_en: str = "Samantha"
    tts_rate: int = Field(default=190, ge=80, le=400)
    # ElevenLabs: the API key lives in the Keychain (`jarvis voice set-elevenlabs-key`).
    elevenlabs_voice_id: str = ""  # from `jarvis voice list-voices`
    elevenlabs_model: str = "eleven_flash_v2_5"  # low latency, Turkish and English
    elevenlabs_first_chunk_timeout_s: float = Field(default=3.0, gt=0)  # fall back to `say` after this
    elevenlabs_monthly_char_budget: int = Field(default=100000, ge=0)  # 0 disables the budget switch
    elevenlabs_use_for_tainted: bool = True  # false: tainted turns (email, web content) are spoken by `say`
    # ElevenLabs' own text normalization (`apply_text_normalization`): "on" spells out whatever digits our
    # normalizer (voice/normalize.py) left behind; "auto" lets ElevenLabs decide; "off" skips it.
    elevenlabs_text_normalization: Literal["on", "auto", "off"] = "on"

    @model_validator(mode="after")
    def _stt_models_by_language_are_codes(self) -> "VoiceConfig":
        for code, model in self.stt_model_by_language.items():
            if not re.fullmatch(r"[a-z]{2,3}", code.strip().lower()) or not model.strip():
                raise ValueError("voice.stt_model_by_language maps a language code to a Whisper model name")
        self.stt_model_by_language = {code.strip().lower(): model.strip() for code, model in self.stt_model_by_language.items()}
        return self

    @model_validator(mode="after")
    def _elevenlabs_voice_id_is_plain(self) -> "VoiceConfig":
        if self.elevenlabs_voice_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.elevenlabs_voice_id):
            raise ValueError("elevenlabs_voice_id must be the id from `jarvis voice list-voices` (letters, digits, - and _)")
        return self

    confirm_phrases: list[str] = Field(default_factory=lambda: ["onaylıyorum", "evet onaylıyorum", "confirm", "yes confirm"], min_length=1)
    confirm_timeout_s: int = Field(default=20, ge=3)
    # Spoken pause phrases, matched exactly in code (same normalisation as confirm_phrases, an optional
    # duration allowed: "1 saat sesi kapat") and handled without the model; anything less exact reaches
    # the model, which has the `pause_listening` tool. Empty list: only the tool handles it.
    pause_phrases: list[str] = Field(default_factory=lambda: ["sesi kapat", "dinlemeyi durdur", "stop listening"])
    # Follow-up mode: after a reply Jarvis keeps listening for a short window without the wake word.
    follow_up_mode: Literal["always", "questions_only", "off"] = "always"
    follow_up_window_s: float = Field(default=6.0, ge=0.5, le=60)  # time to START talking; speech itself may run longer
    sounds: VoiceSoundsConfig = Field(default_factory=VoiceSoundsConfig)  # the `voice_heard` / `voice_ready` cues
    show_transcript_notification: bool = False
    save_audio: bool = False  # true writes every recording to data/voice_audio/ (debugging only)


class SheetsConfig(_Section):
    """Google Sheets. `aliases`: a short name -> spreadsheet id, so "add this to my training sheet" works."""

    aliases: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _aliases_are_ids(self) -> "SheetsConfig":
        for alias, sheet_id in self.aliases.items():
            if not alias.strip():
                raise ValueError("sheets.aliases: an alias must not be empty")
            if not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", sheet_id or ""):
                raise ValueError(f"sheets.aliases[{alias!r}] must be a spreadsheet id (the long token in the sheet's URL)")
        return self


class MapsConfig(_Section):
    """Google Maps Platform. The API key lives in the Keychain (`jarvis key set google_maps_api_key`)."""

    open_with: Literal["apple", "google"] = "apple"
    default_mode: Literal["transit", "driving", "walking", "bicycling"] = "transit"
    leave_buffer_min: int = Field(default=10, ge=0)
    daily_call_limit: int = Field(default=100, ge=0)  # 0 disables the guard


_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(text: str | None) -> tuple[int, int] | None:
    """ "09:00" -> (9, 0); None for an empty string, "off" or anything else."""
    match = _HHMM_RE.match((text or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


class BriefScheduleConfig(_Section):
    """The automatic morning brief (scheduler/brief.py), delivered once a day by the scheduler agent at
    `weekday_time` or `weekend_time` ("" or "off" = no brief on those days). `channel`: "auto" speaks it
    when the user is at the Mac (active within `active_within_min`, voice running, not paused, no Do Not
    Disturb) and sends it to Telegram otherwise; "voice" and "both" still fall back to Telegram when
    speaking is not possible. A brief missed while the Mac slept is delivered on wake until `late_until`,
    then skipped. A brief the user asked for that morning (`daily_brief`) also makes the automatic one skip."""

    enabled: bool = False
    weekday_time: str = "09:00"
    weekend_time: str = "10:30"
    channel: Literal["auto", "telegram", "voice", "both"] = "auto"
    late_until: str = "12:00"
    active_within_min: int = Field(default=15, ge=1, le=240)

    @model_validator(mode="after")
    def _times_parse(self) -> "BriefScheduleConfig":
        for name in ("weekday_time", "weekend_time"):
            value = getattr(self, name).strip()
            if value.lower() in ("", "off"):
                setattr(self, name, "")
            elif parse_hhmm(value) is None:
                raise ValueError(f'brief.schedule.{name} must be HH:MM or "off" (got {value!r})')
            else:
                setattr(self, name, "%02d:%02d" % parse_hhmm(value))
        if parse_hhmm(self.late_until) is None:
            raise ValueError(f"brief.schedule.late_until must be HH:MM (got {self.late_until!r})")
        self.late_until = "%02d:%02d" % parse_hhmm(self.late_until)
        return self


class BriefConfig(_Section):
    include_travel: bool = False  # add the travel time to the first calendar event with a location
    include_mail: bool = True  # list today's inbox (sender, subject, Gmail labels) so the brief names the important mails
    schedule: BriefScheduleConfig = Field(default_factory=BriefScheduleConfig)


class ShortcutEntry(_Section):
    """One Apple Shortcut Jarvis may run. `name` must match the Shortcuts app exactly."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)  # the model matches requests against this
    risk: Literal["safe", "confirm"] = "confirm"  # safe runs directly, confirm asks first
    remote: bool = False  # may run from Telegram
    taints: bool = False  # the output is untrusted (a Shortcut that reads a web page, for example)

    @model_validator(mode="after")
    def _name_is_a_plain_argv_element(self) -> "ShortcutEntry":
        name = self.name.strip()
        if not name:
            raise ValueError("shortcuts.allowed: name must not be empty")
        if name.startswith("-"):
            raise ValueError(
                f"shortcuts.allowed: name {name!r} must not start with '-' (it would look like an option to the shortcuts CLI)"
            )
        if any(ch in name for ch in "\r\n\x00"):
            raise ValueError(f"shortcuts.allowed: name {name!r} must be a single line")
        self.name = name
        self.description = " ".join(self.description.split())
        return self


class ShortcutsConfig(_Section):
    """Apple Shortcuts bridge. Only the listed Shortcuts can run."""

    timeout_seconds: int = Field(default=30, ge=1)
    allowed: list[ShortcutEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _names_are_unique(self) -> "ShortcutsConfig":
        seen: dict[str, str] = {}
        for entry in self.allowed:
            key = " ".join(entry.name.casefold().split())
            if key in seen:
                raise ValueError(f"shortcuts.allowed: duplicate name {entry.name!r} (also listed as {seen[key]!r})")
            seen[key] = entry.name
        return self

    def entry(self, name: str) -> ShortcutEntry | None:
        """The allowlist entry for `name`: exact match first, else case- and whitespace-insensitive."""
        for entry in self.allowed:
            if entry.name == name:
                return entry
        key = " ".join((name or "").casefold().split())
        for entry in self.allowed:
            if " ".join(entry.name.casefold().split()) == key:
                return entry
        return None


class NetworkConfig(_Section):
    """Internet test and history. Full speed tests use data and never run on their own
    unless `periodic_speedtest_hours > 0`; periodic quick checks (diagnostics only) are off by default."""

    periodic_check_min: int = Field(default=0, ge=0)  # 0 = off; else a quick check every N minutes, notify on down/up
    periodic_speedtest_hours: int = Field(default=0, ge=0)  # 0 = never run a speed test automatically
    public_check_hosts: list[str] = Field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"], min_length=1)
    dns_check_names: list[str] = Field(default_factory=lambda: ["apple.com", "google.com"], min_length=1)
    speedtest_max_runtime_s: int = Field(default=30, ge=5, le=120)  # `networkQuality -M`

    @model_validator(mode="after")
    def _hosts_are_plain(self) -> "NetworkConfig":
        for field_name in ("public_check_hosts", "dns_check_names"):
            cleaned = []
            for host in getattr(self, field_name):
                host = host.strip().lower()
                if not re.fullmatch(r"[a-z0-9.\-:]{1,253}", host) or host.startswith("-"):
                    raise ValueError(f"network.{field_name}: {host!r} is not a host name or IP address")
                cleaned.append(host)
            setattr(self, field_name, cleaned)
        return self


_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def normalize_mac(text: str) -> str | None:
    """'a4:2b:b0:7C:1E:9F', '8:47:65:dc:c3:5b' (macOS `arp` drops leading zeros), 'a4-2b-b0-7c-1e-9f'
    -> 'aa:bb:cc:dd:ee:ff'; None when it is not a MAC address."""
    parts = re.split(r"[:\-]", (text or "").strip().lower())
    if len(parts) != 6:
        return None
    try:
        octets = [int(p, 16) for p in parts]
    except ValueError:
        return None
    if any(not 0 <= o <= 255 or not p for o, p in zip(octets, parts)):
        return None
    return ":".join(f"{o:02x}" for o in octets)


class PresencePerson(_Section):
    """One household member and the MAC addresses of their devices on the home Wi-Fi."""

    name: str = Field(min_length=1, max_length=64)
    devices: list[str] = Field(min_length=1)  # MAC addresses (an iPhone's private address must be "Fixed" for the home network)
    is_me: bool = False
    track_history: bool = True  # false: only the current state is kept, no arrivals/departures

    @model_validator(mode="after")
    def _clean(self) -> "PresencePerson":
        name = " ".join(self.name.split())
        if not name or any(ch in "<>" or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
            raise ValueError(f"presence.people: name {self.name!r} must be plain text")
        self.name = name
        macs = []
        for raw in self.devices:
            mac = normalize_mac(raw)
            if mac is None:
                raise ValueError(f"presence.people[{name}]: {raw!r} is not a MAC address (aa:bb:cc:dd:ee:ff)")
            if mac not in macs:
                macs.append(mac)
        self.devices = macs
        return self


class PresenceConfig(_Section):
    """ "Am I home?" and "Who is home?". Filled by `jarvis presence setup`."""

    home_gateway_macs: list[str] = Field(default_factory=list)  # the router's MAC address(es); identifies the home network
    scan_interval_s: int = Field(default=120, ge=30)
    away_after_min: int = Field(default=10, ge=1)  # a person is away only after not being seen this long
    allow_remote: bool = False  # presence tools from Telegram (default: a stolen phone must not learn when the house is empty)
    allow_remote_self: bool = False  # exception: only `am_i_home` from Telegram
    on_arrive_greet: bool = False  # spoken greeting with a short brief when I arrive (voice running and not paused)
    on_leave_pause_voice: bool = False  # pause voice listening when the Mac leaves the home network, resume on return
    notify_person_changes: bool = False  # macOS/Telegram notification when a configured person arrives or leaves
    people: list[PresencePerson] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> "PresenceConfig":
        macs = []
        for raw in self.home_gateway_macs:
            mac = normalize_mac(raw)
            if mac is None:
                raise ValueError(f"presence.home_gateway_macs: {raw!r} is not a MAC address")
            if mac not in macs:
                macs.append(mac)
        self.home_gateway_macs = macs
        names: set[str] = set()
        devices: dict[str, str] = {}
        me = 0
        for person in self.people:
            key = person.name.casefold()
            if key in names:
                raise ValueError(f"presence.people: duplicate name {person.name!r}")
            names.add(key)
            me += int(person.is_me)
            for mac in person.devices:
                if mac in devices:
                    raise ValueError(f"presence.people: device {mac} is listed for both {devices[mac]!r} and {person.name!r}")
                devices[mac] = person.name
        if me > 1:
            raise ValueError("presence.people: only one person can have is_me = true")
        return self

    def me(self) -> PresencePerson | None:
        return next((p for p in self.people if p.is_me), None)

    def person(self, name: str) -> PresencePerson | None:
        key = " ".join((name or "").casefold().split())
        return next((p for p in self.people if p.name.casefold() == key), None)

    def person_for_device(self, mac: str) -> PresencePerson | None:
        mac = normalize_mac(mac) or ""
        return next((p for p in self.people if mac in p.devices), None)

    def is_home_gateway(self, mac: str | None) -> bool:
        return bool(mac) and (normalize_mac(mac) or "") in self.home_gateway_macs


class DashboardConfig(_Section):
    """`jarvis dashboard`: the local monitoring and settings page, served on 127.0.0.1 only
    while the command runs in the foreground."""

    port: int = Field(default=8765, ge=1024, le=65535)  # if busy, the next free port is used
    idle_lock_min: int = Field(default=3, ge=1, le=240)  # the page locks after this much time without clicks, typing or scrolling
    unlock: Literal["click", "touch_id"] = "click"  # how the Continue button unlocks: one click, or the Mac's Touch ID / password
    doctor_refresh_s: int = Field(default=10, ge=2, le=600)  # how often the Overview re-runs the quick health checks
    backups_keep: int = Field(default=20, ge=1, le=200)  # data/backups/<timestamp>/ folders kept by the save flow
    # Analytics "Corrections": a voice request that starts with one of these, within two minutes of the previous one,
    # counts as a correction of it. Your language's words go here; the dashboard code holds none.
    correction_words: list[str] = Field(default_factory=lambda: ["no ", "not that", "wrong", "i meant"])


class Config(_Section):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    models: ModelsConfig
    loop: LoopConfig = Field(default_factory=LoopConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    security: SecurityConfig
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    training: TrainingConfig = Field(default_factory=TrainingConfig)
    timers: TimersConfig = Field(default_factory=TimersConfig)
    alarm: AlarmConfig = Field(default_factory=AlarmConfig)
    football: FootballConfig = Field(default_factory=FootballConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    sheets: SheetsConfig = Field(default_factory=SheetsConfig)
    maps: MapsConfig = Field(default_factory=MapsConfig)
    brief: BriefConfig = Field(default_factory=BriefConfig)
    shortcuts: ShortcutsConfig = Field(default_factory=ShortcutsConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)

    @property
    def deprecated_keys(self) -> list[str]:
        """Config keys from an earlier release that are still read this release (`jarvis doctor` warns)."""
        return [*self.general.deprecated_keys, *self.weather.deprecated_keys]


@dataclass(frozen=True)
class Paths:
    """Everything Jarvis reads or writes on disk, derived from one home directory."""

    home: Path

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def local_config_file(self) -> Path:
        """`config.local.toml`: this Mac's personal values (router MAC, household phones), merged over
        `config.toml` at load and ignored by git."""
        return local_config_path(self.config_file)

    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "jarvis.db"

    @property
    def profile_file(self) -> Path:
        return self.data_dir / "profile.md"

    @property
    def memory_file(self) -> Path:
        return self.data_dir / "memory.md"

    @property
    def delegate_permissions_file(self) -> Path:
        return self.data_dir / "delegate_permissions.json"

    @property
    def docs_dir(self) -> Path:
        return self.home / "docs"

    @property
    def telegram_state_file(self) -> Path:
        return self.data_dir / "telegram_state.json"

    @property
    def voice_pause_file(self) -> Path:
        """Present while voice listening is paused (`jarvis voice pause`, "sesi kapat")."""
        return self.data_dir / "voice_paused.json"

    @property
    def voice_audio_dir(self) -> Path:
        return self.data_dir / "voice_audio"

    @property
    def stt_test_dir(self) -> Path:
        return self.data_dir / "stt_test"  # `jarvis voice test-stt` sessions: WAVs, manifest, results

    @property
    def vocabulary_file(self) -> Path:
        return self.data_dir / "vocabulary.md"  # the user's own speech hints (names, places), one per line

    @property
    def vocabulary_auto_file(self) -> Path:
        return self.data_dir / "vocabulary.auto.md"  # collected by code, rewritten on every refresh (read-only)

    @property
    def corrections_file(self) -> Path:
        return self.data_dir / "corrections.md"  # misheard -> meant, learned from "hayır, X dedim"

    @property
    def bin_dir(self) -> Path:
        return self.data_dir / "bin"  # compiled helpers (the Apple speech recognizer)

    @property
    def voice_lock_file(self) -> Path:
        """Held (flock) by the running voice service: a second `jarvis voice run` refuses to start."""
        return self.data_dir / "voice.lock"

    @property
    def voice_announce_file(self) -> Path:
        """A one-shot request for the voice service to speak (the arrive-home greeting)."""
        return self.data_dir / "voice_announce.json"

    @property
    def location_state_file(self) -> Path:
        """The exact current location (Mac and Telegram-shared) for the tools; never sent to the API."""
        return self.data_dir / "location_state.json"

    @property
    def location_history_file(self) -> Path:
        """Only with `location.keep_history = true`: one JSON line per refresh."""
        return self.data_dir / "location_history.jsonl"

    @property
    def alarm_ring_file(self) -> Path:
        """Present while an alarm rings (written by the scheduler's ringer): id, label, started_at, pid."""
        return self.data_dir / "alarm_ringing.json"

    @property
    def alarm_command_file(self) -> Path:
        """A stop / snooze request for the ringing alarm from another process (tool, hotkey, voice, Telegram, CLI)."""
        return self.data_dir / "alarm_command.json"

    @property
    def sounds_dir(self) -> Path:
        """The user's cue sounds (`voice_heard.*`, `voice_ready.*`) and `alarm.*`; see sounds/README.md."""
        return self.home / "sounds"

    @property
    def tts_cache_dir(self) -> Path:
        """Cached ElevenLabs audio for short repeated system phrases."""
        return self.data_dir / "tts_cache"

    @property
    def backups_dir(self) -> Path:
        """data/backups/<timestamp>/: the files as they were before each dashboard save (History)."""
        return self.data_dir / "backups"

    @property
    def persona_file(self) -> Path:
        """The user's own persona (the dashboard's Persona editor writes it); when it does not exist, the
        persona shipped for the main language is used (`default_persona_file`)."""
        return self.data_dir / "persona.md"

    def default_persona_file(self, language: str) -> Path:
        return self.prompts_dir / f"persona.{language}.md"

    def rules_files(self, language: str) -> list[Path]:
        """The shared rules, then the main language's phrasing notes."""
        return [self.prompts_dir / "rules.md", self.prompts_dir / f"rules.{language}.md"]

    @property
    def prompts_dir(self) -> Path:
        return Path(__file__).resolve().parent / "prompts"

    def ensure_dirs(self) -> None:
        """data/ and logs/; a folder created here is owner-only (an existing one keeps its mode)."""
        for folder in (self.data_dir, self.logs_dir):
            if not folder.is_dir():
                folder.mkdir(parents=True, exist_ok=True)
                folder.chmod(0o700)


def package_repo_root(package_file: str | None = None) -> Path | None:
    """The repository that holds this package when it is installed in editable mode
    (`uv tool install --editable .` or the project venv): src/jarvis/config.py -> repo root.
    None for a regular (copied) install, where the parent directories are site-packages."""
    root = Path(package_file or __file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "src" / "jarvis" / "__init__.py").is_file():
        return root
    return None


HOME_HELP = (
    "set JARVIS_HOME to the Jarvis repository (the folder with config.toml, data/ and logs/), "
    "or install the command from that repository with `uv tool install --editable '.[voice]' --python 3.12`"
)


def resolve_home() -> Path:
    """The Jarvis home, the same from any working directory: $JARVIS_HOME, else the
    repository of the editable install, else a ConfigError that says how to fix it."""
    env = os.environ.get("JARVIS_HOME")
    if env:
        home = Path(env).expanduser().resolve()
        if not home.is_dir():
            raise ConfigError(f"JARVIS_HOME={env!r} is not a directory; {HOME_HELP}")
        return home
    root = package_repo_root()
    if root is not None:
        return root
    raise ConfigError(
        f"cannot find the Jarvis home: the package at {Path(__file__).resolve().parent} is not an editable "
        f"install and JARVIS_HOME is not set; {HOME_HELP}"
    )


LOCAL_CONFIG_NAME = "config.local.toml"


def local_config_path(config_file: Path) -> Path:
    """The personal overlay next to config.toml (`config.local.toml`, gitignored)."""
    return config_file.with_name(LOCAL_CONFIG_NAME)


def _read_toml(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def merge_config_data(base: dict, overlay: dict) -> dict:
    """Deep-merge TOML data: tables merge key by key, every other value (scalars, arrays, arrays of
    tables such as `[[presence.people]]`) from the overlay replaces the base value."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config_data(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path) -> Config:
    """Parse and validate config.toml, with `config.local.toml` next to it merged on top when it exists
    (personal values that stay out of git). Raises ConfigError with a readable message."""
    if not path.is_file():
        raise ConfigError(f"config.toml not found at {path} (Jarvis home: {path.parent}; {HOME_HELP})")
    data = _read_toml(path)
    local = local_config_path(path)
    sources = str(path)
    if local.is_file():
        data = merge_config_data(data, _read_toml(local))
        sources = f"{path} (with {local.name})"
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        lines = [f"{sources} is invalid:"]
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from None


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)  # 3.0, 0.5: a TOML float, never quoted
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def update_local_config(config_file: Path, section: str, values: dict[str, object]) -> Path:
    """Set `key = value` lines inside `[section]` of `config.local.toml` (the personal overlay next to
    config.toml, created when missing). Existing lines are replaced in place with their trailing comment
    kept; new keys are appended to the table; the table is appended when absent. Afterwards the whole
    config is reloaded to prove it still parses, and the overlay is restored (or removed) when it does
    not. Returns the overlay path."""
    local = local_config_path(config_file)
    original = local.read_text(encoding="utf-8") if local.is_file() else None
    lines = original.splitlines(keepends=True) if original else []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        if lines:
            lines.append("\n")
        lines.append(header + "\n")
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1  # new keys go before the blank lines that separate the tables
    for key, value in values.items():
        rendered = f"{key} = {_toml_value(value)}"
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i in range(start + 1, end):
            if pattern.match(lines[i]):
                rest = lines[i].split("=", 1)[1]
                comment = ""
                if "#" in rest and not isinstance(value, str):
                    comment = "  #" + rest.split("#", 1)[1].rstrip("\n")
                elif "#" in rest:
                    tail = rest.split('"')[-1] if rest.count('"') >= 2 else rest
                    if "#" in tail:
                        comment = "  #" + tail.split("#", 1)[1].rstrip("\n")
                lines[i] = rendered + comment + "\n"
                break
        else:
            lines.insert(end, rendered + "\n")
            end += 1
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("".join(lines), encoding="utf-8")
    try:
        load_config(config_file)
    except ConfigError as exc:
        if original is not None:
            local.write_text(original, encoding="utf-8")
        else:
            local.unlink(missing_ok=True)
        raise ConfigError(f"the new {local.name} did not parse and was restored: {exc}") from None
    return local


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no interpolation, never overrides existing env."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
