"""SQLite connection, schema and migrations (tracked with PRAGMA user_version).

All timestamps are stored as integer Unix epoch seconds (UTC).
"""
from __future__ import annotations

import sqlite3

# Each entry migrates the schema from version i to i+1. Append only; never edit old entries.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE groups (
        chat_id        INTEGER PRIMARY KEY,
        title          TEXT,
        deadline_hours INTEGER NOT NULL DEFAULT 24,
        max_attempts   INTEGER NOT NULL DEFAULT 5,
        lock_minutes   INTEGER NOT NULL DEFAULT 60,
        contact_text   TEXT,
        report_chat_id INTEGER
    );
    CREATE TABLE students (
        chat_id        INTEGER NOT NULL,
        code           TEXT NOT NULL,
        full_name      TEXT,
        full_name_norm TEXT,
        PRIMARY KEY (chat_id, code)
    );
    CREATE TABLE bindings (
        chat_id   INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        code      TEXT NOT NULL,
        username  TEXT,
        full_name TEXT,
        bound_at  INTEGER NOT NULL,
        PRIMARY KEY (chat_id, user_id),
        UNIQUE (chat_id, code),
        FOREIGN KEY (chat_id, code) REFERENCES students (chat_id, code) ON DELETE CASCADE
    );
    CREATE INDEX idx_bindings_username ON bindings (chat_id, username COLLATE NOCASE);
    CREATE TABLE members (
        chat_id     INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('pending', 'verified', 'revoked')),
        first_seen  INTEGER NOT NULL,
        deadline_at INTEGER,
        PRIMARY KEY (chat_id, user_id)
    );
    CREATE INDEX idx_members_deadline ON members (status, deadline_at);
    CREATE TABLE attempts (
        chat_id      INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        fails        INTEGER NOT NULL DEFAULT 0,
        locked_until INTEGER,
        PRIMARY KEY (chat_id, user_id)
    );
    CREATE TABLE bot_admins (
        user_id  INTEGER PRIMARY KEY,
        added_by INTEGER,
        added_at INTEGER NOT NULL
    );
    CREATE TABLE admin_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id     INTEGER NOT NULL,
        chat_id      INTEGER,
        action       TEXT NOT NULL,
        details_json TEXT,
        at           INTEGER NOT NULL
    );
    CREATE TABLE cleanup (
        chat_id    INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        delete_at  INTEGER NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    );
    CREATE INDEX idx_cleanup_due ON cleanup (delete_at);
    """,
    # v2: a kicked member whose unban failed (still banned!); the sweeper retries only the unban.
    """
    ALTER TABLE members ADD COLUMN unban_pending INTEGER NOT NULL DEFAULT 0;
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"Database schema v{version} is newer than this code (v{SCHEMA_VERSION}).")
    for i in range(version, SCHEMA_VERSION):
        # executescript commits implicitly; user_version is set inside the same script.
        try:
            conn.executescript(f"BEGIN;\n{MIGRATIONS[i]}\nPRAGMA user_version = {i + 1};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise
    return SCHEMA_VERSION


def connect(path: str) -> sqlite3.Connection:
    """Open the database with WAL + foreign keys and bring the schema up to date."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL, far fewer fsyncs
    conn.execute("PRAGMA busy_timeout = 10000")
    migrate(conn)
    return conn
