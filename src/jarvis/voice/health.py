"""`jarvis doctor` checks for the voice service. Never raises, never prompts, no audio."""

from __future__ import annotations

import shutil
from datetime import datetime
from typing import TYPE_CHECKING, Any

from jarvis.observability.health import Check

if TYPE_CHECKING:
    from jarvis.config import Config, Paths

PERMISSION_HINT = "System Settings > Privacy & Security > {section}: enable the terminal app (or the venv's python under launchd); see docs/troubleshooting.md (voice)"


def health_checks(config: "Config", paths: "Paths") -> list[Check]:  # noqa: ARG001 - signature shared by every module
    from jarvis.voice.service import VoiceService  # noqa: PLC0415

    checks: list[Check] = []
    enabled = bool(config.general.use_voice)
    v = config.voice
    checks.append(Check("voice enabled", True, "use_voice = true" if enabled else "disabled (general.use_voice = false)"))

    missing = VoiceService.missing_dependencies()
    checks.append(
        Check(
            "voice packages",
            not missing or not enabled,
            "installed"
            if not missing
            else "missing " + ", ".join(missing) + ": reinstall with `uv tool install --editable '.[voice]' --python 3.12`",
        )
    )
    if missing:
        return checks + _elevenlabs_checks(config, paths) + _agent_check(enabled) + [_pause_check(paths)]

    # Microphone: the status belongs to the calling process (terminal now, venv python under launchd).
    try:
        from jarvis.voice.audio import microphone_permission  # noqa: PLC0415

        mic = microphone_permission()
    except Exception as exc:
        mic = f"unknown ({type(exc).__name__})"
    mic_ok = mic in ("authorized", "not_determined") or mic.startswith("unknown")
    detail = {
        "authorized": "granted to this process",
        "not_determined": "not asked yet (the first `jarvis voice` run prompts)",
        "denied": "DENIED: " + PERMISSION_HINT.format(section="Microphone"),
        "restricted": "restricted by a profile",
    }.get(mic, mic)
    checks.append(Check("voice microphone", mic_ok or not enabled, detail))

    try:
        from jarvis.voice.hotkey import input_monitoring_granted  # noqa: PLC0415

        granted = input_monitoring_granted()
    except Exception:
        granted = None
    if granted is None:
        checks.append(Check("voice input monitoring", True, "cannot query (Quartz unavailable)"))
    else:
        checks.append(
            Check(
                "voice input monitoring",
                granted or not enabled,
                "granted to this process (hotkey works)"
                if granted
                else "not granted: the hotkey will not work; " + PERMISSION_HINT.format(section="Input Monitoring"),
            )
        )

    try:
        from jarvis.voice.wakeword import model_dir, models_present  # noqa: PLC0415

        present = models_present(v.wake_word_model)
        checks.append(
            Check(
                "voice wake word model",
                present or not enabled,
                f"{v.wake_word_model} present in {model_dir()}"
                if present
                else f"{v.wake_word_model} not downloaded yet (the first `jarvis voice` run fetches it)",
            )
        )
    except Exception as exc:
        checks.append(Check("voice wake word model", not enabled, f"cannot check: {type(exc).__name__}: {exc}"))

    try:
        from jarvis.io.voice import SpeechToText  # noqa: PLC0415

        ffmpeg = shutil.which("ffmpeg") is not None
        stt = SpeechToText.from_config(v, paths)
        cached = SpeechToText.model_cached(stt.model)
        role = "the engine" if stt.primary is None else "the fallback"
        detail = f"{stt.model} ({role}, stt_language {v.stt_language}) " + (
            "cached" if cached else "not downloaded yet (first use fetches it)"
        )
        if not ffmpeg:
            detail += "; ffmpeg missing (`brew install ffmpeg`, needed for Telegram voice notes)"
        checks.append(Check("voice whisper model", True, detail))
    except Exception as exc:
        checks.append(Check("voice whisper model", not enabled, f"cannot check: {type(exc).__name__}: {exc}"))
    checks.append(_engine_check(v, paths, enabled))

    checks.extend(_voice_checks(v, enabled))
    checks.extend(_cue_checks(config, paths))
    return checks + _elevenlabs_checks(config, paths) + _agent_check(enabled) + [_pause_check(paths)]


