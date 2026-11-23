import pytest

from bot.gate import TELEGRAM_SERVICE_USER_ID, should_gate

CHAT = -1001
LINKED = -1002
BOT_ID = 42


def gate(**kw):
    base = dict(user_id=7, is_bot=False, sender_chat_id=None, chat_id=CHAT, is_group_admin=False,
                member_status=None, has_binding=False, bot_id=BOT_ID, linked_chat_id=LINKED)
    base.update(kw)
    return should_gate(**base)


def test_unknown_pending_revoked_are_gated():
    assert gate()  # unknown
    assert gate(member_status="pending")
    assert gate(member_status="revoked")
    assert gate(member_status="revoked", has_binding=True)


def test_verified_passes():
    assert not gate(member_status="verified", has_binding=True)
    assert not gate(member_status=None, has_binding=True)  # bound before joining


def test_verified_without_binding_is_gated():
    assert gate(member_status="verified", has_binding=False)


def test_group_admin_exempt():
    assert not gate(is_group_admin=True)
    assert not gate(is_group_admin=True, member_status="revoked")


@pytest.mark.parametrize("sender", [CHAT, LINKED])
def test_anonymous_admin_and_linked_channel_exempt(sender):
    assert not gate(user_id=1087968824, sender_chat_id=sender)


def test_automatic_forward_exempt_even_without_linked_id():
    assert not gate(user_id=TELEGRAM_SERVICE_USER_ID, sender_chat_id=-100555,
                    linked_chat_id=None, is_automatic_forward=True)


def test_posting_as_foreign_channel_is_gated():
    assert gate(user_id=136817688, sender_chat_id=-100999)


def test_own_bot_and_telegram_exempt():
    assert not gate(user_id=BOT_ID, is_bot=True)
    assert not gate(user_id=TELEGRAM_SERVICE_USER_ID)


def test_other_bots_gated_unless_admin():
    assert gate(user_id=99, is_bot=True)
    assert not gate(user_id=99, is_bot=True, is_group_admin=True)


def test_no_user_no_sender_not_gated():
    assert not gate(user_id=None)
