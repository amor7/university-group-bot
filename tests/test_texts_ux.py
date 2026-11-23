"""Support contact everywhere, attempts left, lock time, and RTL-safe HTML help/usage texts."""
import asyncio
import re

from telegram.constants import ParseMode

from bot import texts
from bot.handlers import admin, manage, private
from tests.conftest import CHAT
from tests.test_handlers import make_context, tg_user
from tests.test_manage import admin_update, private_update


def run(coro):
    return asyncio.run(coro)


def last_reply(upd):
    return upd.effective_message.reply_text.await_args


# ---------- support contact / attempts / lock ----------
def test_bound_and_other_code_use_contact():
    assert "@help" in texts.bound_to_other(None, "@help")
    assert texts.DEFAULT_CONTACT_WHO in texts.bound_to_other(None, None)
    custom = texts.bound_to_other("به آموزش پیام بدهید", "@help")
    assert "به آموزش پیام بدهید" in custom and "@help" not in custom
    assert "@help" in texts.has_other_code("400111222", None, "@help")
    assert "به آموزش" in texts.has_other_code("400111222", "به آموزش", None)


def test_bound_to_other_reply_has_contact_and_attempts_left(repo):
    repo.add_students(CHAT, [("400111222", None)])
    repo.bind(CHAT, 99, "400111222", None, None, 1)
    ctx = make_context(repo)
    upd = private_update(tg_user(), "400111222")
    run(private.on_private_text(upd, ctx))
    text = last_reply(upd).args[0]
    assert "@support" in text and "۴ تلاش دیگر باقی مانده" in text


def test_attempts_left_counts_down_then_lock_with_time(repo):
    repo.update_group_setting(CHAT, "max_attempts", 3)
    ctx = make_context(repo)
    replies = []
    for code in ("999999991", "999999992", "999999993"):
        upd = private_update(tg_user(), code)
        run(private.on_private_text(upd, ctx))
        replies.append(last_reply(upd).args[0])
    assert "۲ تلاش دیگر" in replies[0] and "۱ تلاش دیگر" in replies[1]
    assert "تلاش دیگر" not in replies[2] and "⛔" in replies[2]
    assert "به وقت تهران" in replies[2]  # says when to try again


def test_invalid_format_explains_digits_with_example(repo):
    ctx = make_context(repo)
    upd = private_update(tg_user(), "abc")
    run(private.on_private_text(upd, ctx))
    text = last_reply(upd).args[0]
    assert text == texts.INVALID_FORMAT and "رقم" in text and "۱۲۳۴۵۶۷۸۹" in text
    assert "تلاش دیگر" not in text  # not counted


def test_locked_text():
    assert "به وقت تهران" not in texts.locked(60)
    text = texts.locked(90, 1_700_000_000)
    assert "۲ دقیقه" in text and "۲۰۲۳-۱۱-۱۵" in text


# ---------- HTML / RTL ----------
USAGES = [texts.ADD_USAGE, texts.REMOVE_USAGE, texts.UNBIND_USAGE, texts.WHOIS_USAGE,
          texts.UNLOCK_USAGE, texts.VERIFY_USER_USAGE, texts.SET_CONTACT_USAGE,
          texts.SET_DEADLINE_USAGE, texts.SET_ATTEMPTS_USAGE, texts.SET_REPORT_USAGE,
          texts.ADD_ADMIN_USAGE, texts.REMOVE_ADMIN_USAGE, texts.LOGS_USAGE, texts.HELP_ADMIN]


def test_no_line_starts_with_a_command_and_html_is_clean():
    for text in USAGES:
        for line in text.splitlines():
            assert not line.lstrip("• ").startswith("/"), line
        bare = re.sub(r"</?code>", "", text)
        assert "<" not in bare and ">" not in bare, text  # every < > is escaped
        assert text.count("<code>") == text.count("</code>")


def test_help_admin_sent_as_html(repo):
    ctx = make_context(repo)
    upd = admin_update("/help")
    run(manage.cmd_help(upd, ctx))
    assert last_reply(upd).kwargs["parse_mode"] == ParseMode.HTML
    assert "<code>/unbind &lt;کد|@username|user_id&gt;</code>" in last_reply(upd).args[0]
    upd = private_update(tg_user(), "/help")
    run(manage.cmd_help(upd, ctx))
    assert last_reply(upd).kwargs["parse_mode"] is None


def test_usage_and_usage_errors_sent_as_html(repo):
    ctx = make_context(repo)
    upd = admin_update("/whois")
    run(manage.cmd_whois(upd, ctx))
    assert last_reply(upd).kwargs["parse_mode"] == ParseMode.HTML
    upd = admin_update("/set_attempts 99")
    run(manage.cmd_set_attempts(upd, ctx))
    assert last_reply(upd).args[0] == texts.SET_ATTEMPTS_USAGE
    assert last_reply(upd).kwargs["parse_mode"] == ParseMode.HTML
    upd = admin_update("/add")
    run(admin.cmd_add(upd, ctx))
    assert last_reply(upd).kwargs["parse_mode"] == ParseMode.HTML


def test_contact_preview_is_escaped(repo):
    repo.update_group_setting(CHAT, "contact_text", "<b>x</b> & y")
    ctx = make_context(repo)
    upd = admin_update("/set_contact")
    run(manage.cmd_set_contact(upd, ctx))
    text = last_reply(upd).args[0]
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text
    assert last_reply(upd).kwargs["parse_mode"] == ParseMode.HTML
