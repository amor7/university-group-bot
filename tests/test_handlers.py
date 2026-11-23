"""Handlers with a fake Bot (AsyncMock) and minimal fake Updates — no network."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import (
    Chat, ChatMemberAdministrator, ChatMemberBanned, ChatMemberLeft, ChatMemberMember,
    ChatMemberOwner, ChatMemberRestricted, ChatMemberUpdated, ChatPermissions, Update, User,
)
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop

from bot import main as bot_main, texts
from bot.config import Config
from bot.handlers import admin, group, join, private
from tests.conftest import CHAT

BOT_ID = 42
ADMIN = 1
STUDENT = 7
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def cfg(**kw):
    base = dict(bot_token="x", admin_ids=frozenset({ADMIN}), db_path=":memory:", group_id=CHAT,
                default_contact="@support")
    base.update(kw)
    return Config(**base)


def make_bot():
    bot = AsyncMock()
    bot.id = BOT_ID
    bot.username = "testbot"
    bot.send_message.return_value = SimpleNamespace(message_id=555)
    bot.get_chat.return_value = SimpleNamespace(
        permissions=ChatPermissions(can_send_messages=True, can_send_photos=True),
        username=None, invite_link="https://t.me/+abc", linked_chat_id=None)
    bot.get_chat_administrators.return_value = [SimpleNamespace(user=User(ADMIN, "Ad", False))]
    bot.get_chat_member.return_value = SimpleNamespace(status="restricted", user=None)
    return bot


def make_context(repo, config=None, bot=None):
    return SimpleNamespace(bot=bot or make_bot(), user_data={}, application=MagicMock(),
                           bot_data={admin.CONFIG_KEY: config or cfg(), admin.REPO_KEY: repo})


def tg_user(uid=STUDENT, is_bot=False):
    return User(uid, f"U{uid}", is_bot, username=f"user{uid}")


def group_update(user, sender_chat=None, chat_id=CHAT, text="hi"):
    msg = SimpleNamespace(from_user=user, sender_chat=sender_chat, is_automatic_forward=None,
                          message_id=100, text=text, delete=AsyncMock())
    return SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=chat_id),
                           effective_user=user)


def private_update(user, text):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(effective_message=msg, effective_user=user,
                           effective_chat=SimpleNamespace(id=user.id))


def run(coro):
    return asyncio.run(coro)


# ---------- group gate ----------
def test_gate_unverified_deletes_restricts_registers_and_guides_once(repo):
    ctx = make_context(repo)
    u = tg_user()
    upd = group_update(u)
    with pytest.raises(ApplicationHandlerStop):
        run(group.on_group_message(upd, ctx))
    upd.effective_message.delete.assert_awaited_once()
    chat_id, user_id, perms = ctx.bot.restrict_chat_member.await_args.args
    assert (chat_id, user_id, perms.can_send_messages) == (CHAT, STUDENT, False)
    m = repo.get_member(CHAT, STUDENT)
    assert m["status"] == "pending" and m["deadline_at"] - m["first_seen"] == 24 * 3600
    kwargs = ctx.bot.send_message.await_args.kwargs
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.url == "https://t.me/testbot?start=verify"
    assert f"tg://user?id={STUDENT}" in ctx.bot.send_message.await_args.args[1]
    assert [r["message_id"] for r in repo.due_cleanup(10**12)] == [555]

    # a second message in the same wave: deleted again, but no second guide
    upd2 = group_update(u)
    with pytest.raises(ApplicationHandlerStop):
        run(group.on_group_message(upd2, ctx))
    upd2.effective_message.delete.assert_awaited_once()
    assert ctx.bot.send_message.await_count == 1


def test_gate_verified_user_untouched_without_api_calls(repo):
    repo.add_students(CHAT, [("400111222", None)])
    repo.bind(CHAT, STUDENT, "400111222", None, None, 1)
    repo.set_status(CHAT, STUDENT, "verified", 1)
    ctx = make_context(repo)
    upd = group_update(tg_user())
    run(group.on_group_message(upd, ctx))
    upd.effective_message.delete.assert_not_awaited()
    ctx.bot.get_chat_administrators.assert_not_awaited()


def test_gate_admin_exempt_and_cached(repo):
    ctx = make_context(repo)
    for _ in range(2):
        upd = group_update(tg_user(ADMIN))
        run(group.on_group_message(upd, ctx))
        upd.effective_message.delete.assert_not_awaited()
    assert ctx.bot.get_chat_administrators.await_count == 1  # cached


def test_gate_fails_open_when_admins_unknown(repo):
    bot = make_bot()
    bot.get_chat_administrators.side_effect = BadRequest("boom")
    ctx = make_context(repo, bot=bot)
    upd = group_update(tg_user(ADMIN))
    run(group.on_group_message(upd, ctx))
    upd.effective_message.delete.assert_not_awaited()


def test_gate_anonymous_admin_and_foreign_channel(repo):
    ctx = make_context(repo)
    anon = group_update(User(1087968824, "Group", True), sender_chat=SimpleNamespace(id=CHAT))
    run(group.on_group_message(anon, ctx))
    anon.effective_message.delete.assert_not_awaited()
    foreign = group_update(User(136817688, "Channel", True), sender_chat=SimpleNamespace(id=-5))
    with pytest.raises(ApplicationHandlerStop):
        run(group.on_group_message(foreign, ctx))
    foreign.effective_message.delete.assert_awaited_once()
    ctx.bot.restrict_chat_member.assert_not_awaited()


def test_gate_ignores_other_chats(repo):
    ctx = make_context(repo)
    upd = group_update(tg_user(), chat_id=-999)
    run(group.on_group_message(upd, ctx))
    upd.effective_message.delete.assert_not_awaited()


def test_guide_due():
    sent = {}
    assert group.guide_due(sent, 1, 1000)
    assert not group.guide_due(sent, 1, 1599)
    assert group.guide_due(sent, 1, 1600)


# ---------- chat_member ----------
def restricted(user, is_member=True):
    flags = {k: False for k in (
        "can_change_info", "can_invite_users", "can_pin_messages", "can_send_messages",
        "can_send_polls", "can_send_other_messages", "can_add_web_page_previews",
        "can_manage_topics", "can_send_audios", "can_send_documents", "can_send_photos",
        "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_edit_tag",
        "can_react_to_messages")}
    return ChatMemberRestricted(user, is_member=is_member, until_date=NOW, **flags)


def administrator(user, delete=True, restrict=True):
    return ChatMemberAdministrator(
        user, can_be_edited=False, is_anonymous=False, can_manage_chat=True,
        can_delete_messages=delete, can_manage_video_chats=False, can_restrict_members=restrict,
        can_promote_members=False, can_change_info=False, can_invite_users=True,
        can_post_stories=False, can_edit_stories=False, can_delete_stories=False)


def member_update(old, new, chat_id=CHAT):
    cmu = ChatMemberUpdated(Chat(chat_id, Chat.SUPERGROUP), new.user, NOW, old, new)
    return SimpleNamespace(chat_member=cmu, my_chat_member=cmu)


def test_member_change():
    u = tg_user()
    joined = member_update(ChatMemberLeft(u), ChatMemberMember(u)).chat_member
    assert group.member_change(joined) == (True, False, False)
    left = member_update(restricted(u), ChatMemberLeft(u)).chat_member
    assert group.member_change(left) == (False, True, False)
    kicked = member_update(ChatMemberMember(u), ChatMemberBanned(u, NOW)).chat_member
    assert group.member_change(kicked) == (False, True, False)
    promoted = member_update(ChatMemberMember(u), administrator(u)).chat_member
    assert group.member_change(promoted) == (False, False, True)
    rejoin_restricted = member_update(restricted(u, False), restricted(u, True)).chat_member
    assert group.member_change(rejoin_restricted) == (True, False, False)


def test_new_member_unverified_is_restricted_and_welcomed(repo):
    ctx = make_context(repo)
    u = tg_user()
    run(group.on_chat_member(member_update(ChatMemberLeft(u), ChatMemberMember(u)), ctx))
    assert ctx.bot.restrict_chat_member.await_args.args[2].can_send_messages is False
    assert repo.get_member(CHAT, STUDENT)["status"] == "pending"
    assert ctx.bot.send_message.await_args.kwargs["reply_markup"] is not None
    assert STUDENT in ctx.bot_data[group.GUIDE_SENT_KEY]


def test_new_member_with_binding_stays_verified(repo):
    repo.add_students(CHAT, [("400111222", None)])
    repo.bind(CHAT, STUDENT, "400111222", None, None, 1)
    ctx = make_context(repo)
    u = tg_user()
    run(group.on_chat_member(member_update(ChatMemberLeft(u), ChatMemberMember(u)), ctx))
    ctx.bot.restrict_chat_member.assert_not_awaited()
    ctx.bot.send_message.assert_not_awaited()
    assert repo.get_member(CHAT, STUDENT)["status"] == "verified"


def test_member_left_is_forgotten_binding_kept(repo):
    repo.add_students(CHAT, [("400111222", None)])
    repo.bind(CHAT, STUDENT, "400111222", None, None, 1)
    repo.upsert_pending(CHAT, STUDENT, 0, 100)
    ctx = make_context(repo)
    u = tg_user()
    run(group.on_chat_member(member_update(ChatMemberMember(u), ChatMemberLeft(u)), ctx))
    assert repo.get_member(CHAT, STUDENT) is None
    assert repo.get_binding_by_user(CHAT, STUDENT) is not None


def test_admin_change_invalidates_cache(repo):
    ctx = make_context(repo)
    ctx.bot_data[group.ADMIN_CACHE_KEY] = {CHAT: group.AdminInfo(frozenset(), None, 10**12)}
    u = tg_user()
    run(group.on_chat_member(member_update(ChatMemberMember(u), administrator(u)), ctx))
    assert CHAT not in ctx.bot_data[group.ADMIN_CACHE_KEY]


def test_rights_warning():
    me = User(BOT_ID, "bot", True)
    assert group.rights_warning(administrator(me)) is None
    assert group.rights_warning(ChatMemberOwner(me, is_anonymous=False)) is None
    w = group.rights_warning(administrator(me, delete=False))
    assert texts.RIGHT_DELETE in w and texts.RIGHT_RESTRICT not in w
    assert texts.RIGHT_ADMIN in group.rights_warning(ChatMemberMember(me))
    assert group.rights_warning(ChatMemberLeft(me)) == texts.BOT_REMOVED
    assert group.rights_warning(None) == texts.BOT_REMOVED


def test_my_chat_member_warns_admins(repo):
    ctx = make_context(repo)
    me = User(BOT_ID, "bot", True)
    run(group.on_my_chat_member(member_update(ChatMemberLeft(me), ChatMemberMember(me)), ctx))
    assert ctx.bot.send_message.await_args.args[0] == ADMIN


def test_startup_check(repo):
    bot = make_bot()
    bot.get_chat_member.return_value = administrator(User(BOT_ID, "bot", True), restrict=False)
    app = SimpleNamespace(bot=bot, bot_data={admin.CONFIG_KEY: cfg()})
    run(group.check_bot_rights(app))
    bot.get_chat_member.assert_awaited_once_with(CHAT, BOT_ID)
    assert texts.RIGHT_RESTRICT in bot.send_message.await_args.args[1]


# ---------- private ----------
def test_should_check():
    assert private.should_check("سلام", is_admin=False, awaiting=False)
    assert not private.should_check("سلام", is_admin=True, awaiting=False)
    assert private.should_check("۴۰۰۱۱۱۲۲۲", is_admin=True, awaiting=False)
    assert private.should_check("سلام", is_admin=True, awaiting=True)


def test_start_asks_code_or_says_registered(repo):
    ctx = make_context(repo)
    upd = private_update(tg_user(), "/start verify")
    run(private.cmd_start(upd, ctx))
    assert texts.ASK_CODE in upd.effective_message.reply_text.await_args.args[0]
    assert ctx.user_data[private.AWAIT_CODE]
    repo.add_students(CHAT, [("400111222", None)])
    repo.bind(CHAT, STUDENT, "400111222", None, None, 1)
    upd = private_update(tg_user(), "/start")
    run(private.cmd_start(upd, ctx))
    assert upd.effective_message.reply_text.await_args.args[0] == texts.ALREADY_VERIFIED


def test_private_ok_unrestricts_approves_and_reports(repo):
    repo.add_students(CHAT, [("400111222", "رضایی")])
    ctx = make_context(repo)
    upd = private_update(tg_user(), "۴۰۰۱۱۱۲۲۲")
    run(private.on_private_text(upd, ctx))
    args = ctx.bot.restrict_chat_member.await_args.args
    assert args[:2] == (CHAT, STUDENT) and args[2].can_send_photos is True
    ctx.bot.approve_chat_join_request.assert_awaited_once_with(CHAT, STUDENT)
    reply = upd.effective_message.reply_text.await_args
    assert "ثبت شد" in reply.args[0]
    assert reply.kwargs["reply_markup"].inline_keyboard[0][0].url == "https://t.me/+abc"
    to, report = ctx.bot.send_message.await_args.args
    assert to == ADMIN and "۴۰۰۱۱۱۲۲۲" in report and "@user7" in report and str(STUDENT) in report
    assert repo.get_member(CHAT, STUDENT)["status"] == "verified"


def test_private_ok_when_not_member_and_no_join_request(repo):
    repo.add_students(CHAT, [("400111222", None)])
    bot = make_bot()
    bot.restrict_chat_member.side_effect = BadRequest("user not found")
    bot.approve_chat_join_request.side_effect = BadRequest("HIDE_REQUESTER_MISSING")
    ctx = make_context(repo, bot=bot)
    run(private.on_private_text(private_update(tg_user(), "400111222"), ctx))
    assert repo.get_binding_by_user(CHAT, STUDENT)["code"] == "400111222"


def test_private_default_permissions_fallback(repo):
    repo.add_students(CHAT, [("400111222", None)])
    bot = make_bot()
    bot.get_chat.return_value = SimpleNamespace(permissions=None, username="grp",
                                                invite_link=None, linked_chat_id=None)
    ctx = make_context(repo, bot=bot)
    upd = private_update(tg_user(), "400111222")
    run(private.on_private_text(upd, ctx))
    assert bot.restrict_chat_member.await_args.args[2].can_send_messages is True
    assert upd.effective_message.reply_text.await_args.kwargs["reply_markup"] \
        .inline_keyboard[0][0].url == "https://t.me/grp"


def test_private_not_found_contact_and_lock_notice(repo):
    repo.update_group_setting(CHAT, "max_attempts", 2)
    ctx = make_context(repo)
    upd = private_update(tg_user(), "999999999")
    run(private.on_private_text(upd, ctx))
    assert "@support" in upd.effective_message.reply_text.await_args.args[0]
    ctx.bot.send_message.assert_not_awaited()
    upd = private_update(tg_user(), "999999998")
    run(private.on_private_text(upd, ctx))
    assert "⛔" in upd.effective_message.reply_text.await_args.args[0]
    to, report = ctx.bot.send_message.await_args.args
    assert to == ADMIN and "🔒" in report
    # further attempts: LOCKED, admins are not notified again
    upd = private_update(tg_user(), "999999997")
    run(private.on_private_text(upd, ctx))
    assert upd.effective_message.reply_text.await_args.args[0].startswith("⛔")
    assert ctx.bot.send_message.await_count == 1


def test_private_group_contact_text_overrides(repo):
    repo.update_group_setting(CHAT, "contact_text", "به آموزش پیام بدهید")
    ctx = make_context(repo)
    upd = private_update(tg_user(), "999999999")
    run(private.on_private_text(upd, ctx))
    text = upd.effective_message.reply_text.await_args.args[0]
    assert "به آموزش پیام بدهید" in text and "@support" not in text


def test_private_admin_plain_text_ignored_but_codes_checked(repo):
    ctx = make_context(repo)
    upd = private_update(tg_user(ADMIN), "سلام")
    run(private.on_private_text(upd, ctx))
    upd.effective_message.reply_text.assert_not_awaited()
    upd = private_update(tg_user(ADMIN), "999999999")
    run(private.on_private_text(upd, ctx))
    upd.effective_message.reply_text.assert_awaited_once()


def test_private_has_other_code_message(repo):
    repo.add_students(CHAT, [("400111222", None), ("400111333", None)])
    ctx = make_context(repo)
    run(private.on_private_text(private_update(tg_user(), "400111222"), ctx))
    upd = private_update(tg_user(), "400111333")
    run(private.on_private_text(upd, ctx))
    text = upd.effective_message.reply_text.await_args.args[0]
    assert "۴۰۰۱۱۱۲۲۲" in text and "پشتیبانی" in text


# ---------- join requests ----------
def join_update(user, chat_id=CHAT):
    req = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=user,
                          user_chat_id=user.id, approve=AsyncMock())
    return SimpleNamespace(chat_join_request=req)


def test_join_request_registered_is_approved(repo):
    repo.add_students(CHAT, [("400111222", None)])
    repo.bind(CHAT, STUDENT, "400111222", None, None, 1)
    ctx = make_context(repo)
    upd = join_update(tg_user())
    run(join.on_join_request(upd, ctx))
    upd.chat_join_request.approve.assert_awaited_once()
    ctx.bot.send_message.assert_not_awaited()


def test_join_request_unknown_is_asked_in_private(repo):
    ctx = make_context(repo)
    upd = join_update(tg_user())
    run(join.on_join_request(upd, ctx))
    upd.chat_join_request.approve.assert_not_awaited()
    assert ctx.bot.send_message.await_args.args == (STUDENT, texts.JOIN_ASK_CODE)
    assert repo.get_member(CHAT, STUDENT) is None  # not a member yet: no pending row
    assert ctx.user_data[private.AWAIT_CODE]


def test_join_request_message_failure_is_logged(repo, caplog):
    bot = make_bot()
    bot.send_message.side_effect = Forbidden("bot was blocked")
    ctx = make_context(repo, bot=bot)
    run(join.on_join_request(join_update(tg_user()), ctx))
    assert "could not message" in caplog.text


# ---------- admin revoke in background, wiring ----------
def test_revoke_runs_as_background_task(repo):
    ctx = make_context(repo)
    upd = SimpleNamespace()
    admin._revoke_in_background(upd, ctx, CHAT, [1, 2])
    coro = ctx.application.create_task.call_args.args[0]
    assert ctx.application.create_task.call_args.kwargs["update"] is upd
    coro.close()
    ctx.application.create_task.reset_mock()
    admin._revoke_in_background(upd, ctx, CHAT, [])
    ctx.application.create_task.assert_not_called()


def test_allowed_updates():
    for kind in (Update.MESSAGE, Update.EDITED_MESSAGE, Update.CALLBACK_QUERY,
                 Update.CHAT_MEMBER, Update.MY_CHAT_MEMBER, Update.CHAT_JOIN_REQUEST):
        assert kind in bot_main.ALLOWED_UPDATES


def test_handler_order(tmp_path):
    app = bot_main.build_application(cfg(bot_token="123:abc", db_path=str(tmp_path / "t.db")))
    try:
        gate = [h.callback for h in app.handlers[group.GATE_GROUP]]
        assert gate == [group.on_group_message]
        callbacks = [h.callback for h in app.handlers[0]]
        assert callbacks[-1] is private.on_private_text  # after every admin handler
        assert callbacks.index(admin.on_document) < callbacks.index(private.on_private_text)
        assert [j.name for j in app.job_queue.jobs()] == ["sweeper", "backup"]
    finally:
        app.bot_data[admin.REPO_KEY].conn.close()
