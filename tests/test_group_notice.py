"""Per-group throttle for guide/welcome messages and the restrict TTL."""
import asyncio
from itertools import count
from types import SimpleNamespace

import pytest
from telegram import ChatMemberLeft, ChatMemberMember
from telegram.error import NetworkError
from telegram.ext import ApplicationHandlerStop

from bot.actions import RESTRICTED_KEY
from bot.handlers import group
from tests.conftest import CHAT
from tests.test_handlers import group_update, make_context, member_update, tg_user


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clock(monkeypatch):
    t = SimpleNamespace(now=1_000_000)
    monkeypatch.setattr(group, "now", lambda: t.now)
    return t


def gated(ctx, user):
    upd = group_update(user)
    with pytest.raises(ApplicationHandlerStop):
        run(group.on_group_message(upd, ctx))
    return upd


def with_message_ids(ctx):
    ids = count(500)
    ctx.bot.send_message.side_effect = lambda *a, **k: SimpleNamespace(message_id=next(ids))


def test_take_notice():
    st = group.NoticeState()
    assert group.take_notice(st, "a", 100) == ["a"]
    assert group.take_notice(st, "b", 105) is None      # throttled: queued
    assert group.take_notice(st, "b", 110) is None      # no duplicate
    assert group.take_notice(st, "c", 120) == ["b", "c"]
    for i, m in enumerate("defghij"):
        group.take_notice(st, m, 125 + i)
    assert group.take_notice(st, "k", 141) == ["d", "e", "f", "g", "h"]  # at most 5


def test_one_guide_per_group_per_20s_mentions_collected(repo, clock):
    ctx = make_context(repo)
    with_message_ids(ctx)
    gated(ctx, tg_user(7))
    gated(ctx, tg_user(8))                      # same 20 s: no message, mention queued
    assert ctx.bot.send_message.await_count == 1
    clock.now += 20
    gated(ctx, tg_user(9))
    assert ctx.bot.send_message.await_count == 2
    text = ctx.bot.send_message.await_args.args[1]
    assert "tg://user?id=8" in text and "tg://user?id=9" in text
    assert "tg://user?id=7" not in text
    # the previous guide (500) is deleted and dropped from cleanup
    ctx.bot.delete_message.assert_awaited_once_with(CHAT, 500)
    assert [r["message_id"] for r in repo.due_cleanup(10**12)] == [501]


def test_welcome_shares_the_group_throttle(repo, clock):
    ctx = make_context(repo)
    with_message_ids(ctx)
    for uid in (7, 8, 9):
        u = tg_user(uid)
        run(group.on_chat_member(member_update(ChatMemberLeft(u), ChatMemberMember(u)), ctx))
    assert ctx.bot.send_message.await_count == 1
    assert ctx.bot.restrict_chat_member.await_count == 3  # everyone is still restricted


def test_no_second_restrict_within_10_minutes(repo, clock):
    ctx = make_context(repo)
    u = tg_user()
    first = gated(ctx, u)
    second = gated(ctx, u)
    first.effective_message.delete.assert_awaited_once()
    second.effective_message.delete.assert_awaited_once()     # messages still deleted
    assert ctx.bot.restrict_chat_member.await_count == 1
    clock.now += group.RESTRICT_TTL
    gated(ctx, u)
    assert ctx.bot.restrict_chat_member.await_count == 2


def test_failed_restrict_is_retried_next_message(repo, clock):
    ctx = make_context(repo)
    ctx.bot.restrict_chat_member.side_effect = [NetworkError("down"), None]
    gated(ctx, tg_user())
    gated(ctx, tg_user())
    assert ctx.bot.restrict_chat_member.await_count == 2


def test_leaving_forgets_restrict_mark(repo, clock):
    ctx = make_context(repo)
    u = tg_user()
    gated(ctx, u)
    assert (CHAT, u.id) in ctx.bot_data[RESTRICTED_KEY]
    run(group.on_chat_member(member_update(ChatMemberMember(u), ChatMemberLeft(u)), ctx))
    assert (CHAT, u.id) not in ctx.bot_data[RESTRICTED_KEY]
