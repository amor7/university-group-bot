"""Stage 5: user/settings/admin commands. Pure logic first, then handlers with the AsyncMock
fake bot from test_handlers (no network)."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import BotCommandScopeChat, BotCommandScopeDefault
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop

from bot import main as bot_main, texts
from bot.handlers import admin, group, manage, private
from bot.handlers.manage import AdminChange, ManualStatus, UsageError
from bot.jobs import pick_kicks
from tests.conftest import CHAT
from tests.test_handlers import ADMIN, STUDENT, cfg, make_context, tg_user
from tests.test_handlers import private_update as _private_update

OWNER = ADMIN
DB_ADMIN = 2
REPORT = -2002


def run(coro):
    return asyncio.run(coro)


def private_update(user, text):
    upd = _private_update(user, text)
    upd.effective_chat = SimpleNamespace(id=user.id, type=ChatType.PRIVATE)
    upd.callback_query = None
    return upd


def listed(repo, *codes):
    repo.add_students(CHAT, [(c, None) for c in codes])


# ====================== parsing ======================
def test_parse_deadline():
    assert manage.parse_deadline(["0"]) == 0
    assert manage.parse_deadline(["۷۲۰"]) == 720
    assert manage.parse_deadline(["1"]) == 1
    for bad in (["721"], ["-1"], ["x"], ["1.5"], [], ["1", "2"]):
        with pytest.raises(UsageError):
            manage.parse_deadline(bad)


def test_parse_attempts():
    assert manage.parse_attempts(["5"]) == (5, None)
    assert manage.parse_attempts(["۳", "۹۰"]) == (3, 90)
    for bad in (["0"], ["21"], ["5", "0"], ["5", "10081"], [], ["a"], ["1", "2", "3"]):
        with pytest.raises(UsageError):
            manage.parse_attempts(bad)


def test_parse_contact():
    assert manage.parse_contact("") == ("show", None)
    assert manage.parse_contact("  RESET ") == ("reset", None)
    assert manage.parse_contact("به آموزش\nپیام بدهید") == ("set", "به آموزش\nپیام بدهید")
    with pytest.raises(UsageError):
        manage.parse_contact("x" * (manage.CONTACT_MAX_LEN + 1))


def test_parse_report():
    assert manage.parse_report([]) == ("show", None)
    assert manage.parse_report(["off"]) == ("off", None)
    assert manage.parse_report(["-1001234"]) == ("set", -1001234)
    assert manage.parse_report(["‎-۱۰۰۵"]) == ("set", -1005)
    for bad in (["abc"], ["0"], ["-"], ["1", "2"]):
        with pytest.raises(UsageError):
            manage.parse_report(bad)


def test_parse_verify_user():
    assert manage.parse_verify_user(["۷", "۴۰۰۱۱۱۲۲۲"]) == (7, "400111222")
    with pytest.raises(UsageError, match="user_id"):
        manage.parse_verify_user(["@x", "400111222"])
    with pytest.raises(UsageError, match="معتبر"):
        manage.parse_verify_user(["7", "12"])
    with pytest.raises(UsageError):
        manage.parse_verify_user(["7"])


def test_parse_logs_n_and_user_id():
    assert manage.parse_logs_n([]) == 20
    assert manage.parse_logs_n(["5"]) == 5
    assert manage.parse_logs_n(["500"]) == 100
    with pytest.raises(UsageError):
        manage.parse_logs_n(["0"])
    assert manage.parse_user_id("۱۲") == 12
    assert manage.parse_user_id("0") is None and manage.parse_user_id("-5") is None


def test_chunk_lines():
    assert manage.chunk_lines(["aa", "bb", "cc"], limit=5) == ["aa\nbb", "cc"]
    assert manage.chunk_lines(["x" * 10], limit=4) == ["xxxx"]
    assert manage.chunk_lines([]) == []


def test_registration_percent_and_stats_text(repo):
    listed(repo, "400111222", "400111223", "400111224", "400111225")
    repo.bind(CHAT, 7, "400111222", None, None, 1)
    repo.upsert_pending(CHAT, 8, 0, 100)
    repo.record_failure(CHAT, 9, 0, 1, 60)
    s = repo.stats(CHAT, 1)
    assert manage.registration_percent(s) == 25.0
    assert manage.registration_percent({"students": 0, "bound": 0}) == 0.0
    text = texts.stats(s, manage.registration_percent(s))
    assert "۲۵.۰٪" in text and "ثبت‌نشده‌های لیست: ۳" in text
    assert "(pending): ۱" in text and "قفل‌شده در این لحظه: ۱" in text


# ====================== lookup / whois / unbind ======================
def test_lookup_code_first_then_user_id(repo):
    listed(repo, "400111222")
    repo.bind(CHAT, 7, "400111222", "Ali", "Ali R", 1)
    by_code = manage.lookup(repo, CHAT, "۴۰۰۱۱۱۲۲۲", 10)
    assert (by_code.code, by_code.user_id) == ("400111222", 7)
    by_uid = manage.lookup(repo, CHAT, "7", 10)
    assert (by_uid.code, by_uid.user_id) == ("400111222", 7)
    by_name = manage.lookup(repo, CHAT, "@ali", 10)
    assert by_name.binding["user_id"] == 7 and by_name.student is not None
    assert not manage.lookup(repo, CHAT, "@nobody", 10).found
    assert not manage.lookup(repo, CHAT, "123456789", 10).found


def test_lookup_code_beats_user_id_with_same_digits(repo):
    listed(repo, "500000001")
    repo.bind(CHAT, 500000002, "500000001", None, None, 1)
    listed(repo, "500000002")  # a code equal to someone's user_id, unbound
    lk = manage.lookup(repo, CHAT, "500000002", 10)
    assert lk.code == "500000002" and lk.binding is None and lk.user_id is None


def test_lookup_unbound_user_with_lock(repo):
    repo.upsert_pending(CHAT, 9, 0, 3600)
    repo.record_failure(CHAT, 9, 0, 1, 600)
    lk = manage.lookup(repo, CHAT, "9", 10)
    assert lk.found and lk.code is None and lk.locked_until == 600
    text = manage.whois_text(lk)
    assert "هیچ کدی ثبت نکرده" in text and "🔒" in text and "/unlock 9" in text
    assert "در انتظار ثبت" in text


def test_whois_text_full(repo):
    repo.add_students(CHAT, [("400111222", "علی رضایی")])
    repo.bind(CHAT, 7, "400111222", "ali", "Ali R", 1_700_000_000)
    repo.set_status(CHAT, 7, "verified", 1)
    text = manage.whois_text(manage.lookup(repo, CHAT, "400111222", 10))
    assert "۴۰۰۱۱۱۲۲۲" in text and "علی رضایی" in text and "در لیست: بله" in text
    assert "user_id 7" in text and "@ali" in text and "Ali R" in text
    assert "۲۰۲۳-۱۱-۱۵ ۰۱:۴۳" in text  # 2023-11-14 22:13 UTC = +03:30 Tehran
    assert "تأییدشده" in text and "قفل: نه" in text
    unbound = manage.whois_text(manage.lookup(repo, CHAT, "@nobody", 10))
    assert unbound == texts.NOT_FOUND_TARGET.format(query="@nobody")


def test_unbind_releases_and_sets_pending(repo):
    listed(repo, "400111222")
    repo.bind_and_verify(CHAT, 7, "400111222", "ali", None, 1)
    b = manage.unbind(repo, CHAT, "@ali", 100, deadline_hours=24)
    assert b["code"] == "400111222" and b["user_id"] == 7
    assert repo.get_binding_by_user(CHAT, 7) is None
    assert repo.find_student(CHAT, "400111222") is not None  # code stays listed
    m = repo.get_member(CHAT, 7)
    assert m["status"] == "pending" and m["deadline_at"] == 100 + 24 * 3600
    assert manage.unbind(repo, CHAT, "400111222", 100, 24) is None  # nothing left


def test_unbind_by_user_id_with_kick_off(repo):
    listed(repo, "400111222")
    repo.bind(CHAT, 7, "400111222", None, None, 1)
    assert manage.unbind(repo, CHAT, "7", 100, deadline_hours=0)["code"] == "400111222"
    assert repo.get_member(CHAT, 7)["deadline_at"] is None


def test_resolve_user_id(repo):
    listed(repo, "400111222")
    repo.bind(CHAT, 7, "400111222", "ali", None, 1)
    assert manage.resolve_user_id(repo, CHAT, "@ALI") == 7
    assert manage.resolve_user_id(repo, CHAT, "۹۹") == 99
    assert manage.resolve_user_id(repo, CHAT, "@ghost") is None


# ====================== /verify_user ======================
def test_manual_verify_adds_missing_code(repo):
    r = manage.manual_verify(repo, CHAT, 7, "400111222", "ali", "Ali", 5)
    assert r.status == ManualStatus.OK and r.added_to_list
    assert repo.find_student(CHAT, "400111222") is not None
    assert repo.get_binding_by_user(CHAT, 7)["username"] == "ali"
    assert repo.get_member(CHAT, 7)["status"] == "verified"


def test_manual_verify_bound_to_other_changes_nothing(repo):
    listed(repo, "400111222")
    repo.bind(CHAT, 8, "400111222", None, None, 1)
    r = manage.manual_verify(repo, CHAT, 7, "400111222", None, None, 5)
    assert r.status == ManualStatus.BOUND_TO_OTHER and r.holder == 8 and not r.added_to_list
    assert repo.get_binding_by_code(CHAT, "400111222")["user_id"] == 8
    assert repo.get_member(CHAT, 7) is None


def test_manual_verify_already_and_rebind(repo):
    listed(repo, "400111222", "400111333")
    repo.bind(CHAT, 7, "400111222", "ali", "Ali", 1)
    repo.record_failure(CHAT, 7, 0, 1, 600)
    r = manage.manual_verify(repo, CHAT, 7, "400111222", None, None, 5)
    assert r.status == ManualStatus.ALREADY and not r.added_to_list
    assert repo.get_binding_by_user(CHAT, 7)["username"] == "ali"  # kept when unknown
    assert repo.is_locked(CHAT, 7, 10) is None  # bind_and_verify resets attempts
    r = manage.manual_verify(repo, CHAT, 7, "400111333", None, None, 6)
    assert r.status == ManualStatus.OK and r.prev_code == "400111222"
    assert repo.get_binding_by_code(CHAT, "400111222") is None


# ====================== admins ======================
def test_change_admin_owner_rule(repo):
    config = cfg()
    assert manage.change_admin(config, repo, DB_ADMIN, 5, True, 0) == AdminChange.NOT_OWNER
    assert manage.change_admin(config, repo, OWNER, DB_ADMIN, True, 0) == AdminChange.ADDED
    assert manage.change_admin(config, repo, OWNER, DB_ADMIN, True, 0) == AdminChange.ALREADY
    # a DB admin can neither add nor remove
    assert manage.change_admin(config, repo, DB_ADMIN, 5, True, 0) == AdminChange.NOT_OWNER
    assert manage.change_admin(config, repo, DB_ADMIN, DB_ADMIN, False, 0) \
        == AdminChange.NOT_OWNER
    # env admins can't be added or removed here
    assert manage.change_admin(config, repo, OWNER, OWNER, False, 0) == AdminChange.IS_OWNER
    assert manage.change_admin(config, repo, OWNER, OWNER, True, 0) == AdminChange.IS_OWNER
    assert manage.change_admin(config, repo, OWNER, DB_ADMIN, False, 0) == AdminChange.REMOVED
    assert manage.change_admin(config, repo, OWNER, DB_ADMIN, False, 0) == AdminChange.NOT_ADMIN
    assert not repo.is_admin(DB_ADMIN)


def test_commands_for():
    config = cfg()
    names = [n for n, _ in manage.commands_for(config, OWNER)]
    assert "add_admin" in names and "unbind" in names and "start" in names
    assert "add_admin" not in [n for n, _ in manage.commands_for(config, DB_ADMIN)]


# ====================== repo / sweeper / texts for deadline 0 ======================
def test_pending_deadlines_clear_and_fill(repo):
    repo.upsert_pending(CHAT, 1, 0, 100)
    repo.upsert_pending(CHAT, 2, 0, None)
    repo.set_status(CHAT, 3, "verified", 0)
    assert repo.clear_pending_deadlines(CHAT) == 1
    assert repo.get_member(CHAT, 1)["deadline_at"] is None
    assert repo.fill_pending_deadlines(CHAT, 500) == 2
    assert repo.get_member(CHAT, 2)["deadline_at"] == 500
    assert repo.get_member(CHAT, 3)["deadline_at"] is None


def test_sweeper_respects_deadline_off(repo):
    repo.upsert_pending(CHAT, 1, 0, 100)
    repo.update_group_setting(CHAT, "deadline_hours", 0)
    assert pick_kicks(repo, 200) == []
    repo.update_group_setting(CHAT, "deadline_hours", 24)
    assert pick_kicks(repo, 200) == [(CHAT, 1)]


def test_welcome_without_deadline():
    assert "ساعت" not in texts.welcome("x", 0)
    assert "۲۴ ساعت" in texts.welcome("x", 24)


def test_gate_with_deadline_off_registers_without_deadline(repo):
    repo.update_group_setting(CHAT, "deadline_hours", 0)
    ctx = make_context(repo)
    msg = SimpleNamespace(from_user=tg_user(), sender_chat=None, is_automatic_forward=None,
                          message_id=1, text="hi", delete=AsyncMock())
    upd = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=CHAT),
                          effective_user=msg.from_user)
    with pytest.raises(ApplicationHandlerStop):
        run(group.on_group_message(upd, ctx))
    m = repo.get_member(CHAT, STUDENT)
    assert m["status"] == "pending" and m["deadline_at"] is None


def test_recent_admin_log(repo):
    for i in range(3):
        repo.log_admin(1, CHAT, f"a{i}", {"i": i}, i)
    assert [r["action"] for r in repo.recent_admin_log(2)] == ["a2", "a1"]
    line = texts.log_line(repo.recent_admin_log(1)[0])
    assert line.startswith("#3 ") and "a2" in line and '{"i": 2}' in line


# ====================== handlers ======================
def admin_update(text, uid=ADMIN):
    return private_update(tg_user(uid), text)


def reply_of(upd):
    return upd.effective_message.reply_text.await_args.args[0]


def last_log(repo):
    row = repo.recent_admin_log(1)[0]
    return row["action"], json.loads(row["details_json"]) if row["details_json"] else None


def test_commands_refuse_non_admins_and_groups(repo):
    ctx = make_context(repo)
    upd = private_update(tg_user(), "/stats")
    run(manage.cmd_stats(upd, ctx))
    assert reply_of(upd) == texts.NO_ACCESS
    upd = admin_update("/stats")
    upd.effective_chat = SimpleNamespace(id=CHAT, type=ChatType.SUPERGROUP)
    run(manage.cmd_stats(upd, ctx))
    upd.effective_message.reply_text.assert_not_awaited()


def test_cmd_unbind_restricts_notifies_user_logs_and_reports(repo):
    listed(repo, "400111222")
    repo.bind_and_verify(CHAT, STUDENT, "400111222", "user7", None, 1)
    repo.update_group_setting(CHAT, "report_chat_id", REPORT)
    ctx = make_context(repo)
    upd = admin_update("/unbind 400111222")
    run(manage.cmd_unbind(upd, ctx))
    assert "آزاد شد" in reply_of(upd)
    assert repo.get_member(CHAT, STUDENT)["status"] == "pending"
    ctx.application.create_task.call_args.args[0].close()  # background revoke was scheduled
    sent = [c.args for c in ctx.bot.send_message.await_args_list]
    assert sent[0] == (STUDENT, texts.UNBOUND_USER_DM)
    assert sent[1][0] == REPORT and "👮" in sent[1][1]
    assert last_log(repo) == ("unbind", {"code": "400111222", "user_id": STUDENT,
                                         "username": "user7"})


def test_cmd_unbind_user_dm_failure_is_silent(repo):
    listed(repo, "400111222")
    repo.bind(CHAT, STUDENT, "400111222", None, None, 1)
    ctx = make_context(repo)
    ctx.bot.send_message.side_effect = Forbidden("blocked")
    upd = admin_update("/unbind 7")
    run(manage.cmd_unbind(upd, ctx))
    assert "آزاد شد" in reply_of(upd)
    ctx.application.create_task.call_args.args[0].close()


def test_cmd_unbind_not_found_and_usage(repo):
    ctx = make_context(repo)
    upd = admin_update("/unbind @ghost")
    run(manage.cmd_unbind(upd, ctx))
    assert reply_of(upd) == texts.UNBIND_NO_BINDING.format(query="@ghost")
    upd = admin_update("/unbind")
    run(manage.cmd_unbind(upd, ctx))
    assert reply_of(upd) == texts.UNBIND_USAGE
    assert repo.recent_admin_log(1) == []


def test_cmd_verify_user_admits_and_reports(repo):
    ctx = make_context(repo)
    ctx.bot.get_chat_member.return_value = SimpleNamespace(user=tg_user(), status="restricted")
    upd = admin_update(f"/verify_user {STUDENT} 400111999")
    run(manage.cmd_verify_user(upd, ctx))
    text = reply_of(upd)
    assert "به لیست اضافه شد" in text and "وصل و تأیید شد" in text
    assert ctx.bot.restrict_chat_member.await_args.args[2].can_send_photos is True
    ctx.bot.approve_chat_join_request.assert_awaited_once_with(CHAT, STUDENT)
    assert repo.get_binding_by_user(CHAT, STUDENT)["username"] == "user7"
    assert last_log(repo)[0] == "verify_user"


def test_cmd_verify_user_code_held_by_other(repo):
    listed(repo, "400111222")
    repo.bind(CHAT, 8, "400111222", None, None, 1)
    ctx = make_context(repo)
    ctx.bot.get_chat_member.side_effect = BadRequest("user not found")
    upd = admin_update(f"/verify_user {STUDENT} 400111222")
    run(manage.cmd_verify_user(upd, ctx))
    assert "/unbind 400111222" in reply_of(upd)
    ctx.bot.restrict_chat_member.assert_not_awaited()
    assert repo.recent_admin_log(1) == []


def test_cmd_unlock(repo):
    repo.record_failure(CHAT, 9, 0, 1, 10**11)
    ctx = make_context(repo)
    upd = admin_update("/unlock 9")
    run(manage.cmd_unlock(upd, ctx))
    assert "قفل کاربر 9 باز شد" in reply_of(upd)
    assert repo.get_attempts(CHAT, 9) is None
    assert last_log(repo) == ("unlock", {"user_id": 9, "was_locked": True})
    upd = admin_update("/unlock @ghost")
    run(manage.cmd_unlock(upd, ctx))
    assert "user_id" in reply_of(upd)


def test_cmd_set_deadline(repo):
    repo.upsert_pending(CHAT, 1, 0, 100)
    ctx = make_context(repo)
    upd = admin_update("/set_deadline 0")
    run(manage.cmd_set_deadline(upd, ctx))
    assert repo.get_group(CHAT)["deadline_hours"] == 0
    assert repo.get_member(CHAT, 1)["deadline_at"] is None
    assert last_log(repo) == ("set_deadline", {"hours": 0, "members": 1})
    upd = admin_update("/set_deadline 999")
    run(manage.cmd_set_deadline(upd, ctx))
    assert reply_of(upd) == texts.SET_DEADLINE_USAGE
    assert repo.get_group(CHAT)["deadline_hours"] == 0
    upd = admin_update("/set_deadline")
    run(manage.cmd_set_deadline(upd, ctx))
    assert "خاموش" in reply_of(upd)


def test_cmd_set_attempts_keeps_lock_when_omitted(repo):
    ctx = make_context(repo)
    run(manage.cmd_set_attempts(admin_update("/set_attempts 3 15"), ctx))
    run(manage.cmd_set_attempts(admin_update("/set_attempts 4"), ctx))
    g = repo.get_group(CHAT)
    assert (g["max_attempts"], g["lock_minutes"]) == (4, 15)


def test_cmd_set_contact_show_set_reset(repo):
    ctx = make_context(repo)
    upd = admin_update("/set_contact")
    run(manage.cmd_set_contact(upd, ctx))
    assert "پیش‌فرض" in reply_of(upd) and "@support" in reply_of(upd)
    run(manage.cmd_set_contact(admin_update("/set_contact به آموزش\nپیام بدهید"), ctx))
    assert repo.get_group(CHAT)["contact_text"] == "به آموزش\nپیام بدهید"
    run(manage.cmd_set_contact(admin_update("/set_contact reset"), ctx))
    assert repo.get_group(CHAT)["contact_text"] is None
    assert last_log(repo) == ("set_contact", {"text": None})


def test_cmd_set_report_private(repo):
    ctx = make_context(repo)
    upd = admin_update(f"/set_report {CHAT}")
    run(manage.cmd_set_report(upd, ctx))
    assert reply_of(upd) == texts.REPORT_NOT_MAIN_GROUP
    ctx.bot.send_message.side_effect = BadRequest("chat not found")
    upd = admin_update(f"/set_report {REPORT}")
    run(manage.cmd_set_report(upd, ctx))
    assert "نتوانستم" in reply_of(upd) and repo.get_group(CHAT) is None
    ctx.bot.send_message.side_effect = None
    upd = admin_update(f"/set_report {REPORT}")
    run(manage.cmd_set_report(upd, ctx))
    assert repo.get_group(CHAT)["report_chat_id"] == REPORT
    assert ctx.bot.send_message.await_args.args == (REPORT, texts.REPORT_TEST)
    run(manage.cmd_set_report(admin_update("/set_report off"), ctx))
    assert repo.get_group(CHAT)["report_chat_id"] is None


def group_cmd(text, uid, chat_id):
    upd = private_update(tg_user(uid), text)
    upd.effective_chat = SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP)
    return upd


def test_cmd_set_report_in_group(repo):
    ctx = make_context(repo)
    upd = group_cmd("/set_report", STUDENT, REPORT)  # not a bot admin: silence
    run(manage.cmd_set_report_in_group(upd, ctx))
    upd.effective_message.reply_text.assert_not_awaited()
    assert repo.get_group(CHAT) is None
    upd = group_cmd("/set_report", ADMIN, CHAT)  # the students' group itself
    run(manage.cmd_set_report_in_group(upd, ctx))
    assert reply_of(upd) == texts.REPORT_NOT_MAIN_GROUP
    upd = group_cmd("/set_report", ADMIN, REPORT)
    run(manage.cmd_set_report_in_group(upd, ctx))
    assert repo.get_group(CHAT)["report_chat_id"] == REPORT
    assert last_log(repo) == ("set_report", {"chat_id": REPORT})
    run(manage.cmd_set_report_in_group(group_cmd("/set_report off", ADMIN, REPORT), ctx))
    assert repo.get_group(CHAT)["report_chat_id"] is None


def test_channel_set_report_only_hints(repo):
    ctx = make_context(repo)
    post = SimpleNamespace(text="/set_report", chat=SimpleNamespace(id=-300),
                           reply_text=AsyncMock())
    run(manage.on_channel_set_report(SimpleNamespace(channel_post=post), ctx))
    assert "/set_report -300" in post.reply_text.await_args.args[0]
    assert repo.get_group(CHAT) is None
    other = SimpleNamespace(text="/set_report@otherbot", chat=SimpleNamespace(id=-300),
                            reply_text=AsyncMock())
    run(manage.on_channel_set_report(SimpleNamespace(channel_post=other), ctx))
    other.reply_text.assert_not_awaited()


def test_cmd_add_admin_sets_menu_and_db_admin_is_refused(repo):
    ctx = make_context(repo)
    upd = admin_update(f"/add_admin {DB_ADMIN}")
    run(manage.cmd_add_admin(upd, ctx))
    assert repo.is_admin(DB_ADMIN)
    scope = ctx.bot.set_my_commands.await_args.kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat) and scope.chat_id == DB_ADMIN
    assert last_log(repo) == ("add_admin", {"user_id": DB_ADMIN})
    upd = admin_update("/add_admin 5", uid=DB_ADMIN)
    run(manage.cmd_add_admin(upd, ctx))
    assert reply_of(upd) == texts.ONLY_OWNERS and not repo.is_admin(5)
    upd = admin_update(f"/remove_admin {OWNER}")
    run(manage.cmd_remove_admin(upd, ctx))
    assert reply_of(upd) == texts.ADMIN_IS_OWNER_REMOVE


def test_cmd_admins_works_without_group(repo):
    repo.add_admin(DB_ADMIN, OWNER, 0)
    ctx = make_context(repo, config=cfg(group_id=None))
    upd = admin_update("/admins")
    run(manage.cmd_admins(upd, ctx))
    text = reply_of(upd)
    assert f"• {OWNER}" in text and f"• {DB_ADMIN}" in text


def test_cmd_logs(repo):
    ctx = make_context(repo)
    upd = admin_update("/logs")
    run(manage.cmd_logs(upd, ctx))
    assert reply_of(upd) == texts.LOGS_EMPTY
    for i in range(5):
        repo.log_admin(ADMIN, CHAT, f"act{i}", None, i)
    upd = admin_update("/logs 2")
    run(manage.cmd_logs(upd, ctx))
    text = reply_of(upd)
    assert "act4" in text and "act3" in text and "act2" not in text


def test_cmd_help_admin_vs_student(repo):
    ctx = make_context(repo)
    upd = admin_update("/help")
    run(manage.cmd_help(upd, ctx))
    text = reply_of(upd)
    for section in ("📋 لیست", "👤 کاربران", "⚙️ تنظیمات", "👮 ادمین‌ها", "xlsx", "/unbind"):
        assert section in text
    upd = private_update(tg_user(), "/help")
    run(manage.cmd_help(upd, ctx))
    assert reply_of(upd) == texts.HELP_USER


def test_setup_commands_default_and_admin_scopes_errors_silent(repo):
    repo.add_admin(DB_ADMIN, OWNER, 0)
    bot = AsyncMock()
    bot.set_my_commands.side_effect = [BadRequest("x"), None, BadRequest("chat not found")]
    app = SimpleNamespace(bot=bot, bot_data={admin.CONFIG_KEY: cfg(), admin.REPO_KEY: repo})
    run(manage.setup_commands(app))
    calls = bot.set_my_commands.await_args_list
    assert isinstance(calls[0].kwargs["scope"], BotCommandScopeDefault)
    assert [c.args[0][-1].command for c in calls[1:]] == ["remove_admin", "logs"]
    assert [c.kwargs["scope"].chat_id for c in calls[1:]] == [OWNER, DB_ADMIN]


def test_manage_handlers_registered_before_private_text(tmp_path):
    app = bot_main.build_application(cfg(bot_token="123:abc", db_path=str(tmp_path / "t.db")))
    try:
        callbacks = [h.callback for h in app.handlers[0]]
        assert callbacks.index(manage.cmd_unbind) < callbacks.index(private.on_private_text)
        assert manage.cmd_set_report_in_group in callbacks
    finally:
        app.bot_data[admin.REPO_KEY].conn.close()
