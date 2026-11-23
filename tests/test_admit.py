"""admit(): never lift a ban; act only on restricted/member users; rights errors reach admins."""
import asyncio
from types import SimpleNamespace

from telegram.error import BadRequest, Forbidden, RetryAfter

from bot import actions, texts
from bot.actions import RESTRICTED_KEY
from bot.handlers import private
from bot.handlers.private import AdmitStatus
from tests.conftest import CHAT
from tests.test_handlers import ADMIN, STUDENT, make_context, private_update, tg_user

CODE = "400111222"


def run(coro):
    return asyncio.run(coro)


def status(s):
    return SimpleNamespace(status=s, user=None)


def listed(repo):
    repo.add_students(CHAT, [(CODE, None)])


def reply(upd):
    return upd.effective_message.reply_text.await_args


def test_kicked_user_is_not_unbanned_and_told_to_contact_support(repo):
    listed(repo)
    ctx = make_context(repo)
    ctx.bot.get_chat_member.return_value = status("kicked")
    upd = private_update(tg_user(), CODE)
    run(private.on_private_text(upd, ctx))
    ctx.bot.restrict_chat_member.assert_not_awaited()
    ctx.bot.approve_chat_join_request.assert_not_awaited()
    text = reply(upd).args[0]
    assert "بن" in text and "@support" in text and "حالا می‌توانید" not in text
    assert repo.get_binding_by_user(CHAT, STUDENT)["code"] == CODE  # binding is still saved


def test_left_user_only_join_request_is_approved(repo):
    listed(repo)
    ctx = make_context(repo)
    ctx.bot.get_chat_member.return_value = status("left")
    run(private.on_private_text(private_update(tg_user(), CODE), ctx))
    ctx.bot.restrict_chat_member.assert_not_awaited()
    ctx.bot.approve_chat_join_request.assert_awaited_once_with(CHAT, STUDENT)
    assert repo.get_binding_by_user(CHAT, STUDENT) is not None


def test_user_not_found_is_treated_as_left(repo):
    ctx = make_context(repo)
    ctx.bot.get_chat_member.side_effect = BadRequest("User not found")
    result = run(private.admit(ctx, CHAT, STUDENT))
    assert result.status == AdmitStatus.OK and result.link == "https://t.me/+abc"
    ctx.bot.restrict_chat_member.assert_not_awaited()
    ctx.bot.approve_chat_join_request.assert_awaited_once()


def test_plain_member_needs_nothing(repo):
    ctx = make_context(repo)
    ctx.bot.get_chat_member.return_value = status("member")
    assert run(private.admit(ctx, CHAT, STUDENT)).status == AdmitStatus.OK
    ctx.bot.restrict_chat_member.assert_not_awaited()
    ctx.bot.approve_chat_join_request.assert_not_awaited()


def test_restricted_user_is_unrestricted_and_restrict_cache_cleared(repo):
    ctx = make_context(repo)
    ctx.bot_data[RESTRICTED_KEY] = {(CHAT, STUDENT): 1}
    assert run(private.admit(ctx, CHAT, STUDENT)).status == AdmitStatus.OK
    assert ctx.bot.restrict_chat_member.await_args.args[2].can_send_messages is True
    assert (CHAT, STUDENT) not in ctx.bot_data[RESTRICTED_KEY]


def test_get_chat_failure_does_not_unrestrict_with_basic_permissions(repo):
    listed(repo)
    ctx = make_context(repo)
    ctx.bot.get_chat.side_effect = BadRequest("chat not found")
    upd = private_update(tg_user(), CODE)
    run(private.on_private_text(upd, ctx))
    ctx.bot.restrict_chat_member.assert_not_awaited()
    assert texts.ADMIT_RETRY in reply(upd).args[0] and "/start" in reply(upd).args[0]
    assert repo.get_binding_by_user(CHAT, STUDENT) is not None


