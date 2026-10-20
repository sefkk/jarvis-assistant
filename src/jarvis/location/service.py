"""What the rest of Jarvis calls: refresh the Current Location (in the background at session
start, lazily by age from the tools, on demand from `jarvis location refresh`), write the one
auto-managed block of profile.md, answer the tools' "where is the user now" question with the
channel rules applied, and the lines for the CLI and `jarvis doctor`.

Privacy rules enforced here: logs and audit lines carry the source and the town only (never
coordinates or a street); profile.md receives the label at `location.profile_precision` and
never coordinates; a remote (Telegram) turn never takes the Mac's location as the user's.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from jarvis.i18n import t
from jarvis.location import sources as location_sources
from jarvis.location.model import Location
from jarvis.location.sources import SourceError
from jarvis.location.store import LocationState, append_history, load_state, save_state
from jarvis.observability import logging_setup
from jarvis.utils.text import fold

if TYPE_CHECKING:
    from jarvis.config import Config, Paths
    from jarvis.observability.health import Check
    from jarvis.tools.base import ToolContext

log = logging.getLogger("jarvis.location")

LOCAL_CHANNELS = ("cli", "voice")  # everything else is remote: the Mac's location is not the user's there
DEBOUNCE_S = 60.0  # a session start right after a refresh does not refresh again
HOME_ALIASES = frozenset({"home", "my home", "house", "my house", "ev", "eve", "evim", "evime", "evimiz", "evimize", "ev adresi", "evin"})

ASK_FOR_LOCATION = (
    "Current location unknown from the phone: on a remote turn the Mac's location is not the user's. "
    "Ask the user to share their location in Telegram (attach > Location, or /location for the button) or to name a place."
)

_inflight = threading.Lock()


# --- the refresh -------------------------------------------------------------------------------------


@dataclass
class RefreshResult:
    location: Location | None  # the location now in effect (new, or the last known one after a failure)
    refreshed: bool  # True when a source answered on this run
    source_errors: dict[str, str]  # source -> short reason, in the order tried
    skipped: str | None = None  # why nothing ran (disabled, debounce)
    profile_updated: bool = False

    @property
    def error(self) -> str | None:
        if self.refreshed or self.skipped:
            return None
        return "; ".join(f"{name}: {why}" for name, why in self.source_errors.items()) or "no source available"


def _sources_for(config: "Config") -> list[tuple[str, Callable[..., Location | None]]]:
    """Shortcut, CoreLocationCLI, IP: read from the sources module at call time (tests replace them)."""
    return [(name, location_sources.SOURCES[name]) for name in location_sources.SOURCE_ORDER if name in location_sources.SOURCES]


def refresh(
    config: "Config",
    paths: "Paths",
    *,
    sources: list[tuple[str, Callable[..., Location | None]]] | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> RefreshResult:
    """Try the sources in order (Shortcut, CoreLocationCLI, IP), store the first answer, rewrite the
    profile block. On failure the last known location stays and the error is recorded. Never raises."""
    location_config = config.location
    if not location_config.enabled:
        return RefreshResult(load_state(paths.location_state_file).mac, False, {}, skipped="location.enabled = false")
    state = load_state(paths.location_state_file)
    now = now or datetime.now(timezone.utc)
    if not force and state.mac is not None and state.mac.age_seconds(now) < DEBOUNCE_S:
        return RefreshResult(state.mac, False, {}, skipped=f"refreshed {state.mac.age_seconds(now):.0f} s ago")
    stamp = _iso(now)
    errors: dict[str, str] = {}
    found: Location | None = None
    for name, fn in sources if sources is not None else _sources_for(config):
        try:
            found = fn(config, timeout=float(location_config.timeout_s), now_iso=stamp)
        except SourceError as exc:
            errors[name] = str(exc)[:300]
            continue
        except Exception as exc:  # a broken source must never stop the others or the caller
            errors[name] = f"{type(exc).__name__}"
            log.warning("location source %s crashed: %s", name, type(exc).__name__)
            continue
        if found is not None:
            break
        errors[name] = "no answer"
    state.last_attempt_at = stamp
    if found is None:
        state.last_error = "; ".join(f"{k}: {v}" for k, v in errors.items())[:300] or "no source available"
        _save(paths, state)
        log.warning("location refresh failed", extra={"data": {"errors": list(errors)}})
        return RefreshResult(state.mac, False, errors)
    state.mac = found
    state.last_error = None
    _save(paths, state)
    if location_config.keep_history:
        try:
            append_history(paths.location_history_file, found)
        except OSError as exc:
            log.warning("location history not written: %s", type(exc).__name__)
    profile_updated = write_profile_block(paths, found, location_config.profile_precision)
    log.info(
        "location refreshed",
        extra={"data": {"source": found.source, "place": found.place or "?", "approximate": found.approximate, "tried": list(errors)}},
    )
    logging_setup.audit({"tool": "location", "decision": "refreshed", "source": found.source, "place": found.place or "?"})
    return RefreshResult(found, True, errors, profile_updated=profile_updated)


def _save(paths: "Paths", state: LocationState) -> None:
    try:
        save_state(paths.location_state_file, state)
    except OSError as exc:
        log.warning("location state not written: %s", type(exc).__name__)


def _iso(now: datetime) -> str:
    now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def refresh_in_background(config: "Config", paths: "Paths", **kwargs: Any) -> threading.Thread | None:
    """Session start: run `refresh` in a daemon thread and return at once (startup must not wait
    for a Shortcut or the network). One refresh at a time per process; None when nothing started."""
    if not config.location.enabled or not config.location.refresh_on_start:
        return None
    if not _inflight.acquire(blocking=False):
        return None

    def worker() -> None:
        try:
            refresh(config, paths, **kwargs)
        except Exception:  # the thread must never die loudly
            log.exception("background location refresh crashed")
        finally:
            _inflight.release()

    thread = threading.Thread(target=worker, name="jarvis-location", daemon=True)
    thread.start()
    return thread


def ensure_fresh(config: "Config", paths: "Paths", *, now: datetime | None = None, **kwargs: Any) -> Location | None:
    """Lazy refresh for the tools: the Mac's location when it is younger than `location.max_age_min`,
    else a refresh first (bounded by the source timeouts), else the last known one (or None)."""
    state = load_state(paths.location_state_file)
    now = now or datetime.now(timezone.utc)
    max_age_s = config.location.max_age_min * 60
    if state.mac is not None and state.mac.age_seconds(now) <= max_age_s:
        return state.mac
    if not config.location.enabled:
        return state.mac
    return refresh(config, paths, now=now, force=True, **kwargs).location


# --- profile.md ---------------------------------------------------------------------------------------


def profile_line(location: Location, precision: str) -> str:
    """`- Current Location: Kadıköy, İstanbul (updated 2026-10-16 17:30, source: shortcut)`; the
    label follows the precision, an IP location is marked approximate, coordinates never appear."""
    label = location.label(precision) or "address unknown"
    when = location.updated_datetime().astimezone().strftime("%Y-%m-%d %H:%M")
    notes = []
    if location.approximate:
        notes.append("approximate, IP-based")
    notes.append(f"updated {when}, source: {location.source}")
    return f"- Current Location: {label} ({'; '.join(notes)})"


def write_profile_block(paths: "Paths", location: Location, precision: str) -> bool:
    """Rewrite only the auto-managed block of profile.md (created when missing). False when the
    profile does not exist yet (nothing to add a block to) or the write failed."""
    from jarvis.profile import AUTO_BLOCK_KEY, read_profile_text, replace_auto_block  # noqa: PLC0415
    from jarvis.utils.fileio import atomic_write_text  # noqa: PLC0415

    text = read_profile_text(paths.profile_file)
    if text is None:
        return False
    new_text = replace_auto_block(text, AUTO_BLOCK_KEY, profile_line(location, precision))
    if new_text == text:
        return True
    try:
        atomic_write_text(paths.profile_file, new_text)
    except OSError as exc:
        log.warning("profile.md not updated: %s", type(exc).__name__)
        return False
    return True


# --- what the tools ask ---------------------------------------------------------------------------------


@dataclass
class Resolved:
    """The user's current location as seen from one channel."""

    location: Location | None
    remote: bool
    stale: Location | None = None  # remote: a shared location that is older than max_age_min
    error: str | None = None  # local: why the last refresh failed (the location may be the last known one)

    def describe(self, precision: str = "neighborhood", *, now: datetime | None = None) -> str:
        """`Kadıköy, İstanbul (current location, source shortcut, 12 min ago)` for tool results."""
        assert self.location is not None
        loc = self.location
        label = loc.label(precision) or "address unknown"
        if not label or label == "address unknown":
            label = "the shared location" if loc.source == "telegram" else "address unknown"
        bits = ["current location"]
        if loc.approximate:
            bits.append("approximate, IP-based")
        bits.append(f"source {loc.source}")
        bits.append(age_text(loc.age_seconds(now), "en"))
        return f"{label} ({', '.join(bits)})"


