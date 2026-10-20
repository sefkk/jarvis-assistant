"""The Assistant: the whole brain behind one small public surface.

`handle(text, sink)` streams a reply into the sink and returns metadata.
`new_session()` starts a fresh session. The Assistant never knows where input came from.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from time import monotonic
from typing import Any, Callable

import anthropic

from jarvis import context, memory_pass
from jarvis.brain import Brain
from jarvis.config import Config, LoopConfig, Paths
from jarvis.exceptions import JarvisAPIError
from jarvis.i18n import t
from jarvis.io.base import OutputSink
from jarvis.location import service as location_service
from jarvis.memory import MemoryStore
from jarvis.observability import logging_setup
from jarvis.observability.db import Database
from jarvis.observability.timing import TurnTimer
from jarvis.observability.usage import BudgetTracker, TokenUsage, UsageTracker
from jarvis.profile import read_profile_text
from jarvis.security import sanitize
from jarvis.tools.base import Runtime, ToolContext
from jarvis.tools.registry import ToolRegistry
from jarvis.utils import secrets
from jarvis.utils.timeutil import now_iso, now_utc

log = logging.getLogger("jarvis.assistant")


@dataclass
class TurnResult:
    turn_id: str
    session_id: str
    reply: str
    tools_used: list[str] = field(default_factory=list)
    tainted: bool = False
    usage: TokenUsage = field(default_factory=TokenUsage)
    total_ms: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    compacted: bool = False


def api_timeout(loop: LoopConfig) -> anthropic.Timeout:
    """The granular timeouts for the SDK's HTTP client (`[loop]`). `connect` covers TCP + TLS; `read`
    is the longest silence between two chunks of a streamed reply (httpx applies it per read, so it
    is a stalled-stream detector, not a limit on the whole answer); `pool` waits for a free connection."""
    return anthropic.Timeout(
        connect=loop.api_connect_timeout_s,
        read=loop.api_read_timeout_s,
        write=loop.api_write_timeout_s,
        pool=loop.api_connect_timeout_s,
    )


def make_client(api_key: str | None = None, *, loop: LoopConfig | None = None) -> anthropic.Anthropic:
    """SDK client with built-in retries disabled (the Brain implements its own) and the
    configured timeouts. Without `loop` the config defaults apply (never the SDK's 5 s connect)."""
    key = api_key or secrets.get_secret(secrets.ANTHROPIC_API_KEY)
    if not key:
        raise JarvisAPIError("no Anthropic API key found (Keychain or ANTHROPIC_API_KEY)")
    return anthropic.Anthropic(api_key=key, max_retries=0, timeout=api_timeout(loop or LoopConfig()))


class Assistant:
    def __init__(
        self,
        config: Config,
        paths: Paths,
        *,
        client: anthropic.Anthropic | None = None,
        registry: ToolRegistry | None = None,
        db: Database | None = None,
        debug: bool = False,
        channel: str = "cli",
    ) -> None:
        self.config = config
        self.paths = paths
        self.debug = debug
        self.channel = channel  # cli / telegram / voice: told to the model in the system prompt
        self.client = client or make_client(loop=config.loop)
        self.registry = registry or ToolRegistry.discover(ToolContext.build(config, paths))
        self.db = db or Database(paths.db_file)
        self.usage_tracker = UsageTracker(config, self.db)
        self.budget = BudgetTracker(config, self.db)
        self.brain = Brain(self.client, config, self.registry, self.db, self.usage_tracker, debug=debug)
        self.runtime = Runtime(
            client=self.client,
            db=self.db,
            usage_tracker=self.usage_tracker,
            registry=self.registry,
            budget=self.budget,
            debug=debug,
            channel=channel,
        )
        for tool in self.registry:  # tools such as delegate reach the live objects through their context
            if tool.ctx is not None:
                tool.ctx.runtime = self.runtime
        self.persona, self.rules = context.load_prompts(paths, config.general.language)
        self.memory = MemoryStore(paths.memory_file)
        self.history = context.History()
        self.profile_text: str | None = None
        self._profile_mtime: int | None = None
        self._last_system: list[dict[str, Any]] | None = None
        self._last_prompt_tokens = 0
        self._baseline_prompt_tokens: int | None = None  # the first call of the session: system prompt + tool schemas
        # The end-of-session memory pass: how many of this session's turns it has already read, the time of
        # the last turn (for the idle trigger) and where its "📝 Noted" lines go when no turn is running
        # (set by the interface; None = log only, which is what voice wants).
        self._memory_pass_watermark = 0
        self._turns_in_session = 0
        self._last_turn_at: float | None = None
        self._last_turn_id: str | None = None
        self.memory_notify: Callable[[str], None] | None = None
        self.session_id = ""
        self.new_session(resume=True)

    # --- sessions ---------------------------------------------------------------------------

    def new_session(self, *, resume: bool = False) -> str:
        """Start a fresh session. With `resume`, the last turns of the previous minutes (any session, any
        interface) are carried over as one plain-text turn (`context.resume_after_min`), so a restart or
        a switch from voice to the terminal does not lose the question Jarvis just asked. An explicit
        new session (Telegram /new) does not resume."""
        if self.session_id:
            self.run_memory_pass("session_end")
            self.db.end_session(self.session_id)
        previous = self.session_id
        self.session_id = uuid.uuid4().hex[:12]
        self.history.clear()
        self._memory_pass_watermark = 0
        self._turns_in_session = 0
        self._last_turn_at = None
        self._last_turn_id = None
        self._last_system = None
        self._last_prompt_tokens = 0
        self._baseline_prompt_tokens = None
        self._read_profile()  # read at the session start, re-read when Jarvis's own location block changes
        logging_setup.set_session_id(self.session_id)
        logging_setup.set_turn_id(None)
        self.db.start_session(self.session_id, self.config.models.main_model, self.config.models.delegate_model, self.channel)
        if resume:
            self._carry_over(previous)
        if self.channel != "telegram":  # the Mac's location is not the user's on a remote turn
            location_service.refresh_in_background(self.config, self.paths)
        log.info(
            "session started",
            extra={
                "data": {
                    "model": self.config.models.main_model,
                    "channel": self.channel,
                    "tools": self.registry.names(),
                    "profile": self.profile_text is not None,
                }
            },
        )
        return self.session_id

    def _carry_over(self, previous_session: str) -> bool:
        """Replay the last turns of the previous `context.resume_after_min` minutes as one text turn."""
        minutes = self.config.context.resume_after_min
        limit = max(self.config.context.keep_recent_turns, 1)
        if minutes <= 0:
            return False
        since = (now_utc() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            rows = self.db.recent_turns(since_iso=since, limit=limit, exclude_session=self.session_id)
            turn = context.carry_over_turn(rows, now_iso=now_iso(), channel=self.channel)
        except Exception:  # a broken transcript must never stop a session from starting
            log.exception("carry-over of the previous session failed")
            return False
        if turn is None:
            return False
        self.history.add(turn)
        log.info(
            "carried over %d turn(s) from the previous %d minute(s)",
            len(rows),
            minutes,
            extra={"data": {"channels": sorted({r.get("channel") or "?" for r in rows}), "tainted": turn.tainted}},
        )
        return True

    def _profile_stamp(self) -> int | None:
        try:
            return self.paths.profile_file.stat().st_mtime_ns
        except OSError:
            return None

    def _read_profile(self) -> None:
        self._profile_mtime = self._profile_stamp()
        self.profile_text = read_profile_text(self.paths.profile_file)

    def refresh_profile_if_changed(self) -> bool:
        """profile.md is the user's file, read once per session, except that the background location
        refresh rewrites its auto-managed block: a changed modification time re-reads it (one stat per
        turn), so the Current Location reaches the model in the same session."""
        if self._profile_stamp() == self._profile_mtime:
            return False
        self._read_profile()
        return True

    @property
    def has_profile(self) -> bool:
        return self.profile_text is not None

    # --- prompt assembly ----------------------------------------------------------------------

    def build_system(self) -> list[dict[str, Any]]:
        """Persona, rules, profile, memory. Memory is re-read every turn (tools or the user may change it)."""
        self.refresh_profile_if_changed()
        system = context.build_system(
            self.persona,
            self.rules,
            self.profile_text,
            self.memory.for_prompt(),
            language=self.config.general.language,
            channel=self.channel,
            humor_level=self.config.persona.humor_level,
            memory_auto=self.config.memory.auto,
            team=self.config.football.team,
        )
        if self._last_system is not None and system != self._last_system:
            # The prefix changed, so replayed thinking blocks would be rejected (models that
            # always think return them even when the config says off, so never skip this).
            removed = self.history.strip_thinking()
            if removed:
                log.info("system prompt changed; dropped %d thinking blocks from history", removed)
        self._last_system = system
        return system

    @property
    def conversation_tokens(self) -> int:
        """How much of the last prompt was conversation: the whole prompt minus the session's first call
        (system prompt, profile, memory and the tool schemas, which never shrink by compaction)."""
        if self._baseline_prompt_tokens is None:
            return 0
        return max(0, self._last_prompt_tokens - self._baseline_prompt_tokens)

    def maybe_compact(self, turn_id: str, timer: TurnTimer, sink: OutputSink) -> bool:
        if self.conversation_tokens < self.config.context.compact_after_tokens:
            return False
        if len(self.history) <= max(self.config.context.keep_recent_turns, 1):
            # Few but large turns: nothing to summarize without touching the recent ones.
            # TODO(setup): decide whether to compact by size in that case (would summarize recent turns).
            log.warning(
                "prompt is %d tokens (over %d) but only %d turn(s) are in history; nothing to compact",
                self._last_prompt_tokens,
                self.config.context.compact_after_tokens,
                len(self.history),
            )
            return False
        sink.notice(t("cli.notice.compacting"))
        with timer.stage("compaction"):
            result = context.compact(self.history, self.brain, keep_recent_turns=self.config.context.keep_recent_turns, turn_id=turn_id)
        if result is None:
            return False
        self._last_prompt_tokens = 0  # unknown until the next call; avoid compacting twice in a row
        return True

    # --- turns ---------------------------------------------------------------------------------

    def handle(self, text: str, sink: OutputSink, *, untrusted: list[tuple[str, str]] | None = None, spoken: bool = False) -> TurnResult:
        """One turn. `untrusted` carries content that arrived with the message but was not typed
        by the user (for example a forwarded Telegram message), as (source, text) pairs: it is
        wrapped as untrusted and taints the turn from the start."""
        text = sanitize.user_input(text)
        idle_pass = self._idle_pass_due()
        turn_id = uuid.uuid4().hex[:12]
        logging_setup.set_turn_id(turn_id)
        timer = TurnTimer(turn_id, self.db)
        self.db.start_turn(turn_id, self.session_id, text)
        log.info("turn started", extra={"data": {"chars": len(text)}})
        result = TurnResult(turn_id=turn_id, session_id=self.session_id, reply="")
        self.runtime.sink = sink
        self.runtime.turn_id = turn_id
        self.runtime.user_text = text
        self.runtime.spoken = spoken or self.channel == "voice"
        self.runtime.taint = None
        user_content: Any = text
        initial_taint = None

        try:
            result.compacted = self.maybe_compact(turn_id, timer, sink)
            with timer.stage("prompt_build"):
                system = self.build_system()
                messages = self.history.as_messages()
            start = len(messages)
            if untrusted:
                parts = [{"type": "text", "text": text}] if text.strip() else []
                initial_taint = context.TaintState()
                for source, body in untrusted:
                    cleaned = sanitize.for_model(body)[: self.config.context.tool_result_max_chars]
                    parts.append({"type": "text", "text": sanitize.wrap_untrusted(cleaned, source)})
                    initial_taint.mark(source)
                user_content = parts
            else:
                user_content = text
            messages.append({"role": "user", "content": user_content})
            if self.config.general.save_transcripts:
                self.db.add_message(turn_id, "user", user_content)

            outcome = self.brain.run_turn(
                system=system,
                messages=messages,
                sink=sink,
                turn_id=turn_id,
                timer=timer,
                user_text=text,
                taint=initial_taint,
            )
            self.history.add(context.Turn(messages=messages[start:], tainted=outcome.tainted))
            self._last_prompt_tokens = outcome.last_prompt_tokens
            if self._baseline_prompt_tokens is None and outcome.first_prompt_tokens:
                self._baseline_prompt_tokens = outcome.first_prompt_tokens
            result.reply = outcome.text
            result.tools_used = outcome.tools_used
            result.tainted = outcome.tainted
            result.usage = outcome.usage
        except JarvisAPIError as exc:
            # The user's message stays in the history (marked failed) so the next turn still sees what
            # they said: an answer to a question Jarvis asked must not vanish with a timeout. Nothing else
            # from the turn is kept, so the history never ends with an unanswered tool_use.
            self._keep_failed_turn(user_content, initial_taint)
            result.error = exc.one_line()
            language = self.config.general.language
            sink.error(exc.user_message(language), spoken=exc.spoken_message(language))
            log.error(
                "turn failed: %s",
                exc,
                extra={
                    "data": {
                        "attempts": exc.attempts,
                        "category": exc.category,
                        "phase": exc.phase,
                        "status_code": exc.status_code,
                        "error_type": exc.error_type,
                        "request_id": exc.request_id,
                    }
                },
                exc_info=True,
            )
        except Exception as exc:  # only config and startup failures may stop the program
            self._keep_failed_turn(user_content, initial_taint)
            result.error = f"{type(exc).__name__}: {exc}"
            sink.error(t("cli.notice.turn_crashed", error=type(exc).__name__))
            log.exception("turn crashed")
        finally:
            budget_notice = self.budget.notice()
            if budget_notice:
                sink.notice(budget_notice)
                log.warning(budget_notice)
            sink.finish()
            self.runtime.sink = None
            self.runtime.turn_id = None
            self.runtime.user_text = None
            self.runtime.spoken = False
            self.runtime.taint = None
            result.total_ms = timer.elapsed_ms()
            result.timings = timer.summary()
            self.db.finish_turn(
                turn_id,
                final_reply=result.reply or None,
                tainted=result.tainted,
                total_ms=result.total_ms,
                error=result.error,
            )
            try:
                self.db.add_turn_summary(turn_id)  # the dashboard's per-request row
            except Exception:
                log.warning("turn summary failed", exc_info=True)
            self._turns_in_session += 1
            self._last_turn_at = monotonic()
            self._last_turn_id = turn_id
            if idle_pass:  # the turns before the quiet gap, after this reply so it adds no latency to it
                self.run_memory_pass("idle", before_turns=self._turns_in_session - 1, sink=sink)
            log.info(
                "turn finished",
                extra={
                    "data": {
                        "total_ms": round(result.total_ms, 1),
                        "tools": result.tools_used,
                        "tainted": result.tainted,
                        "usage": result.usage,
                        "prompt_tokens": self._last_prompt_tokens,
                        "turns_in_history": len(self.history),
                        "error": result.error,
                    }
                },
            )
            logging_setup.set_turn_id(None)
        return result

    # --- the end-of-session memory pass ---------------------------------------------------------------

    def _idle_pass_due(self) -> bool:
        minutes = self.config.memory.idle_pass_min
        if not self.config.memory.end_of_session_pass or minutes <= 0 or self._last_turn_at is None:
            return False
        if self._memory_pass_watermark >= self._turns_in_session:
            return False
        return monotonic() - self._last_turn_at >= minutes * 60

    def run_memory_pass(
        self, reason: str, *, before_turns: int | None = None, sink: OutputSink | None = None
    ) -> memory_pass.PassResult | None:
        """Read the session's turns the pass has not seen yet and save the durable facts the user stated
        (`memory.end_of_session_pass`). `before_turns` limits the pass to the first N turns of the session
        (the idle trigger: the turns before the gap). The notes go to the running turn's sink, else to
        `memory_notify`, else only to the log (voice)."""
        if not self.config.memory.end_of_session_pass or not self.session_id:
            return None
        end = self._turns_in_session if before_turns is None else min(before_turns, self._turns_in_session)
        if end <= self._memory_pass_watermark:
            return None
        rows = self.db.conn.execute(
            "SELECT id, user_input, final_reply, tainted, error FROM turns WHERE session_id = ? ORDER BY started_at, rowid",
            (self.session_id,),
        ).fetchall()
        turns = [dict(r) for r in rows][self._memory_pass_watermark : end]
        self._memory_pass_watermark = end
        if not turns:
            return None
        notify: Callable[[str], None] | None = None
        if self.config.memory.auto_notify:
            notify = sink.notice if sink is not None else self.memory_notify
        with TurnTimer(turns[-1]["id"], self.db).stage("memory_pass"):
            result = memory_pass.run_pass(
                brain=self.brain,
                store=self.memory,
                turns=turns,
                turn_id=turns[-1]["id"],
                max_facts=self.config.memory.max_facts_per_pass,
                notify=notify,
                reason=reason,
            )
        return result

    def _keep_failed_turn(self, user_content: Any, taint: context.TaintState | None) -> None:
        if isinstance(user_content, str) and not user_content.strip():
            return
        self.history.add(
            context.Turn(messages=[{"role": "user", "content": user_content}], tainted=bool(taint and taint.tainted), failed=True)
        )

    def close(self) -> None:
        if self.session_id:
            self.run_memory_pass("exit")
            self.db.end_session(self.session_id)
            self.session_id = ""
            self.db.close()
