import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter

from bot import jobs
from bot.config import Config
from bot.handlers.admin import CONFIG_KEY, REPO_KEY
from bot.jobs import cleanup_due, kick_due, pick_kicks, retry_unbans, should_kick
from tests.conftest import CHAT


@pytest.fixture(autouse=True)
def _removal_on(repo):
    """Automatic removal is off by default; these tests are about the sweeper with it on."""
    repo.ensure_group(CHAT)
    repo.update_group_setting(CHAT, "deadline_hours", 24)


def test_should_kick():
    assert should_kick({"status": "pending", "deadline_at": 100}, False, 100)
    assert not should_kick({"status": "pending", "deadline_at": 101}, False, 100)
    assert not should_kick({"status": "pending", "deadline_at": 100}, True, 100)
    assert not should_kick({"status": "verified", "deadline_at": 50}, False, 100)
    assert not should_kick({"status": "revoked", "deadline_at": None}, False, 100)
    assert not should_kick(None, False, 100)


def test_pick_kicks_only_expired_pending(repo):
    repo.add_students(CHAT, [("400111222", None)])
    repo.upsert_pending(CHAT, 1, 0, 100)    # expired
    repo.upsert_pending(CHAT, 2, 0, 1000)   # not yet
    repo.set_status(CHAT, 3, "verified", 0)
    repo.set_status(CHAT, 4, "revoked", 0)
    repo.upsert_pending(CHAT, 5, 0, 100)    # expired, but bound meanwhile
    repo.bind(CHAT, 5, "400111222", None, None, 50)
    assert pick_kicks(repo, 200) == [(CHAT, 1)]


def test_kick_due_bans_unbans_and_forgets(repo):
    repo.upsert_pending(CHAT, 1, 0, 100)
    bot = AsyncMock()
    assert asyncio.run(kick_due(bot, repo, 200)) == 1
    bot.ban_chat_member.assert_awaited_once_with(CHAT, 1)
    bot.unban_chat_member.assert_awaited_once_with(CHAT, 1, only_if_banned=True)
    assert repo.get_member(CHAT, 1) is None


def test_kick_due_errors(repo):
    repo.upsert_pending(CHAT, 1, 0, 100)
    repo.upsert_pending(CHAT, 2, 0, 100)
    bot = AsyncMock()
    bot.ban_chat_member.side_effect = [BadRequest("user not found"), NetworkError("down")]
    assert asyncio.run(kick_due(bot, repo, 200)) == 0
    assert repo.get_member(CHAT, 1) is None          # permanent error: forgotten
    assert repo.get_member(CHAT, 2) is not None      # transient: retried next round
    bot.unban_chat_member.assert_not_awaited()


def test_cleanup_due(repo):
    for mid, at in ((10, 100), (11, 100), (12, 100), (13, 999)):
        repo.add_cleanup(CHAT, mid, at)
    bot = AsyncMock()
    bot.delete_message.side_effect = [True, BadRequest("message to delete not found"),
                                      NetworkError("down")]
    assert asyncio.run(cleanup_due(bot, repo, 200)) == 1
    assert [r["message_id"] for r in repo.due_cleanup(10**9)] == [12, 13]


# ---------- rate limits, unban retries, races ----------
@pytest.fixture
def sleeps(monkeypatch):
    calls = []

    async def fake_sleep(s):
        calls.append(s)

    monkeypatch.setattr(jobs.asyncio, "sleep", fake_sleep)
    return calls


def test_kick_due_gap_and_cap_per_round(repo, sleeps):
    for uid in range(1, 61):
        repo.upsert_pending(CHAT, uid, 0, 100)
    bot = AsyncMock()
    assert asyncio.run(kick_due(bot, repo, 200)) == jobs.MAX_KICKS_PER_ROUND == 50
    assert bot.ban_chat_member.await_count == 50
    assert sleeps == [jobs.KICK_GAP] * 49 and jobs.KICK_GAP == 0.2
    assert len(repo.due_for_kick(200)) == 10  # the rest wait for the next round