def channel_of(ctx: "ToolContext | None") -> str:
    runtime = getattr(ctx, "runtime", None) if ctx is not None else None
    return str(getattr(runtime, "channel", None) or "cli")


def current_location(ctx: "ToolContext", *, now: datetime | None = None) -> Resolved:
    """For a local turn (cli, voice): the Mac's location, refreshed first when older than
    `max_age_min`, else the last known one. For a remote turn: only a location the user shared on
    Telegram and only while it is fresh; the Mac's location is never used."""
    now = now or datetime.now(timezone.utc)
    config, paths = ctx.config, ctx.paths
    if channel_of(ctx) not in LOCAL_CHANNELS:
        shared = load_state(paths.location_state_file).telegram
        if shared is None:
            return Resolved(None, remote=True)
        if shared.age_seconds(now) <= config.location.max_age_min * 60:
            return Resolved(shared, remote=True)
        return Resolved(None, remote=True, stale=shared)
    location = ensure_fresh(config, paths, now=now)
    error = (
        load_state(paths.location_state_file).last_error
        if location is None or location.age_seconds(now) > config.location.max_age_min * 60
        else None
    )
    return Resolved(location, remote=False, error=error)


def ask_for_location_text(resolved: Resolved, *, now: datetime | None = None) -> str:
    """The tool result for a remote turn without a fresh shared location (the model relays it)."""
    text = ASK_FOR_LOCATION
    if resolved.stale is not None:
        text += f" Last shared location: {resolved.stale.label('city') or 'coordinates only'}, {age_text(resolved.stale.age_seconds(now), 'en')}."
    return text


