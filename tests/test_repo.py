import json

import pytest

from bot.repo import CodeAlreadyBound, StudentNotFound
from tests.conftest import CHAT

# Fake sample data only (9- and 11-digit codes), never real students.
ROWS = [("۴۰۰۱۱۱۲۲۲", "نام آزمایشی یک"), ("40011122233", "نام آزمایشی دو"), ("012345678", None)]


def test_group_settings(repo):
    g = repo.ensure_group(CHAT, "G")
    assert (g["deadline_hours"], g["max_attempts"], g["lock_minutes"]) == (0, 5, 60)  # removal off by default
    repo.update_group_setting(CHAT, "max_attempts", 3)
    assert repo.get_group(CHAT)["max_attempts"] == 3
    assert repo.ensure_group(CHAT)["title"] == "G"  # None title does not overwrite
    with pytest.raises(ValueError):
        repo.update_group_setting(CHAT, "chat_id; DROP TABLE groups", 1)


def test_add_students_and_duplicates(repo):
    added, dups = repo.add_students(CHAT, ROWS + [("400111222", "تکراری")])
    assert added == 3 and dups == ["400111222"]
    assert repo.find_student(CHAT, "400111222")["full_name"] == "نام آزمایشی یک"
    assert repo.find_student(CHAT, "۰۱۲۳۴۵۶۷۸")["full_name"] is None  # optional name, zero kept
    added, dups = repo.add_students(CHAT, [("400111222", "x"), ("500000002", "")])
    assert added == 1 and dups == ["400111222"]
    assert repo.find_student(CHAT, "400111222")["full_name"] == "نام آزمایشی یک"  # unchanged
    assert repo.find_student(CHAT, "500000002")["full_name"] is None
    with pytest.raises(ValueError):
        repo.add_students(CHAT, [(" - ", "x")])


def test_bind_unique_and_rebind(repo):
    repo.add_students(CHAT, ROWS)
    assert repo.bind(CHAT, 1, "400111222", "@UserOne", "U One", now=100) is None
    with pytest.raises(CodeAlreadyBound) as e:
        repo.bind(CHAT, 2, "۴۰۰۱۱۱۲۲۲", "two", "U Two", now=101)
    assert e.value.user_id == 1
    with pytest.raises(StudentNotFound):
        repo.bind(CHAT, 2, "999999999", None, None, now=101)
    # same user, same code: idempotent, refreshes username
    assert repo.bind(CHAT, 1, "400111222", "newname", "U One", now=102) is None
    assert repo.get_binding_by_username(CHAT, "@NEWNAME")["user_id"] == 1
    # same user, different code: binding moves, old code is freed
    assert repo.bind(CHAT, 1, "40011122233", "newname", "U One", now=103) == "400111222"
    assert repo.get_binding_by_code(CHAT, "400111222") is None
    assert repo.bind(CHAT, 2, "400111222", None, None, now=104) is None
    assert repo.get_binding_by_user(CHAT, 1)["code"] == "40011122233"


def test_unbind_code(repo):
    repo.add_students(CHAT, ROWS)
    repo.bind(CHAT, 1, "400111222", None, None, now=1)
    assert repo.unbind_code(CHAT, "400111222")["user_id"] == 1
    assert repo.unbind_code(CHAT, "400111222") is None
    assert repo.get_binding_by_user(CHAT, 1) is None


def test_remove_student_revokes(repo):
    repo.add_students(CHAT, ROWS)
    repo.bind(CHAT, 1, "400111222", None, None, now=1)
    repo.set_status(CHAT, 1, "verified", now=1)
    removed, bindings = repo.remove_student(CHAT, "۴۰۰۱۱۱۲۲۲")
    assert removed and [b["user_id"] for b in bindings] == [1]
    assert repo.get_binding_by_user(CHAT, 1) is None
    assert repo.get_member(CHAT, 1)["status"] == "revoked"
    assert repo.remove_student(CHAT, "400111222") == (False, [])
    assert repo.remove_student(CHAT, "012345678") == (True, [])


