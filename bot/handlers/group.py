"""Group gate: delete messages of unverified users, restrict them, guide them to the bot;
track joins/leaves (chat_member) and the bot's own rights (my_chat_member)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from telegram import (
    Bot, ChatMember, ChatMemberUpdated, ChatPermissions, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, Update, User,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, ChatMemberHandler, ContextTypes, MessageHandler, filters,
)

from bot import texts
from bot.actions import RESTRICTED_KEY, group_permissions, notify
from bot.config import Config
from bot.gate import should_gate
from bot.handlers.admin import CONFIG_KEY, REPO_KEY
from bot.handlers.join import is_registered
from bot.repo import Repo
from bot.verify import load_settings, pending_deadline

log = logging.getLogger(__name__)

GATE_GROUP = -1              # handler group: runs before everything else
ADMIN_CACHE_KEY = "admin_cache"
GUIDE_SENT_KEY = "guide_sent"
ADMIN_CACHE_TTL = 600        # seconds
GUIDE_INTERVAL = 600         # at most one guide message per user per 10 minutes
GUIDE_TTL = 180              # guide/welcome messages are deleted after 3 minutes
NOTICE_KEY = "group_notice"  # bot_data: {chat_id: NoticeState}
NOTICE_INTERVAL = 20         # at most one guide/welcome message per group per 20 seconds
MAX_MENTIONS = 5             # users mentioned in one guide/welcome message
RESTRICT_TTL = 600           # don't restrict the same user again within 10 minutes
START_PAYLOAD = "verify"

ADMIN_STATUSES = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)


def now() -> int:
    return int(time.time())


# ---------- pure helpers ----------
@dataclass
class AdminInfo:
    admin_ids: frozenset[int]
    linked_chat_id: int | None
    fetched_at: int


def cache_valid(info: AdminInfo | None, at: int, ttl: int = ADMIN_CACHE_TTL) -> bool:
    return info is not None and at - info.fetched_at < ttl


def guide_due(sent: dict[int, int], user_id: int, at: int, interval: int = GUIDE_INTERVAL) -> bool:
    """True (and records `at`) if no guide was sent to this user in the last `interval` s."""
    last = sent.get(user_id)
    if last is not None and at - last < interval:
        return False
    if len(sent) > 5000:  # keep memory bounded during a raid
        for uid in [u for u, t in sent.items() if at - t >= interval]:
            del sent[uid]
    sent[user_id] = at
    return True


@dataclass
class NoticeState:
    """Per-group throttle for guide/welcome messages."""
    last_at: int | None = None
    message_id: int | None = None          # the last guide/welcome sent (deleted on the next)
    mentions: list[str] = field(default_factory=list)  # users waiting for the next message


def take_notice(state: NoticeState, mention: str, at: int,
                interval: int = NOTICE_INTERVAL, cap: int = MAX_MENTIONS) -> list[str] | None:
    """Queue `mention`; if the group may get a message now, return the queued mentions
    (at most `cap`, possibly fewer) and reset the queue. None = wait (still throttled)."""
    if mention not in state.mentions and len(state.mentions) < cap:
        state.mentions.append(mention)
    if state.last_at is not None and at - state.last_at < interval:
        return None
    mentions, state.mentions = state.mentions, []
    state.last_at = at
    return mentions


def recently_restricted(cache: dict[tuple[int, int], int], chat_id: int, user_id: int,
                        at: int, ttl: int = RESTRICT_TTL) -> bool:
    last = cache.get((chat_id, user_id))
    return last is not None and at - last < ttl


def mark_restricted(cache: dict[tuple[int, int], int], chat_id: int, user_id: int,
                    at: int, ttl: int = RESTRICT_TTL) -> None:
    if len(cache) > 5000:  # keep memory bounded during a raid
        for key in [k for k, t in cache.items() if at - t >= ttl]:
            del cache[key]
    cache[(chat_id, user_id)] = at


def is_in_chat(member: ChatMember) -> bool:
    if member.status in (*ADMIN_STATUSES, ChatMemberStatus.MEMBER):
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


def is_admin_member(member: ChatMember) -> bool:
    return member.status in ADMIN_STATUSES


def member_change(update: ChatMemberUpdated) -> tuple[bool, bool, bool]:
    """(joined, left, admin_changed) for a chat_member update."""
    old, new = update.old_chat_member, update.new_chat_member
    was, now_in = is_in_chat(old), is_in_chat(new)
    return (not was and now_in, was and not now_in, is_admin_member(old) != is_admin_member(new))


def rights_warning(member: ChatMember | None) -> str | None:
    """Warning text if the bot can't do its job in the group, else None."""
    if member is None or member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return texts.BOT_REMOVED
    if member.status == ChatMemberStatus.OWNER:
        return None
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return texts.bot_rights_warning(
            [texts.RIGHT_ADMIN, texts.RIGHT_DELETE, texts.RIGHT_RESTRICT])
    missing = []
    if not getattr(member, "can_delete_messages", False):
        missing.append(texts.RIGHT_DELETE)
    if not getattr(member, "can_restrict_members", False):
        missing.append(texts.RIGHT_RESTRICT)
    return texts.bot_rights_warning(missing) if missing else None


