from types import SimpleNamespace

import pytest

from bot.verify import GroupSettings, VerifyStatus as S, load_settings, looks_like_code, verify_code
from tests.conftest import CHAT

SETTINGS = GroupSettings(max_attempts=3, lock_minutes=10)


def user(uid=1, username="u1", name="User One"):
    return SimpleNamespace(id=uid, username=username, full_name=name)


@pytest.fixture
def listed(repo):
    repo.add_students(CHAT, [("400111222", "رضایی-علی"), ("400111333", None)])
    return repo


def test_looks_like_code():
    assert looks_like_code("۴۰۰۱۱۱۲۲۲")
    assert looks_like_code(" 400-111 222 ")
    assert not looks_like_code("سلام")
    assert not looks_like_code("abc123456")
    assert not looks_like_code("12")
    assert not looks_like_code("")


def test_ok_binds_verifies_and_resets_attempts(listed):
    listed.record_failure(CHAT, 1, 0, 3, 600)
    r = verify_code(listed, CHAT, user(), "400111222", 100, SETTINGS)
    assert r.status is S.OK and r.code == "400111222" and r.student_name == "رضایی-علی"
    b = listed.get_binding_by_user(CHAT, 1)
    assert (b["code"], b["username"], b["full_name"]) == ("400111222", "u1", "User One")
    assert listed.get_member(CHAT, 1)["status"] == "verified"
    assert listed.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_persian_digits(listed):
    assert verify_code(listed, CHAT, user(), "۴۰۰۱۱۱۲۲۲", 100, SETTINGS).status is S.OK


def test_ok_turns_pending_member_verified(listed):
    listed.upsert_pending(CHAT, 1, 0, 500)
    verify_code(listed, CHAT, user(), "400111222", 100, SETTINGS)
    m = listed.get_member(CHAT, 1)
    assert m["status"] == "verified" and m["deadline_at"] is None


def test_already_yours(listed):
    verify_code(listed, CHAT, user(), "400111222", 100, SETTINGS)
    r = verify_code(listed, CHAT, user(), "400 111 222", 101, SETTINGS)
    assert r.status is S.ALREADY_YOURS and r.fails == 0


def test_verified_user_with_other_code_is_refused(listed):
    verify_code(listed, CHAT, user(), "400111222", 100, SETTINGS)
    r = verify_code(listed, CHAT, user(), "400111333", 101, SETTINGS)
    assert r.status is S.HAS_OTHER_CODE and r.code == "400111222"
    assert listed.get_binding_by_user(CHAT, 1)["code"] == "400111222"
    assert listed.get_binding_by_code(CHAT, "400111333") is None


def test_bound_to_other_counts(listed):
    verify_code(listed, CHAT, user(1), "400111222", 100, SETTINGS)
    r = verify_code(listed, CHAT, user(2), "400111222", 101, SETTINGS)
    assert r.status is S.BOUND_TO_OTHER and r.fails == 1
    assert listed.get_binding_by_code(CHAT, "400111222")["user_id"] == 1


def test_not_found_counts_and_locks_after_max(listed):
    r1 = verify_code(listed, CHAT, user(), "999999999", 100, SETTINGS)
    r2 = verify_code(listed, CHAT, user(), "999999998", 101, SETTINGS)
    assert (r1.status, r1.fails, r2.fails) == (S.NOT_FOUND, 1, 2)
    assert not r2.just_locked
    r3 = verify_code(listed, CHAT, user(), "999999997", 102, SETTINGS)
    assert r3.status is S.NOT_FOUND and r3.just_locked
    assert r3.locked_until == 102 + 600 and r3.seconds_left == 600
    # locked: even the right code is refused, with time left
    r4 = verify_code(listed, CHAT, user(), "400111222", 402, SETTINGS)
    assert r4.status is S.LOCKED and r4.seconds_left == 300 and not r4.just_locked
    assert listed.get_binding_by_user(CHAT, 1) is None
    # lock expires
    assert verify_code(listed, CHAT, user(), "400111222", 702, SETTINGS).status is S.OK


def test_invalid_format_not_counted(listed):
    for i in range(10):
        r = verify_code(listed, CHAT, user(), "سلام", 100 + i, SETTINGS)
        assert r.status is S.INVALID_FORMAT
    assert listed.is_locked(CHAT, 1, 200) is None
    assert listed.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_bind_verify_is_one_transaction(listed):
    # make the member write fail: the binding must be rolled back too
    listed.conn.executescript(
        "CREATE TRIGGER boom BEFORE INSERT ON members BEGIN SELECT RAISE(ABORT, 'boom'); END;")
    listed.record_failure(CHAT, 1, 0, 3, 600)
    with pytest.raises(Exception, match="boom"):
        verify_code(listed, CHAT, user(), "400111222", 100, SETTINGS)
    assert listed.get_binding_by_user(CHAT, 1) is None
    assert listed.get_member(CHAT, 1) is None
    assert listed.conn.execute("SELECT fails FROM attempts").fetchone()[0] == 1
    assert not listed.conn.in_transaction


def test_load_settings_defaults_and_row(repo):
    assert load_settings(repo, CHAT) == GroupSettings()
    repo.update_group_setting(CHAT, "max_attempts", 7)
    repo.update_group_setting(CHAT, "contact_text", "به @help پیام بدهید")
    s = load_settings(repo, CHAT)
    assert s.max_attempts == 7 and s.contact_text == "به @help پیام بدهید"