def _voice_checks(v, enabled: bool) -> list[Check]:
    from jarvis.voice.tts import list_voices, voice_quality  # noqa: PLC0415

    if shutil.which("say") is None:
        return [Check("voice say voices", False, "`say` not found; this needs macOS")]
    voices = list_voices()
    if not voices:
        return [Check("voice say voices", not enabled, "could not list voices (`say -v ?` failed)")]
    checks = []
    tr_quality = voice_quality(v.tts_voice_tr, voices)
    tr_detail = {
        "premium": f"{v.tts_voice_tr} (Premium) installed",
        "enhanced": f"{v.tts_voice_tr} (Enhanced) installed",
        "standard": f"{v.tts_voice_tr} installed, but not the Enhanced/Premium version: System Settings > Accessibility > Spoken Content > System Voice > Manage Voices, download it (see docs/integrations/voice.md)",
        "missing": f"{v.tts_voice_tr} not installed: pick a Turkish voice from `say -v ?` or download it (see docs/integrations/voice.md)",
    }[tr_quality]
    checks.append(Check("voice Turkish voice", tr_quality != "missing", tr_detail))
    en_quality = voice_quality(v.tts_voice_en, voices)
    checks.append(
        Check(
            "voice English voice",
            en_quality != "missing",
            f"{v.tts_voice_en} ({en_quality})"
            if en_quality != "missing"
            else f"{v.tts_voice_en} not installed: set tts_voice_en to one of `say -v ?`",
        )
    )
    return checks


def _cue_checks(config: "Config", paths: "Paths") -> list[Check]:
    """`sounds/voice_heard.*` and `sounds/voice_ready.*`: a missing file is a warning (the macOS
    fallback plays), never an error."""
    from jarvis.voice.cues import CUE_EXTENSIONS, CUES, CuePlayer  # noqa: PLC0415

    player = CuePlayer(paths.sounds_dir, config.voice.sounds)
    checks: list[Check] = []
    for cue in CUES:
        name = f"voice cue {cue}"
        try:
            if not player.enabled(cue):
                checks.append(Check(name, True, f"disabled ({cue}_enabled = false)"))
                continue
            file = player.resolve(cue)
            if file.custom:
                checks.append(Check(name, True, str(file.path)))
            else:
                expected = " / ".join(f"{file.name}{ext}" for ext in CUE_EXTENSIONS)
                fallback = f"playing the macOS fallback {file.path}" if file.path else "no fallback either: silent"
                checks.append(
                    Check(name, True, f"missing: put {expected} into {paths.sounds_dir} (see sounds/README.md); {fallback}", warn=True)
                )
        except Exception as exc:
            checks.append(Check(name, True, f"cannot check: {type(exc).__name__}: {exc}", warn=True))
    return checks


def _elevenlabs_checks(config: "Config", paths: "Paths") -> list[Check]:
    """ElevenLabs. With `tts_engine = "say"` every line is informational (ok=True)."""
    from jarvis.voice import elevenlabs as el  # noqa: PLC0415

    v = config.voice
    cloud = v.tts_engine == "elevenlabs"
    note = "" if cloud else " (tts_engine = say: not needed)"
    checks: list[Check] = []
    try:
        key = el.get_api_key()
    except Exception:
        key = None
    checks.append(
        Check(
            "ElevenLabs key",
            bool(key) or not cloud,
            "set in the Keychain" if key else "missing: run `jarvis voice set-elevenlabs-key`" + note,
        )
    )
    checks.append(
        Check(
            "ElevenLabs voice id",
            bool(v.elevenlabs_voice_id) or not cloud,
            v.elevenlabs_voice_id
            if v.elevenlabs_voice_id
            else "not set: run `jarvis voice list-voices` and put the id in config.toml [voice] elevenlabs_voice_id" + note,
        )
    )
    checks.append(
        Check(
            "ElevenLabs model",
            bool(v.elevenlabs_model) or not cloud,
            v.elevenlabs_model or "elevenlabs_model is empty: set it in config.toml (e.g. eleven_flash_v2_5)" + note,
        )
    )

    if key:
        try:
            sub = el.fetch_subscription(key)
        except Exception:
            sub = None
        if sub is None:
            checks.append(
                Check("ElevenLabs quota", not cloud, "could not fetch /v1/user/subscription (network or key problem; see logs/jarvis.log)")
            )
        else:
            used = int(sub.get("character_count") or 0)
            limit = int(sub.get("character_limit") or 0)
            reset = sub.get("next_character_count_reset_unix")
            try:
                reset_text = datetime.fromtimestamp(int(reset)).strftime("%Y-%m-%d") if reset else "unknown"
            except (TypeError, ValueError, OSError):
                reset_text = "unknown"
            remaining = limit - used
            checks.append(
                Check(
                    "ElevenLabs quota",
                    remaining > 0 or not cloud,
                    f"tier {sub.get('tier', '?')}: {used:,} of {limit:,} characters used, resets {reset_text}"
                    + ("" if remaining > 0 else "; EXHAUSTED: every sentence falls back to say"),
                )
            )
    else:
        checks.append(Check("ElevenLabs quota", True, "skipped (no key)"))

    try:
        detail, ok = _elevenlabs_usage(el, paths, v.elevenlabs_monthly_char_budget)
    except Exception as exc:
        detail, ok = f"cannot read the database: {type(exc).__name__}", True
    checks.append(Check("ElevenLabs usage", ok or not cloud, detail))

    try:
        cache_dir = paths.tts_cache_dir
        count = sum(1 for _ in cache_dir.glob("*.pcm")) if cache_dir.is_dir() else 0
        checks.append(Check("ElevenLabs cache", True, f"{count} cached phrase(s) in {cache_dir}"))
    except Exception as exc:
        checks.append(Check("ElevenLabs cache", True, f"cannot read {paths.tts_cache_dir}: {type(exc).__name__}"))
    return checks