@dataclass
class Place:
    """Where a location-dependent tool should look when the user named no place."""

    kind: str  # current | home | fixed | ask
    query: str | None = None  # text to geocode (home / fixed)
    point: tuple[float, float] | None = None  # exact coordinates (current); never shown to the model
    label: str = ""  # how the tool result names it, so the model can say which location was used
    ask: str | None = None  # remote turn without a fresh shared location: the tool returns this text


def resolve_default_place(
    ctx: "ToolContext",
    *,
    order: tuple[str, ...] = ("current", "home"),
    fixed: str = "",
    what: str = "location",
    now: datetime | None = None,
) -> Place:
    """The fallback chain for weather and maps: `current` (the Current Location with the channel rules),
    `home` (the profile's Location line), `fixed` (a configured place). Raises ToolValidationError when
    the chain yields nothing; a remote turn without a shared location yields `kind="ask"`. The label
    says what was used ("(home; current location unknown)") so the model can tell the user."""
    from jarvis.exceptions import ToolValidationError  # noqa: PLC0415

    precision = ctx.config.location.profile_precision
    current_unknown = False
    for kind in order:
        if kind == "current":
            resolved = current_location(ctx, now=now)
            if resolved.location is not None:
                return Place(
                    "current", point=(resolved.location.latitude, resolved.location.longitude), label=resolved.describe(precision, now=now)
                )
            if resolved.remote:
                return Place("ask", ask=ask_for_location_text(resolved, now=now))
            current_unknown = True
        elif kind == "home":
            home = home_location(ctx.paths)
            if home:
                return Place("home", query=home, label=f"{home} (home" + ("; current location unknown)" if current_unknown else ")"))
        elif kind == "fixed" and fixed.strip():
            return Place(
                "fixed",
                query=fixed.strip(),
                label=f"{fixed.strip()} (configured fallback" + ("; current location unknown)" if current_unknown else ")"),
            )
    raise ToolValidationError(
        f"no {what} given and no location in profile.md"
        + (" (and the current location is unknown)" if current_unknown else "")
        + "; run `jarvis setup` for the home Location"
        + (" or `jarvis location refresh`" if current_unknown else "")
    )


def home_location(paths: "Paths") -> str | None:
    """The permanent `Location` line of profile.md (where the user lives). None when not set."""
    from jarvis.profile import load_profile  # noqa: PLC0415

    try:
        profile = load_profile(paths.profile_file)
    except OSError:
        return None
    if profile is None:
        return None
    return profile.fields.get("location", "").strip() or None


