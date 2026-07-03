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

def _init_db_sync() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id      TEXT NOT NULL,
                session_id    TEXT,
                sender        TEXT NOT NULL,
                text          TEXT NOT NULL,
                mqtt_command  TEXT,
                ts            TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS world_scenes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id       TEXT NOT NULL,
                html           TEXT NOT NULL,
                trigger_prompt TEXT,
                ts             TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS world_messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id  TEXT NOT NULL,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                scene_id  INTEGER REFERENCES world_scenes(id),
                ts        TEXT NOT NULL
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
        """)


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


# ── Chat messages ─────────────────────────────────────────────────────────────

def _log_chat_sync(board_id, session_id, sender, text, mqtt_command) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (board_id, session_id, sender, text, mqtt_command, ts) VALUES (?,?,?,?,?,?)",
            (board_id, session_id, sender, text, mqtt_command, _now()),
        )


async def log_chat_message(
    board_id: str,
    session_id: str | None,
    sender: str,
    text: str,
    mqtt_command: str | None = None,
) -> None:
    await asyncio.to_thread(_log_chat_sync, board_id, session_id, sender, text, mqtt_command)


# ── World scenes ──────────────────────────────────────────────────────────────

def _log_world_scene_sync(board_id, html, trigger_prompt) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO world_scenes (board_id, html, trigger_prompt, ts) VALUES (?,?,?,?)",
            (board_id, html, trigger_prompt, _now()),
        )
        return cur.lastrowid


async def log_world_scene(board_id: str, html: str, trigger_prompt: str | None = None) -> int:
    return await asyncio.to_thread(_log_world_scene_sync, board_id, html, trigger_prompt)


# ── World messages ────────────────────────────────────────────────────────────

def _log_world_message_sync(board_id, role, content, scene_id) -> None:
    if not isinstance(content, str):
        content = json.dumps(content)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO world_messages (board_id, role, content, scene_id, ts) VALUES (?,?,?,?,?)",
            (board_id, role, content, scene_id, _now()),
        )


async def log_world_message(
    board_id: str,
    role: str,
    content,
    scene_id: int | None = None,
) -> None:
    await asyncio.to_thread(_log_world_message_sync, board_id, role, content, scene_id)


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


# ── Query helpers ─────────────────────────────────────────────────────────────

def _get_chat_logs_sync(board_id, limit) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        q = """
            SELECT cm.*, s.user_email
            FROM chat_messages cm
            LEFT JOIN sessions s ON s.session_id = cm.session_id
            {where}
            ORDER BY cm.ts DESC LIMIT ?
        """
        if board_id:
            rows = conn.execute(
                q.format(where="WHERE cm.board_id=?"), (board_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(q.format(where=""), (limit,)).fetchall()
        return [dict(r) for r in rows]


async def get_chat_logs(board_id: str | None = None, limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_get_chat_logs_sync, board_id, limit)


def _get_world_logs_sync(board_id, limit) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        q = """
            SELECT wm.id, wm.board_id, wm.role, wm.content, wm.scene_id, wm.ts,
                   ws.trigger_prompt,
                   (SELECT user_email FROM sessions
                    WHERE board_id = wm.board_id
                    ORDER BY started_at DESC LIMIT 1) AS user_email
            FROM world_messages wm
            LEFT JOIN world_scenes ws ON wm.scene_id = ws.id
            {where}
            ORDER BY wm.ts DESC LIMIT ?
        """
        if board_id:
            rows = conn.execute(
                q.format(where="WHERE wm.board_id=?"), (board_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(q.format(where=""), (limit,)).fetchall()
        return [dict(r) for r in rows]


async def get_world_logs(board_id: str | None = None, limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_get_world_logs_sync, board_id, limit)
