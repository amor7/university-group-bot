"""admin.py handlers (file import, /add, /remove, /clear_list, /export, /backup) with the
AsyncMock fake bot; plus: a locked user can't get in even with the right code."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType

from bot import texts
from bot.handlers import admin, private
from tests.conftest import CHAT
from tests.test_handlers import ADMIN, STUDENT, cfg, make_context, tg_user
from tests.test_manage import admin_update, last_log, private_update, reply_of


def run(coro):
    return asyncio.run(coro)


CSV = b"student_id,name\n500000001,Ali\n500000002,Sara\n"


@pytest.fixture
def ctx(repo, tmp_path):
    # db_path decides where backups go: keep them in tmp_path
    return make_context(repo, config=cfg(db_path=str(tmp_path / "bot.db")))


def doc_update(filename, data=CSV, size=None, uid=ADMIN):
    upd = admin_update("", uid=uid)
    tg_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(data)))
    upd.effective_message.document = SimpleNamespace(
        file_name=filename, file_size=len(data) if size is None else size,
        get_file=AsyncMock(return_value=tg_file))
    return upd


def callback_update(data, uid=ADMIN):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            edit_message_reply_markup=AsyncMock())
    return SimpleNamespace(callback_query=query, effective_user=tg_user(uid),
                           effective_chat=SimpleNamespace(id=uid, type=ChatType.PRIVATE),
                           effective_message=None)


def buttons(upd):
    markup = upd.effective_message.reply_text.await_args.kwargs["reply_markup"]
    return [b.callback_data for b in markup.inline_keyboard[0]]


def backups(tmp_path):
    return list((tmp_path / "backups").glob("bot-*.db"))


# ---------- import ----------
def test_import_replace_backs_up_revokes_and_logs(repo, ctx, tmp_path):
    repo.add_students(CHAT, [("500000009", None)])
    repo.bind(CHAT, STUDENT, "500000009", "user7", None, 1)
    upd = doc_update("list.csv")
    run(admin.on_document(upd, ctx))
    data = buttons(upd)
    assert data[1].startswith("imp:replace:")
    q = callback_update(data[1])
    run(admin.on_callback(q, ctx))
    assert len(backups(tmp_path)) == 1
    assert repo.list_codes(CHAT) == {"500000001", "500000002"}
    assert repo.get_binding_by_user(CHAT, STUDENT) is None
    coro = ctx.application.create_task.call_args.args[0]  # background revoke
    assert coro.cr_frame.f_locals["user_ids"] == [STUDENT]
    coro.close()
    action, details = last_log(repo)
    assert action == "import_replace" and details["revoked"] == [STUDENT]
    assert details["backup"] == backups(tmp_path)[0].name
    assert "جایگزین" in q.callback_query.edit_message_text.await_args.args[0]


def test_import_wrong_or_reused_token_changes_nothing(repo, ctx, tmp_path):
    repo.add_students(CHAT, [("500000009", None)])
    upd = doc_update("list.csv")
    run(admin.on_document(upd, ctx))
    good = buttons(upd)[1]
    bad = callback_update("imp:replace:deadbeef")
    run(admin.on_callback(bad, ctx))
    assert bad.callback_query.answer.await_args.args[0] == texts.EXPIRED
    assert repo.list_codes(CHAT) == {"500000009"} and backups(tmp_path) == []
    run(admin.on_callback(callback_update(good), ctx))  # the real one still works
    assert repo.list_codes(CHAT) == {"500000001", "500000002"}
    ctx.application.create_task.reset_mock()
    repo.add_students(CHAT, [("500000003", None)])
    again = callback_update(good)                        # reused token
    run(admin.on_callback(again, ctx))
    assert again.callback_query.answer.await_args.args[0] == texts.EXPIRED
    assert "500000003" in repo.list_codes(CHAT) and len(backups(tmp_path)) == 1
    ctx.application.create_task.assert_not_called()


def test_import_add(repo, ctx):
    repo.add_students(CHAT, [("500000001", None)])
    upd = doc_update("list.csv")
    run(admin.on_document(upd, ctx))
    run(admin.on_callback(callback_update(buttons(upd)[0]), ctx))
    assert repo.list_codes(CHAT) == {"500000001", "500000002"}
    assert last_log(repo) == ("import_add", {"file": "list.csv", "added": 1, "existing": 1})


@pytest.mark.parametrize("name,data,size,expected", [
    ("list.pdf", CSV, None, texts.IMPORT_BAD_TYPE),
    ("list.csv", CSV, admin.MAX_FILE_MB * 1024 * 1024 + 1, None),
    ("list.csv", b"student_id,name\n", None, texts.IMPORT_NO_VALID),
])
def test_import_rejects_bad_files(repo, ctx, name, data, size, expected):
    upd = doc_update(name, data, size)
    run(admin.on_document(upd, ctx))
    text = reply_of(upd)
    if expected is None:
        assert "خیلی بزرگ" in text
        upd.effective_message.document.get_file.assert_not_awaited()
    else:
        assert text == expected
    assert admin.PENDING_IMPORT not in ctx.user_data
    assert repo.list_codes(CHAT) == set()


def test_import_refused_for_non_admin(repo, ctx):
    upd = doc_update("list.csv", uid=STUDENT)
    run(admin.on_document(upd, ctx))
    assert reply_of(upd) == texts.NO_ACCESS


# ---------- /clear_list ----------
def test_clear_list_no_then_yes(repo, ctx, tmp_path):
    repo.add_students(CHAT, [("500000001", None), ("500000002", None)])
    repo.bind(CHAT, STUDENT, "500000001", None, None, 1)
    upd = admin_update("/clear_list")
    run(admin.cmd_clear_list(upd, ctx))
    yes, no = buttons(upd)
    q = callback_update(no)
    run(admin.on_callback(q, ctx))
    assert q.callback_query.edit_message_text.await_args.args[0] == texts.CANCELLED
    assert len(repo.list_codes(CHAT)) == 2 and backups(tmp_path) == []
    assert last_log(repo)[0] == "clear_cancel"
    run(admin.on_callback(callback_update(yes), ctx))  # token was used by "no"
    assert len(repo.list_codes(CHAT)) == 2

    upd = admin_update("/clear_list")
    run(admin.cmd_clear_list(upd, ctx))
    run(admin.on_callback(callback_update(buttons(upd)[0]), ctx))
    assert repo.list_codes(CHAT) == set() and len(backups(tmp_path)) == 1
    action, details = last_log(repo)
    assert action == "clear_list" and details["count"] == 2 and details["revoked"] == [STUDENT]
    ctx.application.create_task.call_args.args[0].close()


def test_clear_list_empty(repo, ctx):
    upd = admin_update("/clear_list")
    run(admin.cmd_clear_list(upd, ctx))
    assert reply_of(upd) == texts.CLEAR_EMPTY


# ---------- /add, /remove ----------
def test_cmd_add(repo, ctx):
    repo.add_students(CHAT, [("500000001", None)])
    upd = admin_update("/add 500000001 500000002\n500000003 علی رضایی\n12")
    run(admin.cmd_add(upd, ctx))
    assert repo.list_codes(CHAT) == {"500000001", "500000002", "500000003"}
    assert repo.find_student(CHAT, "500000003")["full_name"] == "علی رضایی"
    assert "اضافه شد: ۲" in reply_of(upd) and "نامعتبر" in reply_of(upd)
    action, details = last_log(repo)
    assert action == "add" and details["added"] == ["500000002", "500000003"]
    upd = admin_update("/add")
    run(admin.cmd_add(upd, ctx))
    assert reply_of(upd) == texts.ADD_USAGE


def test_cmd_remove_revokes_in_background(repo, ctx):
    repo.add_students(CHAT, [("500000001", None), ("500000002", None)])
    repo.bind(CHAT, STUDENT, "500000001", "user7", None, 1)
    upd = admin_update("/remove 500000001 999999999")
    run(admin.cmd_remove(upd, ctx))
    assert repo.list_codes(CHAT) == {"500000002"}
    assert "@user7" in reply_of(upd)
    assert last_log(repo) == ("remove", {"removed": ["500000001"], "missing": ["999999999"],
                                         "revoked": [STUDENT]})
    ctx.application.create_task.call_args.args[0].close()
    upd = admin_update("/remove")
    run(admin.cmd_remove(upd, ctx))
    assert reply_of(upd) == texts.REMOVE_USAGE


# ---------- /export, /backup ----------
def test_cmd_export_sends_xlsx(repo, ctx):
    repo.add_students(CHAT, [("500000001", "Ali")])
    upd = admin_update("/export")
    upd.effective_message.reply_document = AsyncMock()
    run(admin.cmd_export(upd, ctx))
    kwargs = upd.effective_message.reply_document.await_args.kwargs
    assert kwargs["filename"].endswith(".xlsx") and kwargs["document"][:2] == b"PK"
    assert "۱" in kwargs["caption"]
    assert last_log(repo) == ("export", {"count": 1})


def test_cmd_backup_sends_db_file(repo, ctx, tmp_path):
    upd = admin_update("/backup")
    sent = {}

    async def reply_document(document, filename, caption):
        sent.update(head=document.read(16), filename=filename)

    upd.effective_message.reply_document = reply_document
    run(admin.cmd_backup(upd, ctx))
    assert sent["head"] == b"SQLite format 3\x00"
    assert sent["filename"] == backups(tmp_path)[0].name
    assert last_log(repo)[0] == "backup"
    assert json.loads(repo.recent_admin_log(1)[0]["details_json"])["file"] == sent["filename"]


# ---------- lock beats the right code ----------
def test_locked_user_cannot_get_in_with_right_code(repo):
    repo.add_students(CHAT, [("400111222", None)])
    repo.update_group_setting(CHAT, "max_attempts", 1)
    ctx = make_context(repo)
    run(private.on_private_text(private_update(tg_user(), "999999999"), ctx))  # locks
    upd = private_update(tg_user(), "400111222")
    run(private.on_private_text(upd, ctx))
    assert reply_of(upd).startswith("⛔")
    assert repo.get_binding_by_user(CHAT, STUDENT) is None
    ctx.bot.restrict_chat_member.assert_not_awaited()
    ctx.bot.approve_chat_join_request.assert_not_awaited()
