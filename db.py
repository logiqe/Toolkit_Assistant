from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone


def _db_path() -> str:
    return os.path.join(os.environ.get("DATA_DIR", "."), "toolkit.db")


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(_db_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Schema ────────────────────────────────────────────────────────────────────

def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column in [
        ("chat_messages", "user_email"),
        ("world_messages", "session_id"),
        ("world_messages", "user_email"),
        ("world_scenes", "session_id"),
        ("world_scenes", "user_email"),
    ]:
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def _backfill_emails(conn: sqlite3.Connection) -> None:
    """Attribute emails to rows written before user_email was stored per-row.
    Only touches rows where user_email IS NULL, so it is idempotent."""
    # Chat: hard link via session_id
    conn.execute("""
        UPDATE chat_messages SET user_email = (
            SELECT s.user_email FROM sessions s
            WHERE s.session_id = chat_messages.session_id
            ORDER BY s.started_at DESC LIMIT 1
        )
        WHERE user_email IS NULL AND session_id IS NOT NULL
    """)
    # World tables: most recent session on the board before the message.
    # 'none' (frontend fallback) and 'unknown' (legacy login fallback) are equivalent.
    for table in ("world_messages", "world_scenes"):
        conn.execute(f"""
            UPDATE {table} SET user_email = (
                SELECT s.user_email FROM sessions s
                WHERE (s.board_id = {table}.board_id
                       OR ({table}.board_id IN ('none','unknown')
                           AND s.board_id IN ('none','unknown')))
                  AND s.started_at <= {table}.ts
                ORDER BY s.started_at DESC LIMIT 1
            )
            WHERE user_email IS NULL
        """)


def _init_db_sync() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id      TEXT NOT NULL,
                session_id    TEXT,
                user_email    TEXT,
                sender        TEXT NOT NULL,
                text          TEXT NOT NULL,
                mqtt_command  TEXT,
                ts            TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS world_scenes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id       TEXT NOT NULL,
                session_id     TEXT,
                user_email     TEXT,
                html           TEXT NOT NULL,
                trigger_prompt TEXT,
                ts             TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS world_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id    TEXT NOT NULL,
                session_id  TEXT,
                user_email  TEXT,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                scene_id    INTEGER REFERENCES world_scenes(id),
                ts          TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id    TEXT,
                user_email  TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                ended_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chat_board        ON chat_messages(board_id);
            CREATE INDEX IF NOT EXISTS idx_world_msg_board   ON world_messages(board_id);
            CREATE INDEX IF NOT EXISTS idx_world_scene_board ON world_scenes(board_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_board    ON sessions(board_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_sid      ON sessions(session_id);
        """)
        _migrate(conn)
        _backfill_emails(conn)


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


# ── Chat messages ─────────────────────────────────────────────────────────────

def _log_chat_sync(board_id, session_id, sender, text, mqtt_command, user_email) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (board_id, session_id, user_email, sender, text, mqtt_command, ts) VALUES (?,?,?,?,?,?,?)",
            (board_id, session_id, user_email, sender, text, mqtt_command, _now()),
        )


async def log_chat_message(
    board_id: str,
    session_id: str | None,
    sender: str,
    text: str,
    mqtt_command: str | None = None,
    user_email: str | None = None,
) -> None:
    await asyncio.to_thread(_log_chat_sync, board_id, session_id, sender, text, mqtt_command, user_email)


# ── World scenes ──────────────────────────────────────────────────────────────

def _log_world_scene_sync(board_id, html, trigger_prompt, session_id, user_email) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO world_scenes (board_id, session_id, user_email, html, trigger_prompt, ts) VALUES (?,?,?,?,?,?)",
            (board_id, session_id, user_email, html, trigger_prompt, _now()),
        )
        return cur.lastrowid


async def log_world_scene(
    board_id: str,
    html: str,
    trigger_prompt: str | None = None,
    session_id: str | None = None,
    user_email: str | None = None,
) -> int:
    return await asyncio.to_thread(_log_world_scene_sync, board_id, html, trigger_prompt, session_id, user_email)


# ── World messages ────────────────────────────────────────────────────────────

def _log_world_message_sync(board_id, role, content, scene_id, session_id, user_email) -> None:
    if not isinstance(content, str):
        content = json.dumps(content)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO world_messages (board_id, session_id, user_email, role, content, scene_id, ts) VALUES (?,?,?,?,?,?,?)",
            (board_id, session_id, user_email, role, content, scene_id, _now()),
        )


async def log_world_message(
    board_id: str,
    role: str,
    content,
    scene_id: int | None = None,
    session_id: str | None = None,
    user_email: str | None = None,
) -> None:
    await asyncio.to_thread(_log_world_message_sync, board_id, role, content, scene_id, session_id, user_email)


# ── Sessions ──────────────────────────────────────────────────────────────────

def _log_session_start_sync(board_id, user_email, session_id) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (board_id, user_email, session_id, started_at) VALUES (?,?,?,?)",
            (board_id, user_email, session_id, _now()),
        )


async def log_session_start(board_id: str | None, user_email: str, session_id: str) -> None:
    await asyncio.to_thread(_log_session_start_sync, board_id, user_email, session_id)


def _log_session_end_sync(board_id) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE sessions SET ended_at = ?
               WHERE id = (
                   SELECT id FROM sessions
                   WHERE board_id = ? AND ended_at IS NULL
                   ORDER BY started_at DESC LIMIT 1
               )""",
            (_now(), board_id),
        )


async def log_session_end(board_id: str) -> None:
    await asyncio.to_thread(_log_session_end_sync, board_id)


def _end_session_by_id_sync(session_id) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
            (_now(), session_id),
        )


async def end_session_by_id(session_id: str | None) -> None:
    if not session_id:
        return
    await asyncio.to_thread(_end_session_by_id_sync, session_id)


# ── Query helpers ─────────────────────────────────────────────────────────────

def _get_chat_logs_sync(board_id, limit) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        if board_id:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE board_id=? ORDER BY ts DESC LIMIT ?",
                (board_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chat_messages ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


async def get_chat_logs(board_id: str | None = None, limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_get_chat_logs_sync, board_id, limit)


def _get_world_logs_sync(board_id, limit, include_html) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        html_col = ", ws.html AS scene_html" if include_html else ""
        q = f"""
            SELECT wm.id, wm.board_id, wm.session_id, wm.user_email,
                   wm.role, wm.content, wm.scene_id, wm.ts,
                   ws.trigger_prompt{html_col}
            FROM world_messages wm
            LEFT JOIN world_scenes ws ON wm.scene_id = ws.id
            {{where}}
            ORDER BY wm.ts DESC LIMIT ?
        """
        if board_id:
            rows = conn.execute(
                q.format(where="WHERE wm.board_id=?"), (board_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(q.format(where=""), (limit,)).fetchall()
        return [dict(r) for r in rows]


async def get_world_logs(
    board_id: str | None = None,
    limit: int = 50,
    include_html: bool = False,
) -> list[dict]:
    return await asyncio.to_thread(_get_world_logs_sync, board_id, limit, include_html)


def _get_scene_html_sync(scene_id) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT html FROM world_scenes WHERE id=?", (scene_id,)
        ).fetchone()
        return row[0] if row else None


async def get_scene_html(scene_id: int) -> str | None:
    return await asyncio.to_thread(_get_scene_html_sync, scene_id)


def _get_email_for_session_sync(session_id) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_email FROM sessions WHERE session_id=? ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row[0] if row else None


async def get_email_for_session(session_id: str | None) -> str | None:
    """Resolve the email tied to a login session_id (survives server restarts)."""
    if not session_id:
        return None
    return await asyncio.to_thread(_get_email_for_session_sync, session_id)


# ── Backup ────────────────────────────────────────────────────────────────────

def _backup_db_sync(dest_path: str) -> None:
    src = _connect()
    dst = sqlite3.connect(dest_path)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()


async def backup_db(dest_path: str) -> None:
    await asyncio.to_thread(_backup_db_sync, dest_path)
