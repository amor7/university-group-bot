"""All database queries. Times are integer Unix epoch seconds; `now` is always passed in."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from bot.normalize import normalize_code, normalize_name

Row = sqlite3.Row

GROUP_SETTINGS = frozenset(
    {"title", "deadline_hours", "max_attempts", "lock_minutes", "contact_text", "report_chat_id"}
)
STATUSES = frozenset({"pending", "verified", "revoked"})


class CodeAlreadyBound(Exception):
    def __init__(self, code: str, user_id: int) -> None:
        super().__init__(f"code {code!r} is already bound to user {user_id}")
        self.code = code
        self.user_id = user_id


class StudentNotFound(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"code {code!r} is not in the student list")
        self.code = code


def _clean_username(username: str | None) -> str | None:
    username = (username or "").strip().lstrip("@")
    return username or None


class Repo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---------- groups ----------
    def ensure_group(self, chat_id: int, title: str | None = None) -> Row:
        with self.conn:
            self.conn.execute(
                "INSERT INTO groups (chat_id, title, deadline_hours) VALUES (?, ?, 0) "
                "ON CONFLICT(chat_id) DO UPDATE SET title = COALESCE(excluded.title, title)",
                (chat_id, title),
            )
        return self.get_group(chat_id)  # type: ignore[return-value]

    def get_group(self, chat_id: int) -> Row | None:
        return self.conn.execute("SELECT * FROM groups WHERE chat_id = ?", (chat_id,)).fetchone()

    def update_group_setting(self, chat_id: int, key: str, value: Any) -> None:
        if key not in GROUP_SETTINGS:
            raise ValueError(f"unknown group setting: {key}")
        self.ensure_group(chat_id)
        with self.conn:
            self.conn.execute(f"UPDATE groups SET {key} = ? WHERE chat_id = ?", (value, chat_id))

    # ---------- students ----------
    @staticmethod
    def _prepare(
        rows: Iterable[tuple[str, str | None]],
    ) -> tuple[dict[str, tuple[str | None, str | None]], list[str]]:
        """Normalize (code, full_name) rows; name may be None/empty.
        Returns {code: (full_name, full_name_norm)} and duplicate codes within the input."""
        out: dict[str, tuple[str | None, str | None]] = {}
        dups: list[str] = []
        for raw_code, full_name in rows:
            code = normalize_code(raw_code)
            if not code:
                raise ValueError(f"empty student code: {raw_code!r}")
            name = str(full_name).strip() if full_name is not None else ""
            norm = normalize_name(name)
            if code in out:
                dups.append(code)
            else:
                out[code] = (name or None, norm or None)
        return out, dups

    def add_students(self, chat_id: int, rows: Iterable[tuple[str, str | None]]) -> tuple[int, list[str]]:
        """Insert new students. Returns (added, duplicate_codes); existing codes are left unchanged."""
        data, dups = self._prepare(rows)
        added = 0
        with self.conn:
            for code, (name, norm) in data.items():
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO students (chat_id, code, full_name, full_name_norm) "
                    "VALUES (?, ?, ?, ?)",
                    (chat_id, code, name, norm),
                )
                if cur.rowcount:
                    added += 1
                else:
                    dups.append(code)
        return added, dups

    def replace_students(
        self, chat_id: int, rows: Iterable[tuple[str, str | None]]
    ) -> tuple[int, list[str], list[Row]]:
        """Make the list exactly `rows`. Returns (count, duplicate_codes_in_input, removed_bindings).

        Bindings of codes that are no longer listed are deleted and their members revoked.
        """
        data, dups = self._prepare(rows)
        with self.conn:
            existing = {r["code"] for r in self.conn.execute(
                "SELECT code FROM students WHERE chat_id = ?", (chat_id,))}
            removed = self._drop_codes(chat_id, existing - data.keys())
            self.conn.executemany(
                "INSERT INTO students (chat_id, code, full_name, full_name_norm) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id, code) DO UPDATE SET "
                "full_name = excluded.full_name, full_name_norm = excluded.full_name_norm",
                [(chat_id, c, n, nn) for c, (n, nn) in data.items()],
            )
        return len(data), dups, removed

    def remove_student(self, chat_id: int, code: str) -> tuple[bool, list[Row]]:
        """Delete one student. Returns (removed?, removed_bindings) and revokes the bound member."""
        code = normalize_code(code)
        with self.conn:
            if not self.find_student(chat_id, code):
                return False, []
            return True, self._drop_codes(chat_id, {code})

    def clear_students(self, chat_id: int) -> tuple[int, list[Row]]:
        """Delete the whole list. Returns (deleted_count, removed_bindings)."""
        with self.conn:
            codes = {r["code"] for r in self.conn.execute(
                "SELECT code FROM students WHERE chat_id = ?", (chat_id,))}
            return len(codes), self._drop_codes(chat_id, codes)

    def _drop_codes(self, chat_id: int, codes: set[str]) -> list[Row]:
        """Delete students + their bindings, revoke members. Caller owns the transaction."""
        removed: list[Row] = []
        for code in codes:
            b = self.get_binding_by_code(chat_id, code)
            if b:
                removed.append(b)
                self.conn.execute(
                    "UPDATE members SET status = 'revoked', deadline_at = NULL "
                    "WHERE chat_id = ? AND user_id = ?",
                    (chat_id, b["user_id"]),
                )
            self.conn.execute("DELETE FROM bindings WHERE chat_id = ? AND code = ?", (chat_id, code))
            self.conn.execute("DELETE FROM students WHERE chat_id = ? AND code = ?", (chat_id, code))
        return removed

    def list_codes(self, chat_id: int) -> set[str]:
        return {r["code"] for r in self.conn.execute(
            "SELECT code FROM students WHERE chat_id = ?", (chat_id,))}

    def count_bound_codes(self, chat_id: int, codes: Iterable[str]) -> int:
        """How many of `codes` currently have a binding (e.g. would be revoked by a replace)."""
        codes = set(codes)
        if not codes:
            return 0
        bound = {r["code"] for r in self.conn.execute(
            "SELECT code FROM bindings WHERE chat_id = ?", (chat_id,))}
        return len(bound & codes)

    def students_with_bindings(self, chat_id: int) -> list[Row]:
        """All students (ordered by code) with their binding (user_id, username, bound_at)
        and member status; binding columns are NULL for unregistered codes."""
        return self.conn.execute(
            "SELECT s.code, s.full_name, b.user_id, b.username, b.full_name AS tg_name, "
            "b.bound_at, m.status "
            "FROM students s "
            "LEFT JOIN bindings b ON b.chat_id = s.chat_id AND b.code = s.code "
            "LEFT JOIN members m ON m.chat_id = b.chat_id AND m.user_id = b.user_id "
            "WHERE s.chat_id = ? ORDER BY s.code",
            (chat_id,),
        ).fetchall()

    def find_student(self, chat_id: int, code: str) -> Row | None:
        return self.conn.execute(
            "SELECT * FROM students WHERE chat_id = ? AND code = ?", (chat_id, normalize_code(code))
        ).fetchone()

    # ---------- bindings ----------
    def bind(
        self, chat_id: int, user_id: int, code: str, username: str | None,
        full_name: str | None, now: int,
    ) -> str | None:
        """Bind user to code. Returns the user's previous code if it changed, else None.

        Raises StudentNotFound if the code is not listed and CodeAlreadyBound if another
        user holds it. A user re-binding to a different code releases the old one.
        """
        with self.conn:
            return self._bind_tx(chat_id, user_id, code, username, full_name, now)

    def _bind_tx(
        self, chat_id: int, user_id: int, code: str, username: str | None,
        full_name: str | None, now: int,
    ) -> str | None:
        """Body of bind(); the caller owns the transaction."""
        code = normalize_code(code)
        if not self.find_student(chat_id, code):
            raise StudentNotFound(code)
        holder = self.get_binding_by_code(chat_id, code)
        if holder and holder["user_id"] != user_id:
            raise CodeAlreadyBound(code, holder["user_id"])
        prev = self.get_binding_by_user(chat_id, user_id)
        try:
            self.conn.execute(
                "INSERT INTO bindings (chat_id, user_id, code, username, full_name, bound_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id, user_id) DO UPDATE SET "
                "code = excluded.code, username = excluded.username, "
                "full_name = excluded.full_name, bound_at = excluded.bound_at",
                (chat_id, user_id, code, _clean_username(username), full_name, now),
            )
        except sqlite3.IntegrityError:  # lost a race for the same code
            raise CodeAlreadyBound(code, -1) from None
        if prev and prev["code"] != code:
            return prev["code"]
        return None

    def bind_and_verify(
        self, chat_id: int, user_id: int, code: str, username: str | None,
        full_name: str | None, now: int,
    ) -> str | None:
        """bind + set_status('verified') + reset_attempts in ONE transaction (all or nothing).
        Raises like bind(); returns the previous code if it changed."""
        with self.conn:
            prev = self._bind_tx(chat_id, user_id, code, username, full_name, now)
            self._set_status_tx(chat_id, user_id, "verified", now, None)
            self.conn.execute(
                "DELETE FROM attempts WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        return prev

    def get_binding_by_user(self, chat_id: int, user_id: int) -> Row | None:
        return self.conn.execute(
            "SELECT * FROM bindings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()

    def get_binding_by_code(self, chat_id: int, code: str) -> Row | None:
        return self.conn.execute(
            "SELECT * FROM bindings WHERE chat_id = ? AND code = ?", (chat_id, normalize_code(code))
        ).fetchone()

    def get_binding_by_username(self, chat_id: int, username: str) -> Row | None:
        return self.conn.execute(
            "SELECT * FROM bindings WHERE chat_id = ? AND username = ? COLLATE NOCASE",
            (chat_id, _clean_username(username)),
        ).fetchone()

    def unbind_code(self, chat_id: int, code: str) -> Row | None:
        """Remove the binding of a code; returns the removed binding (member status untouched)."""
        with self.conn:
            b = self.get_binding_by_code(chat_id, code)
            if b:
                self.conn.execute(
                    "DELETE FROM bindings WHERE chat_id = ? AND code = ?", (chat_id, b["code"]))
        return b

    def release_binding(
        self, chat_id: int, code: str, now: int, deadline_at: int | None
    ) -> Row | None:
        """/unbind: remove the code's binding and put its user back to 'pending' with a fresh
        deadline (None = no kick), in ONE transaction. Returns the removed binding or None."""
        with self.conn:
            b = self.get_binding_by_code(chat_id, code)
            if b is None:
                return None
            self.conn.execute(
                "DELETE FROM bindings WHERE chat_id = ? AND code = ?", (chat_id, b["code"]))
            self._set_status_tx(chat_id, b["user_id"], "pending", now, deadline_at)
        return b

    # ---------- members ----------
    def upsert_pending(self, chat_id: int, user_id: int, now: int, deadline_at: int | None) -> Row:
        """Register a new member as pending. Existing rows are NOT changed (a pending member
        keeps the original deadline; verified/revoked stay as they are). deadline_at=None
        means "never kicked" (deadline_hours = 0). Returns the row."""
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO members (chat_id, user_id, status, first_seen, deadline_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (chat_id, user_id, now, deadline_at),
            )
        return self.get_member(chat_id, user_id)  # type: ignore[return-value]

    def set_status(
        self, chat_id: int, user_id: int, status: str, now: int, deadline_at: int | None = None
    ) -> None:
        """Set status (creating the row if needed). deadline_at is only kept for 'pending'."""
        with self.conn:
            self._set_status_tx(chat_id, user_id, status, now, deadline_at)

    def _set_status_tx(
        self, chat_id: int, user_id: int, status: str, now: int, deadline_at: int | None
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"bad status: {status}")
        deadline = deadline_at if status == "pending" else None
        self.conn.execute(
            "INSERT INTO members (chat_id, user_id, status, first_seen, deadline_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "status = excluded.status, deadline_at = excluded.deadline_at",
            (chat_id, user_id, status, now, deadline),
        )

    def get_member(self, chat_id: int, user_id: int) -> Row | None:
        return self.conn.execute(
            "SELECT * FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()

    def delete_member(self, chat_id: int, user_id: int) -> None:
        """Forget a member (e.g. after kick/leave) so a rejoin gets a fresh deadline."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))

    def clear_pending_deadlines(self, chat_id: int) -> int:
        """Kick turned off: no pending member has a deadline any more. Returns rows changed."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE members SET deadline_at = NULL "
                "WHERE chat_id = ? AND status = 'pending' AND deadline_at IS NOT NULL",
                (chat_id,))
        return cur.rowcount

    def count_pending_without_deadline(self, chat_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM members WHERE chat_id = ? AND status = 'pending' "
            "AND deadline_at IS NULL", (chat_id,)).fetchone()[0]

    def fill_pending_deadlines(self, chat_id: int, deadline_at: int) -> int:
        """Kick turned (back) on: pending members without a deadline get `deadline_at`.
        Existing deadlines are left alone. Returns rows changed."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE members SET deadline_at = ? "
                "WHERE chat_id = ? AND status = 'pending' AND deadline_at IS NULL",
                (deadline_at, chat_id))
        return cur.rowcount

    def due_for_kick(self, now: int) -> list[Row]:
        return self.conn.execute(
            "SELECT * FROM members WHERE status = 'pending' AND deadline_at IS NOT NULL "
            "AND deadline_at <= ? AND unban_pending = 0 ORDER BY deadline_at",
            (now,),
        ).fetchall()

    def set_unban_pending(self, chat_id: int, user_id: int, pending: bool) -> None:
        """Banned by the sweeper but the unban failed: only the unban is retried later."""
        with self.conn:
            self.conn.execute(
                "UPDATE members SET unban_pending = ? WHERE chat_id = ? AND user_id = ?",
                (1 if pending else 0, chat_id, user_id))

    def due_unbans(self) -> list[Row]:
        return self.conn.execute(
            "SELECT * FROM members WHERE unban_pending = 1 ORDER BY chat_id, user_id").fetchall()

    # ---------- attempts (per user) ----------
    def _record_failure(
        self, table: str, key_col: str, chat_id: int, key: Any, now: int,
        max_attempts: int, lock_seconds: int,
    ) -> tuple[int, int | None]:
        with self.conn:
            row = self.conn.execute(
                f"SELECT fails, locked_until FROM {table} WHERE chat_id = ? AND {key_col} = ?",
                (chat_id, key),
            ).fetchone()
            if row and row["locked_until"] is not None and row["locked_until"] > now:
                return row["fails"], row["locked_until"]  # already locked: don't count
            fails = (row["fails"] if row and row["locked_until"] is None else 0) + 1
            locked_until = now + lock_seconds if fails >= max_attempts else None
            self.conn.execute(
                f"INSERT INTO {table} (chat_id, {key_col}, fails, locked_until) VALUES (?, ?, ?, ?) "
                f"ON CONFLICT(chat_id, {key_col}) DO UPDATE SET "
                "fails = excluded.fails, locked_until = excluded.locked_until",
                (chat_id, key, fails, locked_until),
            )
        return fails, locked_until

    def _locked_until(self, table: str, key_col: str, chat_id: int, key: Any, now: int) -> int | None:
        row = self.conn.execute(
            f"SELECT locked_until FROM {table} WHERE chat_id = ? AND {key_col} = ?", (chat_id, key)
        ).fetchone()
        if row and row["locked_until"] is not None and row["locked_until"] > now:
            return row["locked_until"]
        return None

    def _reset(self, table: str, key_col: str, chat_id: int, key: Any) -> None:
        with self.conn:
            self.conn.execute(f"DELETE FROM {table} WHERE chat_id = ? AND {key_col} = ?", (chat_id, key))

    def record_failure(
        self, chat_id: int, user_id: int, now: int, max_attempts: int, lock_seconds: int
    ) -> tuple[int, int | None]:
        """Count a failed attempt. Returns (fails, locked_until or None). Reaching
        max_attempts locks until now+lock_seconds; after the lock expires counting restarts."""
        return self._record_failure("attempts", "user_id", chat_id, user_id, now,
                                    max_attempts, lock_seconds)

    def is_locked(self, chat_id: int, user_id: int, now: int) -> int | None:
        """Returns locked_until (epoch) if the user is locked now, else None."""
        return self._locked_until("attempts", "user_id", chat_id, user_id, now)

    def reset_attempts(self, chat_id: int, user_id: int) -> None:
        self._reset("attempts", "user_id", chat_id, user_id)

    def get_attempts(self, chat_id: int, user_id: int) -> Row | None:
        return self.conn.execute(
            "SELECT * FROM attempts WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()

    # ---------- bot admins ----------
    def add_admin(self, user_id: int, added_by: int | None, now: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO bot_admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
                (user_id, added_by, now),
            )
        return cur.rowcount > 0

    def remove_admin(self, user_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM bot_admins WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0

    def list_admins(self) -> list[Row]:
        return self.conn.execute("SELECT * FROM bot_admins ORDER BY added_at").fetchall()

    def is_admin(self, user_id: int) -> bool:
        """DB admins only; env ADMIN_IDS are checked by the caller."""
        return self.conn.execute(
            "SELECT 1 FROM bot_admins WHERE user_id = ?", (user_id,)).fetchone() is not None

    # ---------- admin log ----------
    def log_admin(
        self, admin_id: int, chat_id: int | None, action: str,
        details: dict[str, Any] | None, now: int,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO admin_log (admin_id, chat_id, action, details_json, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (admin_id, chat_id, action,
                 json.dumps(details, ensure_ascii=False) if details is not None else None, now),
            )
        return cur.lastrowid  # type: ignore[return-value]

    def recent_admin_log(self, limit: int) -> list[Row]:
        """The newest `limit` admin_log rows, newest first."""
        return self.conn.execute(
            "SELECT * FROM admin_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---------- cleanup ----------
    def add_cleanup(self, chat_id: int, message_id: int, delete_at: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO cleanup (chat_id, message_id, delete_at) VALUES (?, ?, ?)",
                (chat_id, message_id, delete_at),
            )

    def due_cleanup(self, now: int) -> list[Row]:
        return self.conn.execute(
            "SELECT * FROM cleanup WHERE delete_at <= ? ORDER BY delete_at", (now,)).fetchall()

    def delete_cleanup(self, chat_id: int, message_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM cleanup WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))

    # ---------- stats ----------
    def stats(self, chat_id: int, now: int) -> dict[str, int]:
        def count(sql: str, *args: Any) -> int:
            return self.conn.execute(sql, (chat_id, *args)).fetchone()[0]

        result = {
            "students": count("SELECT COUNT(*) FROM students WHERE chat_id = ?"),
            "bound": count("SELECT COUNT(*) FROM bindings WHERE chat_id = ?"),
            "locked_users": count(
                "SELECT COUNT(*) FROM attempts WHERE chat_id = ? AND locked_until > ?", now),
        }
        for status in ("pending", "verified", "revoked"):
            result[status] = count(
                "SELECT COUNT(*) FROM members WHERE chat_id = ? AND status = ?", status)
        return result
