"""Telegram-side actions shared by handlers."""
from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Iterable
from datetime import timedelta

from telegram import Bot, Chat, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.error import Forbidden, RetryAfter, TelegramError

from bot import texts

log = logging.getLogger(__name__)

REQUEST_GAP_SECONDS = 0.05
# bot_data key: {(chat_id, user_id): epoch} of users the gate restricted recently
RESTRICTED_KEY = "restricted_at"

_RIGHTS_MARKERS = ("not enough rights", "chat_admin_required", "need administrator rights",
                   "have no rights", "can't restrict", "can't remove chat owner")
_NOT_MEMBER_MARKERS = ("user not found", "participant", "member not found",
                       "user_not_participant", "user_id_invalid", "invalid user_id")


def is_rights_error(e: TelegramError) -> bool:
    """The bot lacks admin rights in the group (an admin must fix it)."""
    msg = str(e).lower()
    return isinstance(e, Forbidden) or any(m in msg for m in _RIGHTS_MARKERS)


def is_not_member_error(e: TelegramError) -> bool:
    """The user is not (or no longer) in the group."""
    msg = str(e).lower()
    return any(m in msg for m in _NOT_MEMBER_MARKERS)


def retry_seconds(e: RetryAfter) -> float:
    """RetryAfter.retry_after is int or timedelta depending on PTB settings."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        value = e.retry_after
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


async def warn_rights(bot: Bot, admin_ids: Iterable[int]) -> None:
    await notify(bot, sorted(admin_ids), texts.bot_rights_warning([texts.RIGHT_RESTRICT]))


async def revoke_users(bot: Bot, chat_id: int, user_ids: Iterable[int],
                       admin_ids: Iterable[int] = ()) -> int:
    """Restrict (mute) users whose code was removed; they are not kicked.
    Only users who are in the group (member / restricted) are touched: restricting a banned
    user would lift the ban. Missing rights: admins are told once and the rest is skipped.
    Errors are logged, never raised. Returns how many restrictions succeeded."""
    ok = 0
    for user_id in user_ids:
        for _try in range(2):  # a second try only after RetryAfter
            try:
                member = await bot.get_chat_member(chat_id, user_id)
                if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
                    await bot.restrict_chat_member(chat_id, user_id,
                                                   ChatPermissions.no_permissions())
                    ok += 1
                else:
                    log.info("revoke skipped chat=%s user=%s status=%s",
                             chat_id, user_id, member.status)
            except RetryAfter as e:
                log.warning("revoke flood-limited; waiting %ss", retry_seconds(e))
                await asyncio.sleep(retry_seconds(e))
                continue
            except TelegramError as e:
                if is_rights_error(e):
                    log.error("revoke stopped, no rights in chat=%s: %s", chat_id, e)
                    await warn_rights(bot, admin_ids)
                    return ok
                log.warning("restrict failed chat=%s user=%s: %s", chat_id, user_id, e)
            break
        await asyncio.sleep(REQUEST_GAP_SECONDS)
    return ok


# Fallback when the group has no default permissions: plain chatting with media.
BASIC_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def group_permissions(bot: Bot, chat_id: int) -> tuple[ChatPermissions, Chat | None]:
    """The group's default member permissions (BASIC_PERMISSIONS if unknown) and the Chat."""
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramError as e:
        log.warning("get_chat failed chat=%s: %s", chat_id, e)
        return BASIC_PERMISSIONS, None
    return chat.permissions or BASIC_PERMISSIONS, chat


def group_link(chat: Chat | None) -> str | None:
    if chat is None:
        return None
    if chat.username:
        return f"https://t.me/{chat.username}"
    return chat.invite_link


async def notify(bot: Bot, chat_ids: Iterable[int], text: str) -> int:
    """Send `text` to each chat; failures are logged. Returns how many were delivered."""
    sent = 0
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text)
            sent += 1
        except TelegramError as e:
            log.warning("notify failed chat=%s: %s", chat_id, e)
    return sent


def report_targets(report_chat_id: int | None, admin_ids: Iterable[int]) -> list[int]:
    """Where verification/lock reports go: the configured report chat, else every env admin."""
    return [report_chat_id] if report_chat_id else sorted(admin_ids)
