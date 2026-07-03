"""Database layer - SQLite with aiosqlite for async access."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite

DB_PATH = Path("data/oscam_monitor.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ecm_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    server TEXT NOT NULL,
    username TEXT NOT NULL,
    caid TEXT NOT NULL,
    sid TEXT NOT NULL,
    channel_name TEXT,
    reader TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watch_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server TEXT NOT NULL,
    username TEXT NOT NULL,
    channel_name TEXT,
    caid TEXT NOT NULL,
    sid TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    duration_seconds INTEGER,
    country_tag TEXT
);

CREATE TABLE IF NOT EXISTS channel_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caid TEXT NOT NULL,
    sid TEXT NOT NULL,
    channel_name TEXT,
    country_tag TEXT DEFAULT 'unknown',
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now')),
    UNIQUE(caid, sid)
);

CREATE TABLE IF NOT EXISTS card_serials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server TEXT NOT NULL,
    reader TEXT NOT NULL,
    serial TEXT NOT NULL,
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now')),
    UNIQUE(server, reader, serial)
);

CREATE TABLE IF NOT EXISTS discovered_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server TEXT NOT NULL,
    username TEXT NOT NULL,
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now')),
    UNIQUE(server, username)
);

CREATE INDEX IF NOT EXISTS idx_ecm_timestamp ON ecm_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_ecm_user ON ecm_events(username);
CREATE INDEX IF NOT EXISTS idx_ecm_sid ON ecm_events(caid, sid);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON watch_sessions(username);
CREATE INDEX IF NOT EXISTS idx_sessions_time ON watch_sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_discovered_users_server ON discovered_users(server);

CREATE TABLE IF NOT EXISTS file_backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_backups_name ON file_backups(filename, created_at);
"""


def init_db_sync(path: Path | None = None) -> None:
    """Initialize database synchronously (for startup)."""
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.close()


async def get_db(path: Path | None = None) -> aiosqlite.Connection:
    """Get async database connection."""
    db_path = path or DB_PATH
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    return db


async def insert_ecm_event(
    db: aiosqlite.Connection,
    *,
    timestamp: str,
    server: str,
    username: str,
    caid: str,
    sid: str,
    channel_name: str | None = None,
    reader: str | None = None,
    success: bool = True,
) -> int:
    """Insert an ECM event and return row ID."""
    cursor = await db.execute(
        """INSERT INTO ecm_events (timestamp, server, username, caid, sid, channel_name, reader, success)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (timestamp, server, username, caid, sid, channel_name, reader, int(success)),
    )
    await db.commit()
    return cursor.lastrowid


async def upsert_channel_mapping(
    db: aiosqlite.Connection,
    *,
    caid: str,
    sid: str,
    channel_name: str | None = None,
    country_tag: str = "unknown",
) -> None:
    """Insert or update a channel mapping."""
    await db.execute(
        """INSERT INTO channel_mappings (caid, sid, channel_name, country_tag)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(caid, sid) DO UPDATE SET
               channel_name = COALESCE(excluded.channel_name, channel_mappings.channel_name),
               country_tag = CASE
                   WHEN excluded.country_tag != 'unknown' THEN excluded.country_tag
                   ELSE channel_mappings.country_tag
               END,
               last_seen = datetime('now')""",
        (caid, sid, channel_name, country_tag),
    )
    await db.commit()


async def upsert_card_serial(
    db: aiosqlite.Connection,
    *,
    server: str,
    reader: str,
    serial: str,
) -> bool:
    """Insert or update card serial. Returns True if this is a NEW serial for that reader."""
    cursor = await db.execute(
        "SELECT serial FROM card_serials WHERE server = ? AND reader = ?",
        (server, reader),
    )
    existing = await cursor.fetchone()

    if existing and existing[0] != serial:
        # Serial changed!
        await db.execute(
            """INSERT INTO card_serials (server, reader, serial)
               VALUES (?, ?, ?)
               ON CONFLICT(server, reader, serial) DO UPDATE SET last_seen = datetime('now')""",
            (server, reader, serial),
        )
        await db.commit()
        return True  # Changed

    await db.execute(
        """INSERT INTO card_serials (server, reader, serial)
           VALUES (?, ?, ?)
           ON CONFLICT(server, reader, serial) DO UPDATE SET last_seen = datetime('now')""",
        (server, reader, serial),
    )
    await db.commit()
    return False


async def upsert_discovered_user(
    db: aiosqlite.Connection,
    *,
    server: str,
    username: str,
) -> None:
    """Track a discovered user from log parsing."""
    await db.execute(
        """INSERT INTO discovered_users (server, username)
           VALUES (?, ?)
           ON CONFLICT(server, username) DO UPDATE SET last_seen = datetime('now')""",
        (server, username),
    )
    await db.commit()


async def get_discovered_users(db: aiosqlite.Connection, server: str | None = None) -> list[dict]:
    """Get all discovered users, optionally filtered by server."""
    if server:
        cursor = await db.execute(
            "SELECT server, username, first_seen, last_seen FROM discovered_users WHERE server = ? ORDER BY username",
            (server,),
        )
    else:
        cursor = await db.execute(
            "SELECT server, username, first_seen, last_seen FROM discovered_users ORDER BY server, username"
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_channel_mappings(db: aiosqlite.Connection) -> list[dict]:
    """Get all channel mappings."""
    cursor = await db.execute(
        "SELECT caid, sid, channel_name, country_tag, first_seen, last_seen FROM channel_mappings ORDER BY last_seen DESC"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def cleanup_old_events(db: aiosqlite.Connection, retention_days: int) -> int:
    """Delete ECM events older than retention period. Returns count deleted."""
    cursor = await db.execute(
        "DELETE FROM ecm_events WHERE timestamp < datetime('now', ?)",
        (f"-{retention_days} days",),
    )
    await db.commit()
    return cursor.rowcount
