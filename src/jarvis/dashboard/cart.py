"""The cart: every edit on the Settings, Memory and Persona pages is staged here, in the
`dashboard_drafts` table, and applied together when the user presses Save in the drawer. Applying runs the
seven steps in a background thread: conflict check (is the value the item changed still what it
was?), validation of the whole resulting config, a backup under data/backups/<timestamp>/, an all-or-nothing
write (tomlkit for the TOML files), a graceful restart of only the affected services, a health check, and a
rollback to the backup when writing or restarting fails. The backups double as the History page."""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError

from jarvis import services
from jarvis.config import Config, load_config, merge_config_data
from jarvis.dashboard import settings as settings_mod
from jarvis.dashboard import tomlwrite
from jarvis.dashboard.ui_text import UI
from jarvis.memory import MemoryStore, sanitize_entry
from jarvis.observability import health
from jarvis.observability.db import Database
from jarvis.profile import AUTO_BLOCK_KEY, auto_block_body, find_auto_block, replace_auto_block
from jarvis.utils.timeutil import now_iso, now_local, today_local

if TYPE_CHECKING:
    from jarvis.config import Paths

log = logging.getLogger("jarvis.dashboard.cart")

AREA_SETTINGS, AREA_MEMORY, AREA_PERSONA, AREA_PROFILE = "settings", "memory", "persona", "profile"
AREA_LABELS = {
    AREA_SETTINGS: UI.GROUP_SETTINGS,
    AREA_MEMORY: UI.GROUP_MEMORY,
    AREA_PERSONA: UI.GROUP_PERSONA,
    AREA_PROFILE: UI.GROUP_PROFILE,
}
KIND_SETTING, KIND_MEMORY_EDIT, KIND_MEMORY_DELETE, KIND_MEMORY_ADD, KIND_TEXT, KIND_RESTORE = (
    "setting",
    "memory_edit",
    "memory_delete",
    "memory_add",
    "text",
    "file_restore",
)
FILE_CONFIG, FILE_LOCAL, FILE_MEMORY, FILE_PROFILE, FILE_PERSONA = (
    "config.toml",
    "config.local.toml",
    "memory.md",
    "profile.md",
    "persona.md",
)
TEXT_RESTARTS = {FILE_PERSONA: settings_mod.BOTH, FILE_PROFILE: (), FILE_MEMORY: ()}
STEPS = (
    ("conflicts", UI.STEP_CONFLICTS),
    ("validate", UI.STEP_VALIDATING),
    ("backup", UI.STEP_BACKUP),
    ("write", UI.STEP_WRITING),
    ("restart", UI.STEP_RESTARTING),
    ("health", UI.STEP_HEALTH),
    ("done", UI.STEP_DONE),
)
IDLE_WAIT_S = 60.0
IDLE_POLL_S = 2.0
HEALTH_WAIT_S = 25.0
TURN_IN_PROGRESS_WINDOW_MIN = 10


# --- items -----------------------------------------------------------------------------------------------------------


@dataclass
class Item:
    id: int
    area: str
    kind: str
    key: str
    label: str
    section: str
    file: str
    old: Any
    new: Any
    created_at: str = ""
    updated_at: str = ""
    error: str | None = None  # validation (transient)
    conflict: bool = False  # transient

    @property
    def area_label(self) -> str:
        return AREA_LABELS.get(self.area, self.area)

    @property
    def restarts(self) -> tuple[str, ...]:
        if self.kind == KIND_SETTING:
            return settings_mod.restarts_for(self.key.split(":", 1)[1])
        if self.kind == KIND_RESTORE and self.file in (FILE_CONFIG, FILE_LOCAL):
            return settings_mod.BOTH
        return TEXT_RESTARTS.get(self.file, ())

    @property
    def effect(self) -> str:
        return settings_mod.effect_text(self.restarts)

    @property
    def is_text(self) -> bool:
        return self.kind in (KIND_TEXT, KIND_RESTORE, KIND_MEMORY_EDIT)

    @property
    def old_text(self) -> str:
        return settings_mod.display_value(self.old) if self.kind == KIND_SETTING else str(self.old or "")

    @property
    def new_text(self) -> str:
        return settings_mod.display_value(self.new) if self.kind == KIND_SETTING else str(self.new or "")

    def diff(self, context: int = 1) -> list[tuple[str, str]]:
        """(kind, line) pairs for a compact text diff: kind is add, del or ctx."""
        old_lines = str(self.old or "").splitlines()
        new_lines = str(self.new or "").splitlines()
        out: list[tuple[str, str]] = []
        for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=context):
            if line.startswith(("---", "+++", "@@")):
                if line.startswith("@@") and out:
                    out.append(("gap", "…"))
                continue
            kind = "add" if line.startswith("+") else "del" if line.startswith("-") else "ctx"
            out.append((kind, line[1:]))
        return out[:400]

    @property
    def page(self) -> str:
        return {AREA_SETTINGS: "/settings", AREA_MEMORY: "/memory", AREA_PERSONA: "/persona", AREA_PROFILE: "/persona?tab=profile"}.get(
            self.area, "/"
        )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def profile_user_text(text: str) -> str:
    """profile.md without Jarvis's auto-managed Current Location block (what the editor shows and compares)."""
    span = find_auto_block(text, AUTO_BLOCK_KEY)
    if span is None:
        return text
    before, after = text[: span[0]], text[span[1] :]
    return (before.rstrip("\n") + "\n\n" + after.lstrip("\n")).strip("\n") + ("\n" if text.endswith("\n") else "")