def is_home_alias(text: str | None) -> bool:
    """'eve', 'home', 'evime': the user means the home Location."""
    return _fold(text) in HOME_ALIASES


# --- the origin of a route ------------------------------------------------------------------------------------
# "X'e nasıl giderim", "X'e yol tarifi", "X'e ne kadar sürer" start from where the user is now. The home
# Location is the origin only when the user says so ("evden", "evimden", "from home"); "eve" makes home the
# destination. The model tends to copy the profile's `Current Location` label or the home `Location` into
# `origin`: the first is less exact than the stored coordinates, the second is wrong whenever the user is out.

_FROM_HOME_RE = re.compile(r"\b(evden|evimden|evimizden|evdeki|evdeyim|evdeyiz)\b|\bfrom (my |our |the )?(home|house)\b")
LAST_KNOWN_ALIASES = frozenset({"last known", "last known location", "son bilinen", "son bilinen konum", "son konum"})

ASK_FOR_ORIGIN = (
    "Current location unknown on this Mac{detail}. Do not assume the home Location: tell the user briefly and ask "
    "where they are starting from, or whether to use home (then call again with origin = 'home'){last_known}."
)


def _fold(text: str | None) -> str:
    """Case-insensitive in both languages (every i-like letter is "i"), apostrophes as spaces."""
    return " ".join(fold(text or "").replace("'", " ").replace("\u2019", " ").split())


def from_home_requested(user_text: str | None) -> bool:
    """'Evden', 'evimden', 'evdeyim', 'from home': the user said the route starts at home. 'Eve' is not that."""
    return bool(user_text) and _FROM_HOME_RE.search(_fold(user_text)) is not None


def user_named(user_text: str | None, place: str) -> bool:
    """Did the user's own message mention this place? The first comma-part ("Kadıköy" of "Kadıköy, İstanbul"),
    case-insensitive, apostrophes ignored, so "Kadıköy'den" counts."""
    head = _fold(place.split(",")[0])
    return bool(user_text) and bool(head) and head in _fold(user_text)


def is_last_known_alias(text: str | None) -> bool:
    return _fold(text) in LAST_KNOWN_ALIASES


def _is_current_label(paths: "Paths", text: str) -> bool:
    """Is `text` one of the labels the Current Location is shown under (the profile line, any precision)?"""
    key = _fold(text)
    if not key:
        return False
    state = load_state(paths.location_state_file)
    for loc in (state.mac, state.telegram):
        if loc is None:
            continue
        for precision in ("city", "neighborhood", "street"):
            if _fold(loc.label(precision)) == key:
                return True
        if loc.city and loc.region and key in (_fold(f"{loc.city}, {loc.region}"), _fold(f"{loc.city} {loc.region}")):
            return True
    return False


@dataclass
class OriginCheck:
    origin: str | None  # the origin to use; None = the user named none: the current location
    note: str | None = None  # why a model-given origin was ignored (goes into the tool result so the model learns)


def check_origin(ctx: "ToolContext", origin: str | None) -> OriginCheck:
    """Apply the origin rule to what the model passed. 'home' (or the home Location text) stays only when the
    user's message says the route starts at home; the Current Location label is dropped unless the user said
    it, so the exact stored location is used. Without the user's message (no runtime: scripts, some tests)
    nothing is overridden."""
    if origin is None:
        return OriginCheck(None)
    user_text = getattr(getattr(ctx, "runtime", None), "user_text", None)
    if not user_text:
        return OriginCheck(origin)
    home = home_location(ctx.paths)
    if is_home_alias(origin) or (home is not None and _fold(origin) == _fold(home)):
        if from_home_requested(user_text):
            return OriginCheck(origin)
        return OriginCheck(
            None,
            (
                f"Note: origin {origin!r} was ignored: the user did not say the route starts at home (no 'evden' / 'from home'), "
                "so the current location is used. Pass origin = 'home' only when they do."
            ),
        )
    if not user_named(user_text, origin) and _is_current_label(ctx.paths, origin):
        return OriginCheck(
            None,
            (
                f"Note: origin {origin!r} was ignored: that is the Current Location label from the profile, the exact "
                "current location is used instead. Leave origin empty unless the user names a starting point."
            ),
        )
    return OriginCheck(origin)