def _elevenlabs_usage(el, paths: "Paths", budget: int) -> tuple[str, bool]:
    if not paths.db_file.is_file():
        return "no usage recorded yet", True
    from jarvis.observability.db import Database  # noqa: PLC0415

    db = Database(paths.db_file)
    try:
        used = db.external_units_since(el.SERVICE, el.start_of_month_utc())
        last = el.last_fallback(db)
    finally:
        db.close()
    detail = f"this month {used:,} characters"
    ok = True
    if budget > 0:
        detail += f" of the {budget:,} budget ({used / budget:.0%})"
        if used >= budget:
            detail += "; BUDGET REACHED: every sentence falls back to say until next month"
            ok = False
    else:
        detail += " (no monthly budget set)"
    if last:
        detail += f"; last fallback {str(last.get('ts', ''))[:19]} ({last.get('reason', '?')})"
    return detail, ok


def _agent_check(enabled: bool) -> list[Check]:
    try:
        from jarvis.voice import launchd  # noqa: PLC0415

        agent = launchd.status()
        return [Check("voice agent", agent.running or not enabled, agent.detail)]
    except Exception as exc:
        return [Check("voice agent", not enabled, f"status unknown: {type(exc).__name__}")]


def _pause_check(paths: "Paths") -> Check:
    from jarvis.voice import pause  # noqa: PLC0415

    state = pause.load_state(paths.voice_pause_file)
    return Check("voice listening", True, pause.describe(state))


def _engine_check(v: Any, paths: Any, enabled: bool) -> Check:
    """The speech recognizer (`voice.stt_engine`); never runs the recognizer (no permission prompt here)."""
    engine = getattr(v, "stt_engine", "whisper")
    if engine == "whisper":
        return Check("voice speech engine", True, f"whisper ({v.stt_model})")
    if v.stt_language not in ("tr", "en"):
        return Check("voice speech engine", True, f'{engine} needs stt_language tr or en; Whisper is used with "auto"')
    try:
        from jarvis.voice.stt_engines import helper_path  # noqa: PLC0415

        helper = helper_path(paths.bin_dir)
    except Exception as exc:
        return Check("voice speech engine", not enabled, f"cannot check: {type(exc).__name__}: {exc}")
    parts = [f"{engine}, Whisper as the fallback"]
    if helper.is_file():
        parts.append("Apple helper compiled")
    elif shutil.which("swiftc"):
        parts.append("Apple helper compiles at the next voice start")
    else:
        return Check(
            "voice speech engine", not enabled, f"{engine}: swiftc missing (`xcode-select --install`); Whisper is used until it is there"
        )
    if engine in ("elevenlabs", "hybrid"):
        from jarvis.voice.elevenlabs import get_api_key  # noqa: PLC0415

        parts.append("ElevenLabs key present" if get_api_key() else "no ElevenLabs key: Apple is used alone")
    return Check("voice speech engine", True, "; ".join(parts))
