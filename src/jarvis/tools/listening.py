"""`pause_listening`: pause the hands-free voice service from any channel ("sesi kapat",
"dinlemeyi durdur", "stop listening", with an optional duration). SAFE: it only writes the pause
file under JARVIS_HOME; the voice service picks it up within a second, releases the microphone and
shows a notification. Resuming is deliberately not a tool (the microphone is closed, so voice
cannot resume itself): `jarvis voice resume`, Telegram `/unmute`, the hotkey dialog, or the timer.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from jarvis.tools.base import RiskLevel, Tool, ToolResult
from jarvis.voice import pause as vpause


class PauseListeningInput(BaseModel):
    minutes: int | None = Field(
        default=None,
        ge=1,
        le=vpause.MAX_PAUSE_MINUTES,
        description="Pause for this many minutes, then listen again automatically ('1 hour' / '1 saat' = 60). Omit for an open-ended pause.",
    )


class PauseListening(Tool):
    name = "pause_listening"
    description = (
        "Pause Jarvis's hands-free voice listening on the Mac: the microphone is released and the wake word stops "
        "working until `jarvis voice resume`, /unmute on Telegram, the hotkey dialog, or until the given minutes pass. "
        "Use when the user says 'sesi kapat', 'dinlemeyi durdur', 'stop listening', 'mute', optionally with a duration. "
        "The service keeps running; nothing is uninstalled."
    )
    input_model = PauseListeningInput
    risk = RiskLevel.SAFE
    delegate_ok = False
    taints = False

    def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, PauseListeningInput)
        assert self.ctx is not None
        by = self.ctx.runtime.channel if self.ctx.runtime is not None else "tool"
        state = vpause.pause(self.ctx.paths.voice_pause_file, by=by, minutes=args.minutes)
        if args.minutes:
            return ToolResult(
                f"Voice listening paused for {vpause.duration_label(args.minutes, 'en')}; it resumes automatically at "
                f"{vpause._local(state.until)}. The microphone is released within a second."
            )
        return ToolResult(
            "Voice listening paused until resumed (`jarvis voice resume`, /unmute, or the hotkey dialog). The microphone is released within a second."
        )
