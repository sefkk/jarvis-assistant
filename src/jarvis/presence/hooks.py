"""Optional automations on presence events, all off by default:

- `presence.on_arrive_greet`: when I arrive home, the voice service greets me with a short brief.
  Only if the voice agent is running and listening is not paused; the greeting is a `turn`
  announcement, so the model writes it in the user's language.
- `presence.on_leave_pause_voice`: pause voice listening when the Mac leaves the home network and
  resume when it returns. The resume only undoes a pause made here (`by == "presence"`): a manual
  pause ("stop listening", "sesi kapat") is never undone.
- `presence.notify_person_changes`: a notification when a configured person arrives or leaves.

A hook is a (name, predicate, action) triple; every action runs only when its flag is on and its
failures are logged, never raised (the scheduler must keep going).

# TODO(setup): more automations (e.g. Shortcuts / IKEA lights on arrival)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from jarvis.i18n import t
from jarvis.voice import announce
from jarvis.voice import pause as vpause

if TYPE_CHECKING:
    from jarvis.config import Config, Paths, PresenceConfig
    from jarvis.presence.engine import Event
    from jarvis.scheduler.notify import Notifier

log = logging.getLogger("jarvis.presence")

PAUSED_BY = "presence"
NOTIFICATION_TITLE = "Jarvis"
GREET_PROMPT = (
    "System event: the user just arrived home. Greet them warmly by name in one short spoken sentence, "
    "then give a very short brief of the rest of today (call daily_brief; two or three spoken sentences "
    "at most, no lists). Reply in the user's language ({language})."
)

VoiceStatus = Callable[[], bool]
Predicate = Callable[["Event", "PresenceConfig"], bool]
Action = Callable[["Event", "Config", "Paths", "Notifier | None", VoiceStatus], None]


def voice_agent_running() -> bool:
    """Whether the launchd voice agent is running; False when launchctl cannot be asked."""
    try:
        from jarvis.voice import launchd  # noqa: PLC0415 - launchctl wrapper, imported lazily

        return bool(launchd.status().running)
    except Exception:
        return False


# --- actions ---------------------------------------------------------------------------------------------


def greet(event: "Event", config: "Config", paths: "Paths", notifier: "Notifier | None", voice_status: VoiceStatus) -> None:
    if not voice_status():
        log.info("presence: arrive-greet skipped, the voice agent is not running")
        return
    if vpause.load_state(paths.voice_pause_file).paused:
        log.info("presence: arrive-greet skipped, voice listening is paused")
        return
    announce.request(paths.voice_announce_file, kind="turn", source=PAUSED_BY, text=GREET_PROMPT.format(language=config.general.language))
    log.info("presence: arrive-greet requested for %s", event.person or "me")


def pause_voice(event: "Event", config: "Config", paths: "Paths", notifier: "Notifier | None", voice_status: VoiceStatus) -> None:
    if vpause.load_state(paths.voice_pause_file).paused:
        log.info("presence: Mac left the home network, voice listening is already paused")
        return
    vpause.pause(paths.voice_pause_file, by=PAUSED_BY)
    log.info("presence: Mac left the home network, voice listening paused")


def resume_voice(event: "Event", config: "Config", paths: "Paths", notifier: "Notifier | None", voice_status: VoiceStatus) -> None:
    state = vpause.load_state(paths.voice_pause_file)
    if state.by != PAUSED_BY:
        if state.paused:
            log.info("presence: Mac is back home, but listening was paused by %s; leaving it", state.by or "someone else")
        return
    vpause.resume(paths.voice_pause_file)
    log.info("presence: Mac is back on the home network, voice listening resumed")


def local_only(notifier: "Notifier", config: "Config") -> "Notifier":
    """Presence notifications stay on the Mac unless `allow_remote` is true: the scheduler's notifier
    may include Telegram (`timers.notify_channels`), and a stolen phone must not learn who left the
    house. Keeps the `mac` channel(s) of a multi notifier, else builds a MacNotifier."""
    from jarvis.scheduler.notify import MacNotifier, MultiNotifier  # noqa: PLC0415

    if config.presence.allow_remote:
        return notifier
    if isinstance(notifier, MultiNotifier):
        macs = [c for c in notifier.channels if c.name == "mac"]
        if macs:
            return macs[0] if len(macs) == 1 else MultiNotifier(macs)
    elif notifier.name == "mac":
        return notifier
    return MacNotifier(config.timers.notification_sound)


def notify_change(event: "Event", config: "Config", paths: "Paths", notifier: "Notifier | None", voice_status: VoiceStatus) -> None:
    if notifier is None:
        log.info("presence: no notifier, %s not announced", event.kind)
        return
    key = "presence.notify.arrived" if event.kind == "person_arrived" else "presence.notify.left"
    local_only(notifier, config).notify(NOTIFICATION_TITLE, t(key, name=event.person))


HOOKS: list[tuple[str, Predicate, Action]] = [
    ("arrive_greet", lambda e, p: p.on_arrive_greet and e.kind == "i_arrived_home", greet),
    ("leave_pause_voice", lambda e, p: p.on_leave_pause_voice and e.kind == "mac_left_home_network", pause_voice),
    ("return_resume_voice", lambda e, p: p.on_leave_pause_voice and e.kind == "mac_joined_home_network", resume_voice),
    ("notify_person_changes", lambda e, p: p.notify_person_changes and e.kind in ("person_arrived", "person_left"), notify_change),
]


def dispatch(
    events: list["Event"],
    config: "Config",
    paths: "Paths",
    notifier: "Notifier | None",
    *,
    voice_status: VoiceStatus | None = None,
) -> list[str]:
    """Run every enabled hook for every event. Returns the names of the hooks that ran."""
    voice_status = voice_status or voice_agent_running
    ran: list[str] = []
    for event in events:
        for name, matches, action in HOOKS:
            try:
                if not matches(event, config.presence):
                    continue
                action(event, config, paths, notifier, voice_status)
                ran.append(name)
            except Exception:
                log.exception("presence hook %s failed on %s", name, event.kind)
    return ran