def test_kick_due_retry_after_waits_and_ends_round(repo, sleeps):
    for uid in (1, 2, 3):
        repo.upsert_pending(CHAT, uid, 0, 100)
    bot = AsyncMock()
    bot.ban_chat_member.side_effect = RetryAfter(7)
    assert asyncio.run(kick_due(bot, repo, 200)) == 0
    assert bot.ban_chat_member.await_count == 1
    assert sleeps == [7.0]
    assert len(repo.due_for_kick(200)) == 3


def test_failed_unban_is_retried_then_marked_and_only_unban_next_round(repo, sleeps):
    repo.upsert_pending(CHAT, 1, 0, 100)
    bot = AsyncMock()
    bot.unban_chat_member.side_effect = NetworkError("down")
    assert asyncio.run(kick_due(bot, repo, 200)) == 1
    assert bot.unban_chat_member.await_count == 1 + jobs.UNBAN_RETRIES
    row = repo.get_member(CHAT, 1)
    assert row is not None and row["unban_pending"] == 1     # kept, marked
    assert pick_kicks(repo, 300) == []                        # not banned again
    bot.reset_mock()
    bot.unban_chat_member.side_effect = None
    assert asyncio.run(retry_unbans(bot, repo, 300)) == 1
    bot.ban_chat_member.assert_not_awaited()
    bot.unban_chat_member.assert_awaited_once_with(CHAT, 1, only_if_banned=True)
    assert repo.get_member(CHAT, 1) is None


def test_retry_unbans_keeps_flag_on_failure(repo, sleeps):
    repo.upsert_pending(CHAT, 1, 0, 100)
    repo.set_unban_pending(CHAT, 1, True)
    bot = AsyncMock()
    bot.unban_chat_member.side_effect = BadRequest("boom")
    assert asyncio.run(retry_unbans(bot, repo, 300)) == 0
    assert repo.get_member(CHAT, 1)["unban_pending"] == 1


def test_verified_during_kick_keeps_row(repo, sleeps):
    repo.add_students(CHAT, [("400111222", None)])
    repo.upsert_pending(CHAT, 1, 0, 100)
    bot = AsyncMock()

    async def verify_meanwhile(chat_id, user_id):
        repo.bind_and_verify(CHAT, 1, "400111222", None, None, 150)

    bot.ban_chat_member.side_effect = verify_meanwhile
    asyncio.run(kick_due(bot, repo, 200))
    assert repo.get_member(CHAT, 1)["status"] == "verified"


def test_cleanup_due_retry_after(repo, sleeps):
    for mid in (10, 11):
        repo.add_cleanup(CHAT, mid, 100)
    bot = AsyncMock()
    bot.delete_message.side_effect = RetryAfter(5)
    assert asyncio.run(cleanup_due(bot, repo, 200)) == 0
    assert bot.delete_message.await_count == 1 and sleeps == [5.0]
    assert len(repo.due_cleanup(200)) == 2


def test_sweep_step_failure_does_not_stop_next_steps(repo, monkeypatch):
    ran = []

    async def boom(bot, repo_, at):
        ran.append("kick")
        raise RuntimeError("bad")

    def ok(name):
        async def step(bot, repo_, at):
            ran.append(name)
        return step

    monkeypatch.setattr(jobs, "retry_unbans", ok("unban"))
    monkeypatch.setattr(jobs, "kick_due", boom)
    monkeypatch.setattr(jobs, "cleanup_due", ok("cleanup"))
    ctx = SimpleNamespace(bot=AsyncMock(), bot_data={REPO_KEY: repo})
    asyncio.run(jobs.sweep(ctx))
    assert ran == ["unban", "kick", "cleanup"]


def test_backup_job_writes_backup(repo, tmp_path):
    config = Config(bot_token="x", admin_ids=frozenset(), db_path=str(tmp_path / "bot.db"),
                    group_id=CHAT)
    ctx = SimpleNamespace(bot_data={REPO_KEY: repo, CONFIG_KEY: config})
    asyncio.run(jobs.backup_job(ctx))
    assert len(list((tmp_path / "backups").glob("bot-*.db"))) == 1
