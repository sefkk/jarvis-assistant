"""SQLite schema and writes. Parameterized queries only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from jarvis.observability.logging_setup import redact
from jarvis.utils.timeutil import now_iso


def _json(value: Any) -> str:
    """Serialize for storage with the same secret redaction as the log channels."""
    return redact(json.dumps(value, ensure_ascii=False, default=str))


# Each migration is applied once, in order. Add a new entry to change the schema.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE sessions (
            id             TEXT PRIMARY KEY,
            started_at     TEXT NOT NULL,
            ended_at       TEXT,
            model_main     TEXT NOT NULL,
            model_delegate TEXT NOT NULL
        );
        CREATE TABLE turns (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL REFERENCES sessions(id),
            started_at  TEXT NOT NULL,
            user_input  TEXT NOT NULL,
            final_reply TEXT,
            tainted     INTEGER NOT NULL DEFAULT 0,
            total_ms    REAL,
            error       TEXT
        );
        CREATE TABLE messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id      TEXT NOT NULL REFERENCES turns(id),
            role         TEXT NOT NULL,
            content_json TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE tool_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id     TEXT NOT NULL REFERENCES turns(id),
            tool        TEXT NOT NULL,
            args_json   TEXT NOT NULL,
            risk        TEXT NOT NULL,
            decision    TEXT NOT NULL,
            duration_ms REAL,
            error       TEXT,
            is_delegate INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE api_calls (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id            TEXT NOT NULL REFERENCES turns(id),
            model              TEXT NOT NULL,
            attempt            INTEGER NOT NULL,
            status             TEXT NOT NULL,
            duration_ms        REAL,
            ttft_ms            REAL,
            input_tokens       INTEGER,
            output_tokens      INTEGER,
            cache_read_tokens  INTEGER,
            cache_write_tokens INTEGER,
            cost_usd           REAL,
            is_delegate        INTEGER NOT NULL DEFAULT 0,
            created_at         TEXT NOT NULL
        );
        CREATE TABLE timings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id     TEXT NOT NULL REFERENCES turns(id),
            stage       TEXT NOT NULL,
            duration_ms REAL NOT NULL
        );
        CREATE INDEX idx_turns_session ON turns(session_id);
        CREATE INDEX idx_messages_turn ON messages(turn_id);
        CREATE INDEX idx_tool_calls_turn ON tool_calls(turn_id);
        CREATE INDEX idx_api_calls_turn ON api_calls(turn_id);
        CREATE INDEX idx_api_calls_created ON api_calls(created_at);
        CREATE INDEX idx_timings_turn ON timings(turn_id);
        """,
    ),
    (
        2,
        """
        CREATE TABLE workouts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT NOT NULL CHECK (source IN ('manual', 'health')),
            external_id  TEXT,
            started_at   TEXT NOT NULL,
            ended_at     TEXT,
            type         TEXT NOT NULL,
            duration_s   REAL,
            distance_m   REAL,
            energy_kcal  REAL,
            avg_hr       REAL,
            max_hr       REAL,
            notes        TEXT,
            created_at   TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_workouts_external ON workouts(source, external_id) WHERE external_id IS NOT NULL;
        CREATE INDEX idx_workouts_started ON workouts(started_at);
        CREATE TABLE workout_sets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_id  INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
            exercise    TEXT NOT NULL,
            reps        INTEGER,
            distance_m  REAL,
            time_s      REAL,
            weight_kg   REAL,
            rest_s      REAL,
            is_best     INTEGER NOT NULL DEFAULT 0,
            notes       TEXT
        );
        CREATE INDEX idx_workout_sets_workout ON workout_sets(workout_id);
        CREATE TABLE timers (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            label               TEXT NOT NULL,
            fire_at             TEXT NOT NULL,
            kind                TEXT NOT NULL CHECK (kind IN ('timer', 'pomodoro')),
            pomodoro_state_json TEXT,
            status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'fired', 'cancelled')),
            created_at          TEXT NOT NULL,
            fired_at            TEXT
        );
        CREATE INDEX idx_timers_status_fire ON timers(status, fire_at);
        CREATE TABLE health_imports (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            file         TEXT NOT NULL,
            format       TEXT NOT NULL,
            imported_at  TEXT NOT NULL,
            workouts_new INTEGER NOT NULL,
            workouts_dup INTEGER NOT NULL,
            error        TEXT
        );
        """,
    ),
    (
        3,
        """
        CREATE TABLE api_cache (
            key        TEXT PRIMARY KEY,
            payload    TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX idx_api_cache_expires ON api_cache(expires_at);
        CREATE TABLE kv (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        4,
        # Paid third-party calls (ElevenLabs characters, Google Maps requests) for
        # `jarvis usage`, the monthly character budget and the daily Maps call limit.
        """
        CREATE TABLE external_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            service     TEXT NOT NULL,
            operation   TEXT NOT NULL,
            units       INTEGER NOT NULL DEFAULT 1,
            cached      INTEGER NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'ok',
            duration_ms REAL,
            detail      TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX idx_external_calls_service_created ON external_calls(service, created_at);
        """,
    ),
    (
        5,
        # Voice follow-up windows (listening after a reply without the wake word): one row per
        # window, so `jarvis stats` can show how often they are used and how often they were noise.
        # `turn_id` flags the turn a used window became.
        """
        CREATE TABLE voice_follow_ups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT NOT NULL,
            outcome         TEXT NOT NULL,
            turn_id         TEXT REFERENCES turns(id),
            window_s        REAL NOT NULL,
            speech_after_ms REAL
        );
        CREATE INDEX idx_voice_follow_ups_created ON voice_follow_ups(created_at);
        """,
    ),
    (
        6,
        # Internet tests (`internet_test`, the periodic quick check) and presence
        # ("who is home"): the current state per person plus arrivals and departures for the
        # people with `track_history = true`.
        """
        CREATE TABLE network_tests (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at         TEXT NOT NULL,
            kind               TEXT NOT NULL CHECK (kind IN ('full', 'quick')),
            download_mbps      REAL,
            upload_mbps        REAL,
            responsiveness_rpm REAL,
            latency_ms         REAL,
            gateway_latency_ms REAL,
            gateway_loss_pct   REAL,
            online             INTEGER NOT NULL,
            verdict            TEXT NOT NULL,
            likely_cause       TEXT,
            on_home_network    INTEGER,
            wifi_rssi          INTEGER,
            detail_json        TEXT
        );
        CREATE INDEX idx_network_tests_created ON network_tests(created_at);
        CREATE TABLE presence_state (
            person      TEXT PRIMARY KEY,
            status      TEXT NOT NULL CHECK (status IN ('home', 'away', 'unknown')),
            last_seen   TEXT,
            since       TEXT,
            last_device TEXT,
            last_ip     TEXT,
            updated_at  TEXT NOT NULL
        );
        CREATE TABLE presence_events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            person  TEXT NOT NULL,
            event   TEXT NOT NULL CHECK (event IN ('arrived', 'left')),
            at      TEXT NOT NULL,
            device  TEXT
        );
        CREATE INDEX idx_presence_events_person_at ON presence_events(person, at);
        """,
    ),
    (
        7,
        # Which interface a session ran on (cli / telegram / voice): a new session may carry the last
        # minutes of the previous one over a restart or a channel switch, and says where they came from.
        """
        ALTER TABLE sessions ADD COLUMN channel TEXT;
        """,
    ),
    (
        8,
        # Mac power (tools/power.py): a scheduled sleep is a timer of kind 'sleep' plus its 'sleep_notice'
        # one minute before. SQLite cannot change a CHECK constraint in place, so the table is rebuilt.
        """
        CREATE TABLE timers_new (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            label               TEXT NOT NULL,
            fire_at             TEXT NOT NULL,
            kind                TEXT NOT NULL CHECK (kind IN ('timer', 'pomodoro', 'sleep', 'sleep_notice')),
            pomodoro_state_json TEXT,
            status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'fired', 'cancelled')),
            created_at          TEXT NOT NULL,
            fired_at            TEXT
        );
        INSERT INTO timers_new (id, label, fire_at, kind, pomodoro_state_json, status, created_at, fired_at)
            SELECT id, label, fire_at, kind, pomodoro_state_json, status, created_at, fired_at FROM timers;
        DROP TABLE timers;
        ALTER TABLE timers_new RENAME TO timers;
        CREATE INDEX idx_timers_status_fire ON timers(status, fire_at);
        """,
    ),
    (
        9,
        # Alarms (scheduler/alarm.py): a timer of kind 'alarm' whose pomodoro_state_json holds the AlarmState.
        # The CHECK constraint changes again, so the table is rebuilt the same way as in migration 8.
        """
        CREATE TABLE timers_new (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            label               TEXT NOT NULL,
            fire_at             TEXT NOT NULL,
            kind                TEXT NOT NULL CHECK (kind IN ('timer', 'pomodoro', 'sleep', 'sleep_notice', 'alarm')),
            pomodoro_state_json TEXT,
            status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'fired', 'cancelled')),
            created_at          TEXT NOT NULL,
            fired_at            TEXT
        );
        INSERT INTO timers_new (id, label, fire_at, kind, pomodoro_state_json, status, created_at, fired_at)
            SELECT id, label, fire_at, kind, pomodoro_state_json, status, created_at, fired_at FROM timers;
        DROP TABLE timers;
        ALTER TABLE timers_new RENAME TO timers;
        CREATE INDEX idx_timers_status_fire ON timers(status, fire_at);
        """,
    ),
    (
        10,
        # Dashboard analytics: error categories and the prompt composition per API
        # call, one summary row per turn for the per-request table, voice events (wake-word triggers, dropped
        # transcripts) and the indexes the date-range queries need. `Database.migrate` backfills the derivable
        # parts (categories, summaries) for the rows that already exist.
        """
        ALTER TABLE api_calls ADD COLUMN error_category TEXT;
        ALTER TABLE api_calls ADD COLUMN request_id TEXT;
        ALTER TABLE api_calls ADD COLUMN final_failure INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE api_calls ADD COLUMN thinking_tokens INTEGER;
        ALTER TABLE api_calls ADD COLUMN composition_json TEXT;
        CREATE TABLE turn_summaries (
            turn_id            TEXT PRIMARY KEY REFERENCES turns(id),
            session_id         TEXT NOT NULL,
            channel            TEXT,
            started_at         TEXT NOT NULL,
            total_ms           REAL,
            stages_json        TEXT NOT NULL,
            tools_json         TEXT NOT NULL,
            tool_calls         INTEGER NOT NULL DEFAULT 0,
            tool_errors        INTEGER NOT NULL DEFAULT 0,
            api_calls          INTEGER NOT NULL DEFAULT 0,
            api_errors         INTEGER NOT NULL DEFAULT 0,
            model              TEXT,
            input_tokens       INTEGER NOT NULL DEFAULT 0,
            output_tokens      INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd           REAL NOT NULL DEFAULT 0,
            tainted            INTEGER NOT NULL DEFAULT 0,
            confirmations      INTEGER NOT NULL DEFAULT 0,
            denied             INTEGER NOT NULL DEFAULT 0,
            error              TEXT
        );
        CREATE INDEX idx_turn_summaries_started ON turn_summaries(started_at);
        CREATE INDEX idx_turn_summaries_channel ON turn_summaries(channel, started_at);
        CREATE TABLE voice_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            kind       TEXT NOT NULL,
            detail     TEXT
        );
        CREATE INDEX idx_voice_events_created ON voice_events(created_at);
        CREATE INDEX idx_tool_calls_created ON tool_calls(created_at);
        CREATE INDEX idx_turns_started ON turns(started_at);
        """,
    ),
    (
        11,
        # Dashboard Logs: full-text search over what the user said and what Jarvis replied (FTS5, diacritics folded
        # so "kac" finds "kaç"), kept in step by triggers and filled for the existing turns; the raw voice
        # transcript next to the cleaned text the model saw.
        """
        ALTER TABLE turns ADD COLUMN raw_transcript TEXT;
        CREATE VIRTUAL TABLE turns_fts USING fts5(turn_id UNINDEXED, user_input, final_reply, tokenize = 'unicode61 remove_diacritics 2');
        INSERT INTO turns_fts (turn_id, user_input, final_reply) SELECT id, user_input, COALESCE(final_reply, '') FROM turns;
        CREATE TRIGGER turns_fts_insert AFTER INSERT ON turns BEGIN
            INSERT INTO turns_fts (turn_id, user_input, final_reply) VALUES (new.id, new.user_input, COALESCE(new.final_reply, ''));
        END;
        CREATE TRIGGER turns_fts_update AFTER UPDATE OF user_input, final_reply ON turns BEGIN
            DELETE FROM turns_fts WHERE turn_id = old.id;
            INSERT INTO turns_fts (turn_id, user_input, final_reply) VALUES (new.id, new.user_input, COALESCE(new.final_reply, ''));
        END;
        CREATE TRIGGER turns_fts_delete AFTER DELETE ON turns BEGIN
            DELETE FROM turns_fts WHERE turn_id = old.id;
        END;
        """,
    ),
]

MIGRATIONS.append(
    (
        12,
        # Dashboard Settings / Memory / Persona: the staged changes of the dashboard's cart survive page loads here
        # (one row per staged item; applied or discarded together).
        """
    CREATE TABLE dashboard_drafts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        area       TEXT NOT NULL,
        kind       TEXT NOT NULL,
        key        TEXT NOT NULL UNIQUE,
        label      TEXT NOT NULL,
        section    TEXT NOT NULL DEFAULT '',
        file       TEXT NOT NULL,
        old_json   TEXT NOT NULL,
        new_json   TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    )
)