def test_replace_students(repo):
    repo.add_students(CHAT, ROWS)
    repo.bind(CHAT, 1, "400111222", None, None, now=1)
    repo.bind(CHAT, 2, "40011122233", None, None, now=1)
    count, dups, removed = repo.replace_students(
        CHAT, [("40011122233", "نام جدید"), ("500000001", None), ("500000001", None)])
    assert count == 2 and dups == ["500000001"]
    assert [b["user_id"] for b in removed] == [1]
    assert repo.find_student(CHAT, "400111222") is None
    assert repo.find_student(CHAT, "40011122233")["full_name"] == "نام جدید"
    assert repo.get_binding_by_user(CHAT, 2) is not None  # kept code keeps its binding


def test_clear_students(repo):
    repo.add_students(CHAT, ROWS)
    repo.bind(CHAT, 1, "400111222", None, None, now=1)
    count, removed = repo.clear_students(CHAT)
    assert count == 3 and len(removed) == 1
    assert repo.stats(CHAT, now=0)["students"] == 0


def test_attempt_lock_with_fake_clock(repo):
    now = 1000
    for i in range(1, 3):
        assert repo.record_failure(CHAT, 7, now, max_attempts=3, lock_seconds=60) == (i, None)
    assert repo.record_failure(CHAT, 7, now, max_attempts=3, lock_seconds=60) == (3, 1060)
    assert repo.is_locked(CHAT, 7, now + 59) == 1060
    # failures while locked do not extend the lock
    assert repo.record_failure(CHAT, 7, now + 30, 3, 60) == (3, 1060)
    assert repo.is_locked(CHAT, 7, now + 60) is None
    # counting restarts after the lock expires
    assert repo.record_failure(CHAT, 7, now + 61, 3, 60) == (1, None)
    repo.reset_attempts(CHAT, 7)
    assert repo.record_failure(CHAT, 7, now + 62, 3, 60) == (1, None)
    assert repo.is_locked(CHAT, 8, now) is None


def test_members_and_due_for_kick(repo):
    m = repo.upsert_pending(CHAT, 1, now=0, deadline_at=100)
    assert m["status"] == "pending" and m["deadline_at"] == 100
    # rejoin does not extend the deadline
    assert repo.upsert_pending(CHAT, 1, now=50, deadline_at=150)["deadline_at"] == 100
    repo.upsert_pending(CHAT, 2, now=0, deadline_at=200)
    repo.upsert_pending(CHAT, 3, now=0, deadline_at=50)
    repo.set_status(CHAT, 3, "verified", now=10)
    assert repo.get_member(CHAT, 3)["deadline_at"] is None
    assert repo.upsert_pending(CHAT, 3, now=20, deadline_at=99)["status"] == "verified"
    assert [r["user_id"] for r in repo.due_for_kick(99)] == []
    assert [r["user_id"] for r in repo.due_for_kick(100)] == [1]
    assert [r["user_id"] for r in repo.due_for_kick(500)] == [1, 2]
    repo.delete_member(CHAT, 1)
    assert repo.get_member(CHAT, 1) is None
    with pytest.raises(ValueError):
        repo.set_status(CHAT, 1, "bogus", now=0)


def test_admins_log_cleanup_stats(repo):
    assert repo.add_admin(5, added_by=1, now=10)
    assert not repo.add_admin(5, added_by=1, now=11)
    assert repo.is_admin(5) and not repo.is_admin(6)
    assert [r["user_id"] for r in repo.list_admins()] == [5]
    assert repo.remove_admin(5) and not repo.remove_admin(5)

    log_id = repo.log_admin(1, CHAT, "add", {"code": "۱۲"}, now=5)
    row = repo.conn.execute("SELECT * FROM admin_log WHERE id = ?", (log_id,)).fetchone()
    assert json.loads(row["details_json"]) == {"code": "۱۲"}

    repo.add_cleanup(CHAT, 10, delete_at=100)
    repo.add_cleanup(CHAT, 11, delete_at=200)
    assert [r["message_id"] for r in repo.due_cleanup(150)] == [10]
    repo.delete_cleanup(CHAT, 10)
    assert repo.due_cleanup(150) == []

    repo.add_students(CHAT, ROWS)
    repo.bind(CHAT, 1, "400111222", None, None, now=1)
    repo.set_status(CHAT, 1, "verified", now=1)
    repo.upsert_pending(CHAT, 2, now=1, deadline_at=9)
    repo.record_failure(CHAT, 2, 1, max_attempts=1, lock_seconds=60)
    assert repo.stats(CHAT, now=2) == {
        "students": 3, "bound": 1, "locked_users": 1,
        "pending": 1, "verified": 1, "revoked": 0,
    }