def resolve_origin(ctx: "ToolContext", *, now: datetime | None = None) -> Place:
    """Where a route starts when the user named no origin: the fresh Current Location (local turns: the Mac's,
    refreshed first when older than `max_age_min`; remote turns: the location shared on Telegram). Never the
    home Location: when nothing fresh is known the Place is `kind="ask"` and its text tells the model what to
    ask. A stale location is offered as 'last known' (origin = 'last known' uses it)."""
    now = now or datetime.now(timezone.utc)
    resolved = current_location(ctx, now=now)
    max_age_s = ctx.config.location.max_age_min * 60
    loc = resolved.location
    if loc is not None and loc.age_seconds(now) <= max_age_s:
        return Place(
            "current", point=(loc.latitude, loc.longitude), label=resolved.describe(ctx.config.location.profile_precision, now=now)
        )
    if resolved.remote:
        return Place("ask", ask=ask_for_location_text(resolved, now=now))
    detail = ""
    last_known = ""
    if loc is not None:
        detail = f" (last known: {loc.label('city') or 'coordinates only'}, {age_text(loc.age_seconds(now), 'en')}, older than {ctx.config.location.max_age_min} min)"
        last_known = ", or with origin = 'last known' to use that older location if they say it is still right"
    if resolved.error:
        detail += f"; last refresh failed: {resolved.error}"
    return Place("ask", ask=ASK_FOR_ORIGIN.format(detail=detail, last_known=last_known))


def last_known_place(ctx: "ToolContext", *, now: datetime | None = None) -> Place | None:
    """The stored location whatever its age (origin = 'last known' after `resolve_origin` offered it); None when
    there is none for this channel."""
    now = now or datetime.now(timezone.utc)
    state = load_state(ctx.paths.location_state_file)
    loc = state.telegram if channel_of(ctx) not in LOCAL_CHANNELS else state.mac
    if loc is None:
        return None
    label = loc.label(ctx.config.location.profile_precision) or ("the shared location" if loc.source == "telegram" else "address unknown")
    return Place(
        "current",
        point=(loc.latitude, loc.longitude),
        label=f"{label} (last known location, source {loc.source}, {age_text(loc.age_seconds(now), 'en')})",
    )


def store_telegram_location(
    paths: "Paths", latitude: float, longitude: float, *, accuracy_m: float | None = None, now: datetime | None = None
) -> Location:
    """A location the user shared in the Telegram chat. Raises ValueError for invalid coordinates."""
    now = now or datetime.now(timezone.utc)
    location = Location(latitude=latitude, longitude=longitude, source="telegram", updated_at=_iso(now), accuracy_m=accuracy_m)
    state = load_state(paths.location_state_file)
    state.telegram = location
    save_state(paths.location_state_file, state)
    log.info("telegram location stored", extra={"data": {"source": "telegram"}})
    logging_setup.audit({"tool": "location", "decision": "telegram_location", "source": "telegram"})
    return location


# --- text helpers ---------------------------------------------------------------------------------------


