from bot.db import SCHEMA_VERSION, connect, migrate


def test_schema_and_version(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"groups", "students", "bindings", "members", "attempts",
            "bot_admins", "admin_log", "cleanup"} <= tables
    assert migrate(conn) == SCHEMA_VERSION  # idempotent
    conn.close()


def test_reopen_keeps_data(tmp_path):
    path = str(tmp_path / "t.db")
    conn = connect(path)
    conn.execute("INSERT INTO groups (chat_id) VALUES (1)")
    conn.commit()
    conn.close()
    conn = connect(path)
    assert conn.execute("SELECT deadline_hours FROM groups").fetchone()[0] == 24
    conn.close()


def test_synchronous_normal_and_unban_column(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    cols = {r[1] for r in conn.execute("PRAGMA table_info(members)")}
    assert "unban_pending" in cols
    conn.close()


def test_migrate_v1_database_keeps_members(tmp_path):
    import sqlite3

    from bot.db import MIGRATIONS
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    old.execute("INSERT INTO members VALUES (1, 2, 'pending', 0, 100)")
    old.commit()
    old.close()
    conn = connect(path)
    row = conn.execute("SELECT * FROM members").fetchone()
    assert (row["user_id"], row["unban_pending"]) == (2, 0)
    conn.close()
