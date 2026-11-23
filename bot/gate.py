"""Pure decision: should a group message be gated (deleted + sender restricted)?"""
from __future__ import annotations

TELEGRAM_SERVICE_USER_ID = 777000  # "Telegram": channel auto-forwards, service notices


def should_gate(
    *,
    user_id: int | None,
    is_bot: bool,
    sender_chat_id: int | None,
    chat_id: int,
    is_group_admin: bool,
    member_status: str | None,
    has_binding: bool,
    bot_id: int | None = None,
    linked_chat_id: int | None = None,
    is_automatic_forward: bool = False,
) -> bool:
    """True when the message must be removed.

    Exempt: group admins, messages sent on behalf of a chat (anonymous admin = the group
    itself, the linked channel, automatic channel forwards), our own bot and Telegram (777000).
    A message sent "as" some other channel is NOT exempt (otherwise anyone could bypass the
    gate by posting as their own channel).

    A user passes when they have a binding (= a listed code) and are not revoked; pending,
    revoked and unknown users are gated. `is_bot` alone does not exempt: other bots must be
    made group admins to post.
    """
    if sender_chat_id is not None:
        if sender_chat_id == chat_id or is_automatic_forward:
            return False
        if linked_chat_id is not None and sender_chat_id == linked_chat_id:
            return False
        return True
    if user_id is None:
        return False  # nothing to act on
    if user_id == TELEGRAM_SERVICE_USER_ID:
        return False
    if bot_id is not None and user_id == bot_id:
        return False
    if is_group_admin:
        return False
    return not (has_binding and member_status != "revoked")