# Python steps that run once, right after the migration of the same version was applied.
POST_MIGRATION_STEPS = {10: ("backfill_error_categories", "backfill_turn_summaries")}


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if path != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._connect()
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        # timeout: wait for a lock instead of failing when the scheduler or another interface
        # writes at the same time (WAL mode lets readers and one writer coexist).
        conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        self._conn = conn
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """The live connection. After `close()` the next use reopens it: the power tools close the
        database before the Mac sleeps, and the same process goes on after the wake. A `:memory:`
        database cannot come back; it stays closed."""
        if self._conn is None:
            if self.path == ":memory:":
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            self._connect()
        assert self._conn is not None
        return self._conn

    @property
    def closed(self) -> bool:
        return self._conn is None

    # --- schema -----------------------------------------------------------------

    def schema_version(self) -> int:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    def migrate(self) -> None:
        current = self.schema_version()
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            self.conn.executescript(sql)
            for step in POST_MIGRATION_STEPS.get(version, ()):
                getattr(self, step)()
            self.conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, now_iso()))

    # --- sessions and turns -----------------------------------------------------

    def start_session(self, session_id: str, model_main: str, model_delegate: str, channel: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO sessions (id, started_at, model_main, model_delegate, channel) VALUES (?, ?, ?, ?, ?)",
            (session_id, now_iso(), model_main, model_delegate, channel),
        )

    def recent_turns(self, *, since_iso: str, limit: int, exclude_session: str | None = None) -> list[dict[str, Any]]:
        """The last `limit` turns started at or after `since_iso`, oldest first, with their session's channel
        (for the carry-over of a conversation across a restart or a channel switch)."""
        rows = self.conn.execute(
            "SELECT t.user_input, t.final_reply, t.started_at, t.tainted, t.error, s.channel FROM turns t"
            " JOIN sessions s ON s.id = t.session_id WHERE t.started_at >= ? AND t.session_id IS NOT ?"
            " ORDER BY t.started_at DESC LIMIT ?",
            (since_iso, exclude_session, int(limit)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def end_session(self, session_id: str) -> None:
        self.conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (now_iso(), session_id))

    def start_turn(self, turn_id: str, session_id: str, user_input: str) -> None:
        self.conn.execute(
            "INSERT INTO turns (id, session_id, started_at, user_input) VALUES (?, ?, ?, ?)",
            (turn_id, session_id, now_iso(), redact(user_input)),
        )

    def finish_turn(self, turn_id: str, *, final_reply: str | None, tainted: bool, total_ms: float, error: str | None) -> None:
        self.conn.execute(
            "UPDATE turns SET final_reply = ?, tainted = ?, total_ms = ?, error = ? WHERE id = ?",
            (redact(final_reply) if final_reply else final_reply, int(tainted), total_ms, error, turn_id),
        )

    # --- transcript and events ---------------------------------------------------

    def add_message(self, turn_id: str, role: str, content: Any) -> None:
        self.conn.execute(
            "INSERT INTO messages (turn_id, role, content_json, created_at) VALUES (?, ?, ?, ?)",
            (turn_id, role, _json(content), now_iso()),
        )

    def add_tool_call(
        self,
        turn_id: str,
        *,
        tool: str,
        args: Any,
        risk: str,
        decision: str,
        duration_ms: float | None,
        error: str | None,
        is_delegate: bool = False,
    ) -> None:
        self.conn.execute(
            "INSERT INTO tool_calls (turn_id, tool, args_json, risk, decision, duration_ms, error, is_delegate, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn_id,
                tool,
                _json(args),
                risk,
                decision,
                duration_ms,
                error,
                int(is_delegate),
                now_iso(),
            ),
        )

    def add_api_call(
        self,
        turn_id: str,
        *,
        model: str,
        attempt: int,
        status: str,
        duration_ms: float | None,
        ttft_ms: float | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_write_tokens: int | None,
        cost_usd: float | None,
        is_delegate: bool = False,
        error_category: str | None = None,
        request_id: str | None = None,
        final_failure: bool = False,
    ) -> int:
        """One API request (an attempt). Returns the row id, so the prompt composition can be attached later."""
        cur = self.conn.execute(
            "INSERT INTO api_calls (turn_id, model, attempt, status, duration_ms, ttft_ms, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, cost_usd, is_delegate, created_at, error_category, request_id, final_failure)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                turn_id,
                model,
                attempt,
                status,
                duration_ms,
                ttft_ms,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                cost_usd,
                int(is_delegate),
                now_iso(),
                error_category,
                request_id,
                int(bool(final_failure)),
            ),
        )
        return int(cur.lastrowid)

    def set_api_call_composition(self, call_id: int, composition: dict[str, Any], thinking_tokens: int | None) -> None:
        """Attach the prompt composition (observability/composition.py) to an api_calls row."""
        self.conn.execute(
            "UPDATE api_calls SET composition_json = ?, thinking_tokens = ? WHERE id = ?",
            (json.dumps(composition, ensure_ascii=False), thinking_tokens, int(call_id)),
        )

    def set_turn_raw_transcript(self, turn_id: str, raw: str | None) -> None:
        """What the speech-to-text model heard before cleaning (voice turns; the Logs page shows both)."""
        self.conn.execute("UPDATE turns SET raw_transcript = ? WHERE id = ?", (redact(raw)[:2000] if raw else None, turn_id))

    def add_voice_event(self, kind: str, detail: str | None = None) -> None:
        """Voice service events the analytics count: `trigger` (detail: wake / hotkey / follow_up), `transcribed`,
        `transcript_dropped` (speech that was noise or a hallucination), `barge_in`."""
        self.conn.execute(
            "INSERT INTO voice_events (created_at, kind, detail) VALUES (?, ?, ?)", (now_iso(), kind, (detail or "")[:200] or None)
        )

    # --- turn summaries: one row per turn, derived from the detail tables ---------------

    def add_turn_summary(self, turn_id: str) -> dict[str, Any] | None:
        """Write (or rewrite) the summary row of a turn from turns, sessions, timings, tool_calls and api_calls.
        Called when a turn finishes and again when the voice service adds its stages. Returns the row."""
        row = self.conn.execute(
            "SELECT t.id, t.session_id, t.started_at, t.total_ms, t.tainted, t.error, s.channel"
            " FROM turns t LEFT JOIN sessions s ON s.id = t.session_id WHERE t.id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        stages: dict[str, float] = {}
        for r in self.conn.execute("SELECT stage, SUM(duration_ms) AS ms FROM timings WHERE turn_id = ? GROUP BY stage", (turn_id,)):
            stages[r["stage"]] = round(float(r["ms"]), 1)
        tools = [r["tool"] for r in self.conn.execute("SELECT tool FROM tool_calls WHERE turn_id = ? ORDER BY id", (turn_id,))]
        tool_stats = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(decision IN ('error', 'validation_error')), 0) AS errors,"
            " COALESCE(SUM(decision = 'confirmed'), 0) AS confirmed, COALESCE(SUM(decision = 'denied'), 0) AS denied"
            " FROM tool_calls WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        api = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(status != 'ok'), 0) AS errors, COALESCE(SUM(input_tokens), 0) AS inp,"
            " COALESCE(SUM(output_tokens), 0) AS outp, COALESCE(SUM(cache_read_tokens), 0) AS cr,"
            " COALESCE(SUM(cache_write_tokens), 0) AS cw, COALESCE(SUM(cost_usd), 0) AS cost"
            " FROM api_calls WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        model_row = self.conn.execute(
            "SELECT model FROM api_calls WHERE turn_id = ? AND is_delegate = 0 ORDER BY id LIMIT 1", (turn_id,)
        ).fetchone()
        summary = {
            "turn_id": turn_id,
            "session_id": row["session_id"],
            "channel": row["channel"] or "cli",
            "started_at": row["started_at"],
            "total_ms": row["total_ms"],
            "stages_json": json.dumps(stages),
            "tools_json": json.dumps(tools),
            "tool_calls": int(tool_stats["n"]),
            "tool_errors": int(tool_stats["errors"]),
            "api_calls": int(api["n"]),
            "api_errors": int(api["errors"]),
            "model": model_row["model"] if model_row else None,
            "input_tokens": int(api["inp"]),
            "output_tokens": int(api["outp"]),
            "cache_read_tokens": int(api["cr"]),
            "cache_write_tokens": int(api["cw"]),
            "cost_usd": float(api["cost"]),
            "tainted": int(row["tainted"] or 0),
            "confirmations": int(tool_stats["confirmed"]),
            "denied": int(tool_stats["denied"]),
            "error": row["error"],
        }
        columns = ", ".join(summary)
        placeholders = ", ".join("?" * len(summary))
        self.conn.execute(f"INSERT OR REPLACE INTO turn_summaries ({columns}) VALUES ({placeholders})", tuple(summary.values()))
        return summary

    def backfill_turn_summaries(self) -> int:
        """Summaries for every turn without one (migration 10 and `jarvis doctor` repairs). Returns the count."""
        ids = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM turns WHERE id NOT IN (SELECT turn_id FROM turn_summaries) ORDER BY started_at"
            ).fetchall()
        ]
        for turn_id in ids:
            self.add_turn_summary(turn_id)
        return len(ids)

    def backfill_error_categories(self) -> int:
        """Derive `error_category` and `final_failure` for failed api_calls rows written before migration 10."""
        from jarvis.observability.usage import classify_status  # noqa: PLC0415 - usage imports this module

        rows = self.conn.execute(
            "SELECT id, turn_id, status FROM api_calls WHERE status != 'ok' AND error_category IS NULL ORDER BY id"
        ).fetchall()
        for r in rows:
            later_ok = self.conn.execute(
                "SELECT 1 FROM api_calls WHERE turn_id = ? AND id > ? AND status = 'ok' LIMIT 1", (r["turn_id"], r["id"])
            ).fetchone()
            later_error = self.conn.execute(
                "SELECT 1 FROM api_calls WHERE turn_id = ? AND id > ? AND status != 'ok' LIMIT 1", (r["turn_id"], r["id"])
            ).fetchone()
            final = later_ok is None and later_error is None
            self.conn.execute(
                "UPDATE api_calls SET error_category = ?, final_failure = ? WHERE id = ?",
                (classify_status(r["status"]), int(final), r["id"]),
            )
        return len(rows)

    def add_timing(self, turn_id: str, stage: str, duration_ms: float) -> None:
        self.conn.execute("INSERT INTO timings (turn_id, stage, duration_ms) VALUES (?, ?, ?)", (turn_id, stage, duration_ms))

    # --- voice follow-up windows ------------------------------------------------------

    FOLLOW_UP_OUTCOMES = ("turn", "pause", "silence", "noise", "cancelled")

    def add_follow_up(self, outcome: str, *, window_s: float, turn_id: str | None = None, speech_after_ms: float | None = None) -> None:
        """One follow-up window: `turn` (became a turn, `turn_id` set), `pause` (a pause phrase),
        `silence` (nobody spoke), `noise` (speech, but the transcript was dropped), `cancelled`."""
        if outcome not in self.FOLLOW_UP_OUTCOMES:
            raise ValueError(f"unknown follow-up outcome {outcome!r}")
        self.conn.execute(
            "INSERT INTO voice_follow_ups (created_at, outcome, turn_id, window_s, speech_after_ms) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), outcome, turn_id, float(window_s), speech_after_ms),
        )

    def follow_up_counts(self, since: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM voice_follow_ups WHERE created_at >= ? GROUP BY outcome", (since,)
        ).fetchall()
        return {r["outcome"]: int(r["n"]) for r in rows}

    # --- cache and key-value ---------------------------------------------------------

    def cache_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT payload FROM api_cache WHERE key = ? AND expires_at > ?", (key, now_iso())).fetchone()
        return row["payload"] if row else None

    def cache_set(self, key: str, payload: str, ttl_s: float) -> None:
        from datetime import datetime, timedelta, timezone

        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.conn.execute(
            "INSERT OR REPLACE INTO api_cache (key, payload, fetched_at, expires_at) VALUES (?, ?, ?, ?)",
            (key, payload, now_iso(), expires),
        )

    def cache_prune(self) -> int:
        cur = self.conn.execute("DELETE FROM api_cache WHERE expires_at <= ?", (now_iso(),))
        return cur.rowcount

    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)", (key, value, now_iso()))

    # --- external (paid) services: ElevenLabs, Google Maps ------------------------------

    def add_external_call(
        self,
        *,
        service: str,
        operation: str,
        units: int = 1,
        cached: bool = False,
        status: str = "ok",
        duration_ms: float | None = None,
        detail: str | None = None,
    ) -> None:
        """One row per request to a paid service. `units` = characters (ElevenLabs) or 1 (Maps)."""
        self.conn.execute(
            "INSERT INTO external_calls (service, operation, units, cached, status, duration_ms, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (service, operation, int(units), int(cached), status, duration_ms, redact(detail) if detail else detail, now_iso()),
        )

    def external_units_since(self, service: str, since_iso: str, *, include_cached: bool = False) -> int:
        """Sum of `units` for a service since a UTC ISO prefix (budget and daily-limit checks)."""
        sql = "SELECT COALESCE(SUM(units), 0) AS total FROM external_calls WHERE service = ? AND created_at >= ? AND status = 'ok'"
        if not include_cached:
            sql += " AND cached = 0"
        row = self.conn.execute(sql, (service, since_iso)).fetchone()
        return int(row["total"])

    def external_calls_since(self, service: str, since_iso: str, *, include_cached: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM external_calls WHERE service = ? AND created_at >= ? AND status = 'ok'"
        if not include_cached:
            sql += " AND cached = 0"
        return int(self.conn.execute(sql, (service, since_iso)).fetchone()["n"])

    # --- reads ---------------------------------------------------------------------

    def spent_since(self, since_iso: str) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM api_calls WHERE created_at >= ?", (since_iso,)).fetchone()
        return float(row["total"])

    def close(self) -> None:
        """Close the connection (WAL is checkpointed by SQLite on the last close). Idempotent."""
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()
