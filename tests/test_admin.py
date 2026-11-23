import asyncio
from types import SimpleNamespace

from telegram.error import BadRequest

from bot import actions, texts
from bot.config import Config
from bot.handlers.admin import add_rows, build_preview, is_bot_admin, remove_codes, split_codes
from bot.importer import parse_codes_text
from tests.conftest import CHAT


def cfg(admin_ids=(1,), group_id=CHAT):
    return Config(bot_token="x", admin_ids=frozenset(admin_ids), db_path=":memory:",
                  group_id=group_id)


def test_is_bot_admin(repo):
    config = cfg(admin_ids=(1,))
    repo.add_admin(2, added_by=1, now=0)
    assert is_bot_admin(config, repo, 1)  # from .env
    assert is_bot_admin(config, repo, 2)  # from DB
    assert not is_bot_admin(config, repo, 3)
    assert not is_bot_admin(config, repo, None)


def test_fa_num_and_list():
    assert texts.fa_num(1405) == "۱۴۰۵"
    assert texts.fa_num("کد 12") == "کد ۱۲"
    assert texts.fa_list(["a", "b", "c"], limit=2) == "a، b … و ۱ مورد دیگر"


def test_add_and_remove_flow(repo):
    repo.add_students(CHAT, [("500000001", None)])
    added, existing = add_rows(repo, CHAT, parse_codes_text("500000001 500000002\n500000003 نام").rows)
    assert added == ["500000002", "500000003"] and existing == ["500000001"]
    assert repo.find_student(CHAT, "500000003")["full_name"] == "نام"

    repo.bind(CHAT, 9, "500000002", "user9", None, now=1)
    removed, missing, bindings = remove_codes(
        repo, CHAT, split_codes("۵۰۰۰۰۰۰۰۲, 500000002 999999999\n500000003"))
    assert removed == ["500000002", "500000003"] and missing == ["999999999"]
    assert [(b["user_id"], b["username"]) for b in bindings] == [(9, "user9")]
    report = texts.remove_report(removed, missing, bindings)
    assert "@user9" in report and "۲" in report


def test_build_preview(repo):
    repo.add_students(CHAT, [("500000001", None), ("500000009", None)])
    repo.bind(CHAT, 5, "500000009", None, None, now=1)
    result = parse_codes_text("500000001 500000002 50000000003 500000002 12")
    text = build_preview(repo, CHAT, result, "f.csv")
    assert "کد معتبر: ۳" in text
    assert "جدید نسبت به لیست فعلی: ۲" in text
    assert "تکراری داخل فایل: ۱" in text
    assert "ردیف خراب: ۱" in text
    assert "۹رقمی" in text and "۱۱رقمی" in text
    assert "۱ کد از لیست حذف می‌شود و ۱ نفر" in text


class FakeBot:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="restricted")

    async def restrict_chat_member(self, chat_id, user_id, permissions):
        self.calls.append((chat_id, user_id, permissions.can_send_messages))
        if user_id in self.fail_for:
            raise BadRequest("user not found")


def test_revoke_users_restricts_and_swallows_errors(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(actions.asyncio, "sleep", fake_sleep)
    bot = FakeBot(fail_for={2})
    ok = asyncio.run(actions.revoke_users(bot, CHAT, [1, 2, 3]))
    assert ok == 2
    assert bot.calls == [(CHAT, 1, False), (CHAT, 2, False), (CHAT, 3, False)]
    assert sleeps == [0.05] * 3
