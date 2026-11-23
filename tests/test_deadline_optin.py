"""Removing unregistered members is opt-in: off by default, explained and confirmed before it turns on."""
import asyncio

from bot import texts
from bot.db import MIGRATIONS, connect
from bot.handlers import group, manage
from bot.repo import Repo
from bot.verify import load_settings
from tests.conftest import CHAT
from tests.test_admin_handlers import callback_update
from tests.test_handlers import group_update, make_context, tg_user
from tests.test_manage import admin_update


def run(coro):
    return asyncio.run(coro)


def reply(upd):
    return upd.effective_message.reply_text.await_args


def hours(repo):
    return load_settings(repo, CHAT).deadline_hours


def test_new_group_has_removal_off(repo):
    assert repo.ensure_group(CHAT)["deadline_hours"] == 0
    assert hours(repo) == 0


def test_unregistered_member_gets_no_deadline_by_default(repo):
    ctx = make_context(repo)
    try:
        run(group.on_group_message(group_update(tg_user()), ctx))
    except Exception:  # ApplicationHandlerStop ends the gated update
        pass
    m = repo.get_member(CHAT, tg_user().id)
    assert m["status"] == "pending" and m["deadline_at"] is None


def test_migration_v3_turns_old_24h_default_off(tmp_path):
    path = str(tmp_path / "old.db")
    conn = connect(path)
    # simulate a v2 database: old default (24h) and a pending member with a deadline
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO groups (chat_id, deadline_hours) VALUES (?, 24)", (CHAT,))
    conn.execute("INSERT INTO groups (chat_id, deadline_hours) VALUES (?, 48)", (CHAT - 1,))
    conn.execute("INSERT INTO members (chat_id, user_id, status, first_seen, deadline_at) "
                 "VALUES (?, 1, 'pending', 0, 100)", (CHAT,))
    conn.execute("INSERT INTO members (chat_id, user_id, status, first_seen, deadline_at) "
                 "VALUES (?, 1, 'pending', 0, 100)", (CHAT - 1,))
    conn.commit()
    conn.close()
    repo = Repo(connect(path))
    assert repo.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert repo.get_group(CHAT)["deadline_hours"] == 0
    assert repo.get_member(CHAT, 1)["deadline_at"] is None
    # a value the admin chose on purpose is kept
    assert repo.get_group(CHAT - 1)["deadline_hours"] == 48
    assert repo.get_member(CHAT - 1, 1)["deadline_at"] == 100


def _ask(repo, ctx, text="/set_deadline 24"):
    upd = admin_update(text)
    run(manage.cmd_set_deadline(upd, ctx))
    return upd


def _token(upd):
    markup = reply(upd).kwargs["reply_markup"]
    data = [b.callback_data for b in markup.inline_keyboard[0]]
    assert data[0].startswith("ddl:yes:") and data[1].startswith("ddl:no:")
    return data[0].split(":")[2]


def test_set_deadline_explains_and_waits_for_confirmation(repo):
    repo.ensure_group(CHAT)
    repo.upsert_pending(CHAT, 7, 0, None)
    ctx = make_context(repo)
    upd = _ask(repo, ctx)
    text = reply(upd).args[0]
    assert "حذف می‌شود" in text and "دوباره بیاید" in text and "۲۴ ساعت" in text
    assert "۱ عضوِ ثبت‌نکرده" in text
    assert hours(repo) == 0  # nothing changes before "yes"
    assert repo.get_member(CHAT, 7)["deadline_at"] is None


def test_confirm_yes_turns_removal_on_and_gives_pending_a_deadline(repo):
    repo.ensure_group(CHAT)
    repo.upsert_pending(CHAT, 7, 0, None)
    ctx = make_context(repo)
    token = _token(_ask(repo, ctx))
    q = callback_update(f"ddl:yes:{token}")
    run(manage.on_deadline_choice(q, ctx))
    assert hours(repo) == 24
    assert repo.get_member(CHAT, 7)["deadline_at"] is not None
    assert "۲۴ ساعت" in q.callback_query.edit_message_text.await_args.args[0]
    # the token is single-use
    again = callback_update(f"ddl:yes:{token}")
    run(manage.on_deadline_choice(again, ctx))
    assert again.callback_query.answer.await_args.args[0] == texts.EXPIRED


def test_confirm_no_keeps_removal_off(repo):
    repo.ensure_group(CHAT)
    ctx = make_context(repo)
    token = _token(_ask(repo, ctx))
    q = callback_update(f"ddl:no:{token}")
    run(manage.on_deadline_choice(q, ctx))
    assert hours(repo) == 0
    assert q.callback_query.edit_message_text.await_args.args[0] == texts.DEADLINE_CANCELLED


def test_wrong_token_changes_nothing(repo):
    repo.ensure_group(CHAT)
    ctx = make_context(repo)
    _ask(repo, ctx)
    q = callback_update("ddl:yes:deadbeef")
    run(manage.on_deadline_choice(q, ctx))
    assert hours(repo) == 0
    assert q.callback_query.answer.await_args.args[0] == texts.EXPIRED


def test_turning_off_needs_no_confirmation(repo):
    repo.ensure_group(CHAT)
    repo.update_group_setting(CHAT, "deadline_hours", 24)
    repo.upsert_pending(CHAT, 7, 0, 500)
    ctx = make_context(repo)
    upd = _ask(repo, ctx, "/set_deadline 0")
    assert hours(repo) == 0
    assert repo.get_member(CHAT, 7)["deadline_at"] is None
    assert "reply_markup" not in reply(upd).kwargs or reply(upd).kwargs["reply_markup"] is None


def test_non_admin_cannot_confirm(repo):
    repo.ensure_group(CHAT)
    ctx = make_context(repo)
    token = _token(_ask(repo, ctx))
    q = callback_update(f"ddl:yes:{token}", uid=tg_user().id)
    run(manage.on_deadline_choice(q, ctx))
    assert hours(repo) == 0