def verify_keyboard(bot_username: str | None) -> InlineKeyboardMarkup | None:
    if not bot_username:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        texts.BTN_VERIFY, url=f"https://t.me/{bot_username}?start={START_PAYLOAD}")]])


# ---------- admin cache ----------
async def get_admin_info(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> AdminInfo | None:
    """Cached admin ids + linked channel; None if unknown (fetch failed, nothing cached)."""
    cache: dict[int, AdminInfo] = context.bot_data.setdefault(ADMIN_CACHE_KEY, {})
    info = cache.get(chat_id)
    at = now()
    if cache_valid(info, at):
        return info  # type: ignore[return-value]
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        chat = await context.bot.get_chat(chat_id)
    except TelegramError as e:
        log.warning("could not fetch admins of chat=%s: %s", chat_id, e)
        if info is not None:
            return info  # stale is better than treating admins as strangers
        return None
    info = AdminInfo(frozenset(a.user.id for a in admins), chat.linked_chat_id, at)
    cache[chat_id] = info
    return info


def invalidate_admin_cache(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    context.bot_data.setdefault(ADMIN_CACHE_KEY, {}).pop(chat_id, None)


# ---------- actions ----------
async def send_guide(
    context: ContextTypes.DEFAULT_TYPE, repo: Repo, chat_id: int, text: str, at: int,
) -> Message | None:
    try:
        sent = await context.bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML,
            reply_markup=verify_keyboard(context.bot.username),
        )
    except TelegramError as e:
        log.warning("guide message failed chat=%s: %s", chat_id, e)
        return None
    repo.add_cleanup(chat_id, sent.message_id, at + GUIDE_TTL)
    return sent


async def send_notice(context: ContextTypes.DEFAULT_TYPE, repo: Repo, chat_id: int,
                      mention: str, make_text, at: int) -> Message | None:
    """Guide/welcome with the per-group throttle: mentions pile up while throttled and go out
    in the next message; the group's previous guide/welcome is deleted."""
    states: dict[int, NoticeState] = context.bot_data.setdefault(NOTICE_KEY, {})
    state = states.setdefault(chat_id, NoticeState())
    mentions = take_notice(state, mention, at)
    if mentions is None:
        return None
    sent = await send_guide(context, repo, chat_id, make_text("، ".join(mentions)), at)
    if sent is None:
        return None
    old, state.message_id = state.message_id, sent.message_id
    if old is not None:
        try:
            await context.bot.delete_message(chat_id, old)
            repo.delete_cleanup(chat_id, old)
        except TelegramError as e:  # the sweeper will try again (cleanup row stays)
            log.info("old guide delete failed chat=%s msg=%s: %s", chat_id, old, e)
    return sent


async def restrict_all(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        await bot.restrict_chat_member(chat_id, user_id, ChatPermissions.no_permissions())
        return True
    except TelegramError as e:
        log.warning("restrict failed chat=%s user=%s: %s", chat_id, user_id, e)
        return False


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> tuple[Config, Repo]:
    return context.bot_data[CONFIG_KEY], context.bot_data[REPO_KEY]


# ---------- handlers ----------
async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, repo = _ctx(context)
    msg, chat = update.effective_message, update.effective_chat
    if msg is None or chat is None or chat.id != config.group_id:
        return
    user: User | None = msg.from_user
    sender_chat = msg.sender_chat
    member = repo.get_member(chat.id, user.id) if user and not sender_chat else None
    binding = repo.get_binding_by_user(chat.id, user.id) if user and not sender_chat else None

    def gate(info: AdminInfo | None) -> bool:
        return should_gate(
            user_id=user.id if user else None, is_bot=bool(user and user.is_bot),
            sender_chat_id=sender_chat.id if sender_chat else None, chat_id=chat.id,
            is_group_admin=bool(info and user and user.id in info.admin_ids),
            member_status=member["status"] if member else None,
            has_binding=binding is not None, bot_id=context.bot.id,
            linked_chat_id=info.linked_chat_id if info else None,
            is_automatic_forward=bool(msg.is_automatic_forward),
        )

    # Cheap pass first (no admin info = strictest): verified users cost no API call.
    if not gate(None):
        return
    info = await get_admin_info(context, chat.id)
    if info is None:
        log.warning("admin list unknown; not gating msg=%s in chat=%s", msg.message_id, chat.id)
        return  # fail open rather than delete admins' messages
    if not gate(info):
        return

    try:
        await msg.delete()
    except TelegramError as e:
        log.warning("delete failed chat=%s msg=%s: %s", chat.id, msg.message_id, e)
    if sender_chat is not None or user is None:
        raise ApplicationHandlerStop  # posted "as" a foreign channel: deleting is all we do
    at = now()
    restricted: dict[tuple[int, int], int] = context.bot_data.setdefault(RESTRICTED_KEY, {})
    if not recently_restricted(restricted, chat.id, user.id, at):  # else: only delete
        if await restrict_all(context.bot, chat.id, user.id):
            mark_restricted(restricted, chat.id, user.id, at)
    if user.is_bot:
        raise ApplicationHandlerStop
    if member is None:
        settings = load_settings(repo, chat.id)
        repo.upsert_pending(chat.id, user.id, at, pending_deadline(settings.deadline_hours, at))
    if guide_due(context.bot_data.setdefault(GUIDE_SENT_KEY, {}), user.id, at):
        await send_notice(context, repo, chat.id, user.mention_html(), texts.gate_guide, at)
    raise ApplicationHandlerStop


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, repo = _ctx(context)
    cmu = update.chat_member
    if cmu is None or cmu.chat.id != config.group_id:
        return
    chat_id = cmu.chat.id
    joined, left, admin_changed = member_change(cmu)
    if admin_changed:
        invalidate_admin_cache(context, chat_id)
    user = cmu.new_chat_member.user
    if left:
        repo.delete_member(chat_id, user.id)  # a rejoin gets a fresh deadline; binding stays
        context.bot_data.setdefault(GUIDE_SENT_KEY, {}).pop(user.id, None)
        context.bot_data.setdefault(RESTRICTED_KEY, {}).pop((chat_id, user.id), None)
        return
    if not joined or user.is_bot or is_admin_member(cmu.new_chat_member):
        return
    at = now()
    if is_registered(repo, chat_id, user.id):
        repo.set_status(chat_id, user.id, "verified", at)
        if cmu.new_chat_member.status == ChatMemberStatus.RESTRICTED:  # left over from before
            perms, _ = await group_permissions(context.bot, chat_id)
            try:
                await context.bot.restrict_chat_member(chat_id, user.id, perms)
            except TelegramError as e:
                log.warning("unrestrict failed chat=%s user=%s: %s", chat_id, user.id, e)
        return
    if await restrict_all(context.bot, chat_id, user.id):
        mark_restricted(context.bot_data.setdefault(RESTRICTED_KEY, {}), chat_id, user.id, at)
    settings = load_settings(repo, chat_id)
    repo.upsert_pending(chat_id, user.id, at, pending_deadline(settings.deadline_hours, at))
    context.bot_data.setdefault(GUIDE_SENT_KEY, {})[user.id] = at  # no extra guide right away
    await send_notice(context, repo, chat_id, user.mention_html(),
                      lambda who: texts.welcome(who, settings.deadline_hours), at)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _repo = _ctx(context)
    cmu = update.my_chat_member
    if cmu is None or cmu.chat.id != config.group_id:
        return
    invalidate_admin_cache(context, cmu.chat.id)
    warning = rights_warning(cmu.new_chat_member)
    if warning:
        await notify(context.bot, sorted(config.admin_ids), warning)


async def check_bot_rights(app: Application) -> None:
    """Startup check (post_init): warn ADMIN_IDS if the bot can't moderate the group."""
    config: Config = app.bot_data[CONFIG_KEY]
    if config.group_id is None:
        return
    try:
        member = await app.bot.get_chat_member(config.group_id, app.bot.id)
    except TelegramError as e:
        log.warning("startup rights check failed chat=%s: %s", config.group_id, e)
        member = None
    warning = rights_warning(member)
    if warning:
        log.warning("bot rights problem in chat=%s: %s", config.group_id, warning)
        await notify(app.bot, sorted(config.admin_ids), warning)


def register(app: Application, group_id: int) -> None:
    gate_filter = (filters.Chat(group_id) & filters.UpdateType.MESSAGES
                   & ~filters.StatusUpdate.ALL)
    app.add_handler(MessageHandler(gate_filter, on_group_message), group=GATE_GROUP)
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER,
                                      chat_id=group_id))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER,
                                      chat_id=group_id))