# --- the cart --------------------------------------------------------------------------------------------------------


class Cart:
    def __init__(self, config: Config, paths: "Paths", *, db_factory: Callable[[], Database] | None = None) -> None:
        self.config = config
        self.paths = paths
        self._db_factory = db_factory or (lambda: Database(paths.db_file))
        self.job: ApplyJob | None = None
        self._lock = threading.Lock()

    # --- current state of the files ---

    def current_config(self) -> Config:
        return load_config(self.paths.config_file)

    def memory_lines(self) -> list[str]:
        return MemoryStore(self.paths.memory_file).lines()

    def read_text(self, name: str) -> str:
        path = self.file_path(name)
        if name == FILE_PERSONA and not path.is_file():
            # no persona of the user's own yet: the editor starts from the shipped one; saving writes data/persona.md
            from jarvis import i18n

            path = self.paths.default_persona_file(i18n.current_language())
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        return profile_user_text(text) if name == FILE_PROFILE else text

    def file_path(self, name: str) -> Path:
        return {
            FILE_CONFIG: self.paths.config_file,
            FILE_LOCAL: self.paths.local_config_file,
            FILE_MEMORY: self.paths.memory_file,
            FILE_PROFILE: self.paths.profile_file,
            FILE_PERSONA: self.paths.persona_file,
        }[name]

    # --- storage ---

    def items(self) -> list[Item]:
        db = self._db_factory()
        try:
            rows = db.conn.execute("SELECT * FROM dashboard_drafts ORDER BY id").fetchall()
        finally:
            db.close()
        return [
            Item(
                id=r["id"],
                area=r["area"],
                kind=r["kind"],
                key=r["key"],
                label=r["label"],
                section=r["section"],
                file=r["file"],
                old=json.loads(r["old_json"]),
                new=json.loads(r["new_json"]),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def count(self) -> int:
        db = self._db_factory()
        try:
            return int(db.conn.execute("SELECT COUNT(*) FROM dashboard_drafts").fetchone()[0])
        except Exception:
            return 0
        finally:
            db.close()

    def item(self, item_id: int) -> Item | None:
        return next((i for i in self.items() if i.id == int(item_id)), None)

    def by_key(self, key: str) -> Item | None:
        return next((i for i in self.items() if i.key == key), None)

    def staged_settings(self) -> dict[str, Any]:
        return {i.key.split(":", 1)[1]: i.new for i in self.items() if i.kind == KIND_SETTING}

    def _upsert(self, *, area: str, kind: str, key: str, label: str, section: str, file: str, old: Any, new: Any) -> Item | None:
        """Stage, replace or (when new equals old) remove the item with this key."""
        db = self._db_factory()
        try:
            if new == old:
                db.conn.execute("DELETE FROM dashboard_drafts WHERE key = ?", (key,))
                return None
            now = now_iso()
            db.conn.execute(
                "INSERT INTO dashboard_drafts (area, kind, key, label, section, file, old_json, new_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET new_json = excluded.new_json, label = excluded.label,"
                " updated_at = excluded.updated_at",
                (area, kind, key, label, section, file, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False), now, now),
            )
            row = db.conn.execute("SELECT * FROM dashboard_drafts WHERE key = ?", (key,)).fetchone()
        finally:
            db.close()
        return Item(
            id=row["id"],
            area=area,
            kind=kind,
            key=key,
            label=label,
            section=section,
            file=file,
            old=old,
            new=new,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def remove(self, item_id: int) -> None:
        db = self._db_factory()
        try:
            db.conn.execute("DELETE FROM dashboard_drafts WHERE id = ?", (int(item_id),))
        finally:
            db.close()

    def remove_key(self, key: str) -> None:
        db = self._db_factory()
        try:
            db.conn.execute("DELETE FROM dashboard_drafts WHERE key = ?", (key,))
        finally:
            db.close()

    def clear(self) -> None:
        db = self._db_factory()
        try:
            db.conn.execute("DELETE FROM dashboard_drafts")
        finally:
            db.close()

    # --- staging ---

    def stage_setting(self, path: str, raw: str | None) -> tuple[Item | None, str | None]:
        """Coerce and stage one setting; (item or None when it equals the current value, error text)."""
        config = self.current_config()
        spec = settings_mod.field_spec(config, path)
        if spec is None:
            return None, "unknown setting"
        if spec.read_only:
            return None, UI.READ_ONLY
        try:
            value = settings_mod.coerce(spec, raw)
        except ValueError as exc:
            return None, str(exc)
        local_data = _read_toml(self.paths.local_config_file)
        target = tomlwrite.target_file(self.paths.config_file, local_data, path)
        file_name = FILE_LOCAL if target == self.paths.local_config_file else FILE_CONFIG
        section_title = settings_mod.SECTIONS.get(spec.section, (spec.section, ()))[0]
        item = self._upsert(
            area=AREA_SETTINGS,
            kind=KIND_SETTING,
            key=f"setting:{path}",
            label=spec.label,
            section=section_title,
            file=file_name,
            old=spec.value,
            new=value,
        )
        return item, None

    def stage_memory_edit(self, line: str, new_text: str) -> tuple[Item | None, str | None]:
        if line not in self.memory_lines():
            return None, UI.CONFLICT_TEXT
        try:
            clean = sanitize_entry(new_text)
        except Exception:
            return None, UI.MEMORY_EMPTY_TEXT
        new_line = _rebuild_line(line, clean)
        item = self._upsert(
            area=AREA_MEMORY,
            kind=KIND_MEMORY_EDIT,
            key=f"memory:{_hash(line)}",
            label=UI.MEMORY_EDIT,
            section=UI.GROUP_MEMORY,
            file=FILE_MEMORY,
            old=line,
            new=new_line,
        )
        return item, None

    def stage_memory_delete(self, line: str) -> tuple[Item | None, str | None]:
        if line not in self.memory_lines():
            return None, UI.CONFLICT_TEXT
        item = self._upsert(
            area=AREA_MEMORY,
            kind=KIND_MEMORY_DELETE,
            key=f"memory:{_hash(line)}",
            label=UI.MEMORY_DIFF_DELETE,
            section=UI.GROUP_MEMORY,
            file=FILE_MEMORY,
            old=line,
            new="",
        )
        return item, None

    def stage_memory_add(self, text: str) -> tuple[Item | None, str | None]:
        try:
            clean = sanitize_entry(text)
        except Exception:
            return None, UI.MEMORY_EMPTY_TEXT
        line = f"- {today_local()}: {clean}"
        item = self._upsert(
            area=AREA_MEMORY,
            kind=KIND_MEMORY_ADD,
            key=f"memory_add:{uuid.uuid4().hex[:10]}",
            label=UI.MEMORY_DIFF_ADD,
            section=UI.GROUP_MEMORY,
            file=FILE_MEMORY,
            old="",
            new=line,
        )
        return item, None

    def stage_text(self, area: str, text: str) -> tuple[Item | None, str | None]:
        file_name = FILE_PERSONA if area == AREA_PERSONA else FILE_PROFILE
        current = self.read_text(file_name)
        text = text.replace("\r\n", "\n")
        if area == AREA_PERSONA and not text.strip():
            return None, UI.PERSONA_EMPTY
        item = self._upsert(
            area=area,
            kind=KIND_TEXT,
            key=f"text:{area}",
            label=AREA_LABELS[area],
            section=AREA_LABELS[area],
            file=file_name,
            old=current,
            new=text,
        )
        return item, None

    def stage_restore(self, backup_id: str) -> tuple[list[Item], str | None]:
        backup = self.backup(backup_id)
        if backup is None:
            return [], UI.HISTORY_MISSING
        staged: list[Item] = []
        for name in backup["files"]:
            source = self.paths.backups_dir / backup_id / name
            if not source.is_file():
                continue
            text = source.read_text(encoding="utf-8")
            if name == FILE_PROFILE:
                text = profile_user_text(text)
            current = self.read_text(name)
            if text == current:
                continue
            area = {FILE_PERSONA: AREA_PERSONA, FILE_PROFILE: AREA_PROFILE, FILE_MEMORY: AREA_MEMORY}.get(name, AREA_SETTINGS)
            item = self._upsert(
                area=area,
                kind=KIND_RESTORE,
                key=f"restore:{name}",
                label=UI.RESTORE_LABEL.format(file=name),
                section=backup["label"],
                file=name,
                old=current,
                new=text,
            )
            if item is not None:
                staged.append(item)
        return staged, None

    # --- validation and conflicts ---

    def check(self, items: list[Item] | None = None) -> tuple[list[Item], list[str]]:
        """Mark conflicts and validation errors on the items (transient fields); return them and general errors."""
        items = self.items() if items is None else items
        general: list[str] = []
        config = self.current_config()
        current_memory = self.memory_lines()
        for item in items:
            item.error, item.conflict = None, False
            if item.kind == KIND_SETTING:
                spec = settings_mod.field_spec(config, item.key.split(":", 1)[1])
                if spec is None or spec.value != item.old:
                    item.conflict = True
            elif item.kind in (KIND_MEMORY_EDIT, KIND_MEMORY_DELETE):
                if item.old not in current_memory:
                    item.conflict = True
            elif item.kind in (KIND_TEXT, KIND_RESTORE):
                if self.read_text(item.file) != item.old:
                    item.conflict = True
            if item.conflict:
                item.error = UI.CONFLICT_TEXT
        for item in items:
            if item.kind == KIND_MEMORY_ADD and not str(item.new).strip():
                item.error = UI.MEMORY_EMPTY_TEXT
            if item.kind == KIND_TEXT and item.area == AREA_PERSONA and not str(item.new).strip():
                item.error = UI.PERSONA_EMPTY
        config_items = [i for i in items if i.file in (FILE_CONFIG, FILE_LOCAL) and not i.conflict]
        if config_items:
            try:
                texts = self.render_config_texts(items)
                data = _merge_texts(texts, self.paths)
                Config.model_validate(data)
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(p) for p in err["loc"])
                    target = next((i for i in config_items if i.kind == KIND_SETTING and i.key.split(":", 1)[1] == loc), None)
                    if target is None:
                        target = next(
                            (
                                i
                                for i in config_items
                                if i.kind == KIND_SETTING and loc.startswith(i.key.split(":", 1)[1].rsplit(".", 1)[0])
                            ),
                            None,
                        )
                    message = f"{loc}: {err['msg']}" if loc else err["msg"]
                    if target is not None and not target.error:
                        target.error = message
                    else:
                        general.append(message)
            except Exception as exc:  # unparsable TOML after a restore, for example
                general.append(f"{type(exc).__name__}: {exc}")
        return items, general

    def render_config_texts(self, items: list[Item]) -> dict[str, str]:
        """New text of config.toml and config.local.toml after the staged settings and restores."""
        out: dict[str, str] = {}
        for name in (FILE_CONFIG, FILE_LOCAL):
            path = self.file_path(name)
            restore = next((i for i in items if i.kind == KIND_RESTORE and i.file == name), None)
            text = str(restore.new) if restore is not None else (path.read_text(encoding="utf-8") if path.is_file() else "")
            changes = {i.key.split(":", 1)[1]: i.new for i in items if i.kind == KIND_SETTING and i.file == name}
            if changes:
                text = tomlwrite.render_with(text, changes)
            if restore is not None or changes:
                out[name] = text
        return out

    def render_texts(self, items: list[Item]) -> dict[str, str]:
        """Every file the items touch -> its new content (profile keeps the current auto block)."""
        out = self.render_config_texts(items)
        memory_items = [i for i in items if i.file == FILE_MEMORY]
        if memory_items:
            restore = next((i for i in memory_items if i.kind == KIND_RESTORE), None)
            lines = str(restore.new).splitlines() if restore is not None else self.memory_lines()
            for item in memory_items:
                if item.kind == KIND_MEMORY_DELETE and item.old in lines:
                    lines.remove(item.old)
                elif item.kind == KIND_MEMORY_EDIT and item.old in lines:
                    lines[lines.index(item.old)] = str(item.new)
            for item in memory_items:
                if item.kind == KIND_MEMORY_ADD:
                    lines.append(str(item.new))
            out[FILE_MEMORY] = "\n".join(line for line in lines if line.strip()) + "\n"
        for name in (FILE_PERSONA, FILE_PROFILE):
            item = next((i for i in items if i.file == name and i.kind in (KIND_TEXT, KIND_RESTORE)), None)
            if item is None:
                continue
            text = str(item.new)
            if name == FILE_PROFILE:
                path = self.file_path(name)
                current = path.read_text(encoding="utf-8") if path.is_file() else ""
                body = auto_block_body(current)
                if body is not None:
                    text = replace_auto_block(text, AUTO_BLOCK_KEY, body)
            out[name] = text if text.endswith("\n") or not text else text + "\n"
        return out

    def services_to_restart(self, items: list[Item]) -> list[str]:
        wanted: list[str] = []
        for item in items:
            for service in item.restarts:
                if service not in wanted:
                    wanted.append(service)
        return wanted

    # --- backups / history ---

    def backups(self, limit: int = 20) -> list[dict[str, Any]]:
        root = self.paths.backups_dir
        if not root.is_dir():
            return []
        out = []
        for folder in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:limit]:
            info = self.backup(folder.name)
            if info is not None:
                out.append(info)
        return out

    def backup(self, backup_id: str) -> dict[str, Any] | None:
        if not backup_id or any(ch not in "0123456789-" for ch in backup_id):
            return None
        folder = self.paths.backups_dir / backup_id
        manifest = folder / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            return None
        files = [f for f in data.get("files", []) if (folder / f).is_file()]
        return {
            "id": backup_id,
            "at": data.get("at", ""),
            "items": data.get("items", []),
            "files": files,
            "label": UI.SAVED_ON.format(time=_local(data.get("at", ""))),
        }

    def _create_backup(self, files: list[str], items: list[Item]) -> Path:
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        folder = self.paths.backups_dir / stamp
        n = 1
        while folder.exists():
            folder = self.paths.backups_dir / f"{stamp}-{n}"
            n += 1
        folder.mkdir(parents=True)
        copied = []
        for name in files:
            source = self.file_path(name)
            if source.is_file():
                shutil.copy2(source, folder / name)
                copied.append(name)
        manifest = {
            "at": now_iso(),
            "files": copied,
            "items": [
                {
                    "area": i.area,
                    "label": i.label,
                    "section": i.section,
                    "file": i.file,
                    "old": _short(i.old_text),
                    "new": _short(i.new_text),
                }
                for i in items
            ],
        }
        (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self._prune_backups()
        return folder

    def _prune_backups(self) -> None:
        root = self.paths.backups_dir
        keep = int(self.config.dashboard.backups_keep)
        folders = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
        for old in folders[keep:]:
            shutil.rmtree(old, ignore_errors=True)

    # --- applying ---

    def start_apply(self, *, probes: "Probes | None" = None) -> "ApplyJob":
        with self._lock:
            if self.job is not None and self.job.running:
                return self.job
            self.job = ApplyJob(self, probes=probes or Probes.for_paths(self.paths))
            self.job.start()
            return self.job


def _rebuild_line(line: str, text: str) -> str:
    """Keep the date prefix of a `- YYYY-MM-DD: fact` line, replace the fact."""
    import re  # noqa: PLC0415

    m = re.match(r"^(- \d{4}-\d{2}-\d{2}: )", line)
    return (m.group(1) if m else "") + text


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _merge_texts(texts: dict[str, str], paths: "Paths") -> dict:
    base_text = texts.get(FILE_CONFIG)
    base = tomllib.loads(base_text) if base_text is not None else _read_toml(paths.config_file)
    local_text = texts.get(FILE_LOCAL)
    local = tomllib.loads(local_text) if local_text is not None else _read_toml(paths.local_config_file)
    return merge_config_data(base, local)


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


# --- the apply job --------------------------------------------------------------------------------------------------


@dataclass
class Probes:
    """What "idle" means for a graceful restart; tests replace the callables."""

    turn_in_progress: Callable[[str], bool]
    alarm_ringing: Callable[[], bool]
    service_reports: Callable[[], list] = services.service_reports
    restart: Callable[[str], Any] = lambda name: None
    health: Callable[[], list] = lambda: []
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def for_paths(cls, paths: "Paths") -> "Probes":
        def turn_in_progress(channel: str) -> bool:
            try:
                from jarvis.dashboard.queries import connect_readonly  # noqa: PLC0415

                conn = connect_readonly(paths.db_file)
                if conn is None:
                    return False
                try:
                    since = datetime.now().astimezone().timestamp() - TURN_IN_PROGRESS_WINDOW_MIN * 60
                    since_iso = datetime.utcfromtimestamp(since).strftime("%Y-%m-%dT%H:%M:%S")
                    row = conn.execute(
                        "SELECT 1 FROM turns t JOIN sessions s ON s.id = t.session_id WHERE s.channel = ? AND t.total_ms IS NULL"
                        " AND t.error IS NULL AND t.started_at >= ? LIMIT 1",
                        (channel, since_iso),
                    ).fetchone()
                    return row is not None
                finally:
                    conn.close()
            except Exception:
                return False

        return cls(
            turn_in_progress=turn_in_progress,
            alarm_ringing=lambda: paths.alarm_ring_file.is_file(),
            restart=lambda name: services.restart_agent(name, paths),
            health=lambda: health.run_checks(load_config(paths.config_file), paths, full=False),
        )


@dataclass
class Step:
    key: str
    label: str
    state: str = "pending"  # pending | running | done | failed | skipped
    message: str = ""


class ApplyJob:
    def __init__(self, cart: Cart, *, probes: Probes) -> None:
        self.cart = cart
        self.probes = probes
        self.steps = [Step(key, label) for key, label in STEPS]
        self.items: list[Item] = []
        self.general_errors: list[str] = []
        self.finished = False
        self.success = False
        self.failed_step: str | None = None
        self.result_text = ""
        self.waiting_for: str | None = None  # the service we wait on past the idle timeout
        self.restarted: list[str] = []
        self.backup_dir: Path | None = None
        self._decision = threading.Event()
        self._force = False
        self._thread: threading.Thread | None = None
        self._written: list[str] = []

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dashboard-apply", daemon=True)
        self._thread.start()

    def step(self, key: str) -> Step:
        return next(s for s in self.steps if s.key == key)

    def force_restart(self) -> None:
        self._force = True
        self._decision.set()

    def keep_waiting(self) -> None:
        self._force = False
        self._decision.set()

    def _fail(self, key: str, message: str) -> None:
        step = self.step(key)
        step.state, step.message = "failed", message
        for later in self.steps:
            if later.state == "pending":
                later.state = "skipped"
        self.failed_step = step.label
        self.finished, self.success = True, False
        self.result_text = UI.SAVE_FAILED.format(step=step.label) if self._written else f"{step.label}: {message}"

    def _run(self) -> None:
        try:
            self._apply()
        except Exception as exc:  # the UI must always get a final state
            log.exception("dashboard save crashed")
            running = next((s for s in self.steps if s.state == "running"), self.steps[0])
            self._rollback()
            self._fail(running.key, f"{type(exc).__name__}: {exc}")

    def _apply(self) -> None:
        cart = self.cart
        step = self.step("conflicts")
        step.state = "running"
        self.items, self.general_errors = cart.check()
        if not self.items:
            self._fail("conflicts", UI.NO_CHANGES)
            return
        conflicts = [i for i in self.items if i.conflict]
        if conflicts:
            self._fail("conflicts", UI.CONFLICT_TEXT)
            return
        step.state = "done"

        step = self.step("validate")
        step.state = "running"
        if any(i.error for i in self.items) or self.general_errors:
            self._fail("validate", UI.FIX_HIGHLIGHTED)
            return
        texts = cart.render_texts(self.items)
        if FILE_CONFIG in texts or FILE_LOCAL in texts:
            try:
                Config.model_validate(_merge_texts(texts, cart.paths))
            except (ValidationError, tomllib.TOMLDecodeError) as exc:
                self._fail("validate", str(exc).splitlines()[0])
                return
        step.state = "done"

        step = self.step("backup")
        step.state = "running"
        self.backup_dir = cart._create_backup(list(texts), self.items)
        step.state, step.message = "done", self.backup_dir.name

        step = self.step("write")
        step.state = "running"
        try:
            self._write_all(texts)
        except Exception as exc:
            self._rollback()
            self._fail("write", f"{type(exc).__name__}: {exc}")
            return
        step.state, step.message = "done", ", ".join(texts)

        step = self.step("restart")
        step.state = "running"
        wanted = cart.services_to_restart(self.items)
        running_services = {r.name for r in self.probes.service_reports() if r.installed and r.running}
        targets = [s for s in wanted if s in running_services]
        if not targets:
            step.state, step.message = "done", UI.NO_SERVICE_RESTART if not wanted else "not running: " + ", ".join(wanted)
        else:
            try:
                for service in targets:
                    self._wait_idle(service)
                    self.probes.restart(service)
                    self.restarted.append(service)
            except Exception as exc:
                self._rollback()
                self._restart_after_rollback()
                self._fail("restart", f"{type(exc).__name__}: {exc}")
                return
            step.state, step.message = "done", ", ".join(targets)

        step = self.step("health")
        step.state = "running"
        problem = self._health_check()
        if problem:
            self._rollback()
            self._restart_after_rollback()
            self._fail("health", problem)
            return
        step.state = "done"

        cart.clear()
        self.step("done").state = "done"
        self.finished, self.success, self.result_text = True, True, UI.SAVE_OK

    def _write_all(self, texts: dict[str, str]) -> None:
        """Temp files first, then one os.replace per file; a failure half-way is undone by the caller's rollback."""
        temps: list[tuple[Path, Path]] = []
        for name, text in texts.items():
            target = self.cart.file_path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".dashboard.tmp")
            tmp.write_text(text, encoding="utf-8")
            temps.append((tmp, target))
        for tmp, target in temps:
            os.replace(tmp, target)
            self._written.append(target.name)

    def _rollback(self) -> None:
        if self.backup_dir is None or not self._written:
            return
        for name in list(self._written):
            source = self.backup_dir / name
            target = self.cart.file_path(name)
            try:
                if source.is_file():
                    shutil.copy2(source, target)
                else:
                    target.unlink(missing_ok=True)  # the file did not exist before the save
            except OSError:
                log.exception("rollback of %s failed", name)
        log.warning("dashboard save rolled back from %s", self.backup_dir)

    def _restart_after_rollback(self) -> None:
        for service in self.restarted:
            try:
                self.probes.restart(service)
            except Exception:
                log.warning("restart of %s after the rollback failed", service, exc_info=True)

    def _wait_idle(self, service: str) -> None:
        """Up to IDLE_WAIT_S for the service to be between turns (and no alarm ringing); then wait for the user."""
        channel = service
        deadline = self.probes.clock() + IDLE_WAIT_S
        while True:
            busy = self.probes.alarm_ringing() or self.probes.turn_in_progress(channel)
            if not busy:
                self.waiting_for = None
                return
            if self.probes.clock() >= deadline:
                self.waiting_for = service
                self.step("restart").message = UI.RESTART_WAIT.format(service=UI.SERVICE_LABEL.format(service=service))
                self._decision.clear()
                self._decision.wait()
                self.waiting_for = None
                if self._force:
                    return
                deadline = self.probes.clock() + IDLE_WAIT_S
                continue
            self.probes.sleep(IDLE_POLL_S)

    def _health_check(self) -> str | None:
        if self.restarted:
            deadline = self.probes.clock() + HEALTH_WAIT_S
            missing = list(self.restarted)
            while missing and self.probes.clock() < deadline:
                reports = {r.name: r for r in self.probes.service_reports()}
                missing = [s for s in missing if not (s in reports and reports[s].running)]
                if missing:
                    self.probes.sleep(IDLE_POLL_S)
            if missing:
                return "not running after the restart: " + ", ".join(missing)
        try:
            checks = self.probes.health()
        except Exception as exc:
            return f"health check failed: {exc}"
        fatal = [c for c in checks if not c.ok and getattr(c, "fatal", False)]
        if fatal:
            return "; ".join(f"{c.name}: {c.detail}" for c in fatal)
        return None