def age_text(seconds: float, lang: str | None = None) -> str:
    """'just now' / '12 min ago' / '3 h ago' / '2 days ago', in `lang` (default: the main language)."""
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    if minutes < 1:
        return t("location.age.just_now", lang)
    if minutes < 60:
        return t("location.age.minutes", lang, n=minutes)
    hours = minutes // 60
    if hours < 48:
        return t("location.age.hours", lang, n=hours)
    return t("location.age.days", lang, n=hours // 24)


def last_known_text(location: Location, *, now: datetime | None = None, precision: str = "neighborhood") -> str:
    """'<place> (last known location, 3 h ago, source: shortcut)', for the CLI and notices."""
    label = location.label(precision) or t("location.last_known.address_unknown")
    return t("location.last_known.text", label=label, age=age_text(location.age_seconds(now)), source=location.source)


def status_lines(config: "Config", paths: "Paths", *, now: datetime | None = None) -> list[str]:
    """`jarvis location`: the Mac's location, source and age, the shared Telegram location, the
    profile line, and the last error. The user's own terminal, so the coordinates are shown too."""
    now = now or datetime.now(timezone.utc)
    state = load_state(paths.location_state_file)
    precision = config.location.profile_precision
    lines: list[str] = []
    if not config.location.enabled:
        lines.append(t("location.status.disabled"))
    if state.mac is None:
        lines.append(t("location.status.mac_unknown"))
    else:
        loc = state.mac
        fresh = loc.age_seconds(now) <= config.location.max_age_min * 60
        label = loc.label("street") or t("location.last_known.address_unknown")
        extra = t("location.status.approximate") if loc.approximate else ""
        lines.append(t("location.status.mac" if fresh else "location.status.mac_stale", label=label, extra=extra))
        lines.append(
            t(
                "location.status.details",
                coordinates=loc.coordinates_text(4),
                source=loc.source,
                updated=loc.updated_datetime().astimezone().strftime("%Y-%m-%d %H:%M"),
                age=age_text(loc.age_seconds(now)),
            )
        )
        lines.append(t("location.status.profile_line", precision=precision, line=profile_line(loc, precision)[2:]))
    if state.telegram is not None:
        tg = state.telegram
        lines.append(t("location.status.telegram", coordinates=tg.coordinates_text(4), age=age_text(tg.age_seconds(now))))
    else:
        lines.append(t("location.status.telegram_none"))
    if state.last_error:
        when = ""
        if state.last_attempt_at:
            from jarvis.location.model import parse_iso  # noqa: PLC0415

            when = f" ({age_text((now - parse_iso(state.last_attempt_at)).total_seconds())})"
        lines.append(t("location.status.last_error", when=when, error=state.last_error))
    lines.append(
        t(
            "location.status.settings",
            max_age=config.location.max_age_min,
            timeout=f"{config.location.timeout_s:g}",
            precision=precision,
            history=t("location.status.on" if config.location.keep_history else "location.status.off"),
            shortcut=config.location.shortcut_name,
            ip_off="" if config.location.ip_fallback else t("location.status.ip_off"),
        )
    )
    return lines


# --- jarvis doctor -----------------------------------------------------------------------------------------


def _fallback_text(lc) -> str:
    """What answers instead of the Shortcut, for the doctor lines."""
    return (
        "CoreLocationCLI (if installed) or the IP lookup is used"
        if lc.ip_fallback
        else "CoreLocationCLI is used if installed, else the home Location"
    )


def health_checks(config: "Config", paths: "Paths") -> list["Check"]:
    """Sources available, state age, and the deprecated weather key. Never raises, no lookup."""
    from jarvis.observability.health import Check  # noqa: PLC0415

    checks: list[Check] = []
    lc = config.location
    if not lc.enabled:
        checks.append(Check("location", True, "disabled (location.enabled = false)"))
        return checks
    entry = config.shortcuts.entry(lc.shortcut_name)
    if entry is None:
        checks.append(
            Check(
                "location shortcut",
                True,
                f"{lc.shortcut_name!r} not allowlisted under [[shortcuts.allowed]]; {_fallback_text(lc)} (docs/integrations/location.md)",
                warn=True,
            )
        )
    else:
        try:
            from jarvis.tools.shortcuts import installed_shortcuts  # noqa: PLC0415

            present = {" ".join(n.casefold().split()) for n in installed_shortcuts()}
            ok = " ".join(entry.name.casefold().split()) in present
        except Exception:
            ok = False
        # missing is a warning, not a failure: the fallbacks keep the feature working (the Shortcuts allowlist check flags it too)
        checks.append(
            Check(
                "location shortcut",
                True,
                f"{entry.name!r} in the Shortcuts app"
                if ok
                else f"{entry.name!r} is allowlisted but not in the Shortcuts app: create it (docs/integrations/location.md); until then {_fallback_text(lc)}",
                warn=not ok,
            )
        )
    checks.append(
        Check(
            "location CoreLocationCLI",
            True,
            "installed" if location_sources.corelocation_installed() else "optional, not installed (brew install --cask corelocationcli)",
        )
    )
    state = load_state(paths.location_state_file)
    if state.mac is None:
        checks.append(
            Check("current location", True, "not detected yet (detected at the next session start; `jarvis location refresh`)", warn=True)
        )
    else:
        age = age_text(state.mac.age_seconds(), "en")
        checks.append(
            Check(
                "current location",
                True,
                f"{state.mac.label('city') or 'address unknown'}, source {state.mac.source}, {age}"
                + ("; last refresh failed: " + state.last_error if state.last_error else ""),
            )
        )
    if config.weather.default_location:
        checks.append(
            Check(
                "weather.default_location",
                True,
                "deprecated: rename it to `fixed_location` (and pick `location_source`); it is still read this release",
                warn=True,
            )
        )
    return checks