def test_not_enough_rights_notifies_admins(repo):
    ctx = make_context(repo)
    ctx.bot.restrict_chat_member.side_effect = BadRequest(
        "Not enough rights to restrict/unrestrict chat member")
    assert run(private.admit(ctx, CHAT, STUDENT)).status == AdmitStatus.RETRY
    to, text = ctx.bot.send_message.await_args.args
    assert to == ADMIN and texts.RIGHT_RESTRICT in text


def test_not_participant_on_restrict_is_quiet(repo):
    ctx = make_context(repo)
    ctx.bot.restrict_chat_member.side_effect = BadRequest("User_not_participant")
    assert run(private.admit(ctx, CHAT, STUDENT)).status == AdmitStatus.OK
    ctx.bot.send_message.assert_not_awaited()


def test_start_with_binding_unrestricts_old_member(repo):
    listed(repo)
    repo.bind(CHAT, STUDENT, CODE, None, None, 1)
    ctx = make_context(repo)
    upd = private_update(tg_user(), "/start")
    run(private.cmd_start(upd, ctx))
    assert ctx.bot.restrict_chat_member.await_args.args[2].can_send_photos is True
    assert reply(upd).args[0] == texts.ALREADY_VERIFIED
    assert reply(upd).kwargs["reply_markup"].inline_keyboard[0][0].url == "https://t.me/+abc"


def test_start_with_binding_but_banned(repo):
    listed(repo)
    repo.bind(CHAT, STUDENT, CODE, None, None, 1)
    ctx = make_context(repo)
    ctx.bot.get_chat_member.return_value = status("kicked")
    upd = private_update(tg_user(), "/start")
    run(private.cmd_start(upd, ctx))
    ctx.bot.restrict_chat_member.assert_not_awaited()
    assert "@support" in reply(upd).args[0]


# ---------- revoke_users ----------
class RevokeBot:
    def __init__(self, statuses, restrict_errors=None):
        self.statuses = statuses
        self.restrict_errors = list(restrict_errors or [])
        self.restricted = []
        self.sent = []

    async def get_chat_member(self, chat_id, user_id):
        return status(self.statuses[user_id])

    async def restrict_chat_member(self, chat_id, user_id, permissions):
        if self.restrict_errors:
            err = self.restrict_errors.pop(0)
            if err is not None:
                raise err
        self.restricted.append(user_id)

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def _no_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(actions.asyncio, "sleep", fake_sleep)
    return sleeps


def test_revoke_users_skips_banned_and_left(monkeypatch):
    _no_sleep(monkeypatch)
    bot = RevokeBot({1: "member", 2: "kicked", 3: "left", 4: "restricted"})
    assert run(actions.revoke_users(bot, CHAT, [1, 2, 3, 4])) == 2
    assert bot.restricted == [1, 4]


def test_revoke_users_rights_error_notifies_and_stops(monkeypatch):
    _no_sleep(monkeypatch)
    bot = RevokeBot({1: "member", 2: "member"},
                    restrict_errors=[BadRequest("Not enough rights to restrict")])
    assert run(actions.revoke_users(bot, CHAT, [1, 2], admin_ids=[ADMIN])) == 0
    assert bot.restricted == []  # user 2 not even tried
    assert bot.sent[0][0] == ADMIN and texts.RIGHT_RESTRICT in bot.sent[0][1]


def test_revoke_users_waits_on_retry_after(monkeypatch):
    sleeps = _no_sleep(monkeypatch)
    bot = RevokeBot({1: "member"}, restrict_errors=[RetryAfter(3), None])
    assert run(actions.revoke_users(bot, CHAT, [1])) == 1
    assert sleeps[0] == 3.0 and bot.restricted == [1]


def test_error_classification():
    assert actions.is_rights_error(BadRequest("Not enough rights to restrict/unrestrict"))
    assert actions.is_rights_error(Forbidden("bot was kicked from the supergroup chat"))
    assert not actions.is_rights_error(BadRequest("User not found"))
    assert actions.is_not_member_error(BadRequest("User not found"))
    assert actions.is_not_member_error(BadRequest("USER_NOT_PARTICIPANT"))
    assert not actions.is_not_member_error(BadRequest("Not enough rights"))
    assert actions.retry_seconds(RetryAfter(12)) == 12.0
