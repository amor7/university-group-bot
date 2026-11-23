"""Private chat: /start and student-code verification (any digits-only text is a code)."""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot import texts
from bot.actions import (
    RESTRICTED_KEY, group_link, group_permissions, is_not_member_error, is_rights_error, notify,
    report_targets, warn_rights,
)
from bot.config import Config
from bot.handlers.admin import CONFIG_KEY, PRIVATE, REPO_KEY, is_bot_admin
from bot.repo import Repo
from bot.verify import GroupSettings, VerifyResult, VerifyStatus, load_settings, looks_like_code, verify_code

log = logging.getLogger(__name__)

AWAIT_CODE = "await_code"  # user_data flag: the next text is a code (used for admins)


def now() -> int:
    return int(time.time())


def should_check(text: str | None, is_admin: bool, awaiting: bool) -> bool:
    """Non-admins: every text is a code attempt. Admins: only code-like text or while waiting."""
    if not text:
        return False
    return not is_admin or awaiting or looks_like_code(text)


def result_text(result: VerifyResult, settings: GroupSettings, config: Config) -> str:
    s = VerifyStatus
    match result.status:
        case s.OK:
            text = texts.VERIFIED_OK.format(code=texts.fa_num(result.code))
        case s.ALREADY_YOURS:
            text = texts.ALREADY_VERIFIED
        case s.HAS_OTHER_CODE:
            text = texts.has_other_code(result.code or "", settings.contact_text,
                                        config.default_contact)
        case s.BOUND_TO_OTHER:
            text = texts.bound_to_other(settings.contact_text, config.default_contact)
        case s.NOT_FOUND:
            text = texts.not_found(settings.contact_text, config.default_contact)
        case s.INVALID_FORMAT:
            text = texts.INVALID_FORMAT
        case s.LOCKED:
            return texts.locked(result.seconds_left, result.locked_until)
        case _:
            text = texts.ERROR
    if result.just_locked:
        text += "\n\n" + texts.locked(result.seconds_left, result.locked_until)
    elif result.fails > 0:  # a counted failure that did not lock (yet)
        text += "\n\n" + texts.attempts_left(max(0, settings.max_attempts - result.fails))
    return text


class AdmitStatus(enum.Enum):
    OK = "ok"              # free to chat in the group (or join request approved / not in it)
    KICKED = "kicked"      # banned from the group: only a human can undo that
    RETRY = "retry"        # temporary problem (no rights, get_chat failed, ...): /start later


@dataclass(frozen=True)
class Admission:
    status: AdmitStatus
    link: str | None = None


async def _admit_error(context: ContextTypes.DEFAULT_TYPE, e: TelegramError, what: str,
                       group_id: int, user_id: int) -> AdmitStatus | None:
    """None = "user not in the group" (fine); otherwise RETRY (admins told about rights)."""
    if is_not_member_error(e):
        log.info("%s: user not in group chat=%s user=%s: %s", what, group_id, user_id, e)
        return None
    if is_rights_error(e):
        log.error("%s: bot lacks rights chat=%s user=%s: %s", what, group_id, user_id, e)
        await warn_rights(context.bot, context.bot_data[CONFIG_KEY].admin_ids)
    else:
        log.warning("%s failed chat=%s user=%s: %s", what, group_id, user_id, e)
    return AdmitStatus.RETRY


async def admit(context: ContextTypes.DEFAULT_TYPE, group_id: int, user_id: int) -> Admission:
    """Let a registered user into the group, depending on their current status there:
    restricted → restore the group's default permissions; left / not found → only approve a
    pending join request; kicked (banned) → do nothing (restricting would lift the ban);
    member / admin → nothing to do. Never unrestricts with guessed permissions."""
    bot = context.bot
    try:
        status = (await bot.get_chat_member(group_id, user_id)).status
    except TelegramError as e:
        problem = await _admit_error(context, e, "get_chat_member", group_id, user_id)
        if problem is not None:
            return Admission(problem)
        status = ChatMemberStatus.LEFT
    if status == ChatMemberStatus.BANNED:
        log.info("admit refused, user is banned chat=%s user=%s", group_id, user_id)
        return Admission(AdmitStatus.KICKED)
    perms, chat = await group_permissions(bot, group_id)
    if status == ChatMemberStatus.RESTRICTED:
        if chat is None:  # don't unrestrict with fallback permissions the group may not allow
            log.warning("unrestrict postponed, group permissions unknown chat=%s user=%s",
                        group_id, user_id)
            return Admission(AdmitStatus.RETRY)
        try:
            await bot.restrict_chat_member(group_id, user_id, perms)
        except TelegramError as e:
            problem = await _admit_error(context, e, "unrestrict", group_id, user_id)
            if problem is not None:
                return Admission(problem)
        context.bot_data.get(RESTRICTED_KEY, {}).pop((group_id, user_id), None)
    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.RESTRICTED):
        try:
            await bot.approve_chat_join_request(group_id, user_id)
        except TelegramError as e:  # no open request — the normal case
            log.debug("no join request to approve chat=%s user=%s: %s", group_id, user_id, e)
    return Admission(AdmitStatus.OK, group_link(chat))


def admission_problem(admission: Admission, settings: GroupSettings,
                      config: Config) -> str | None:
    if admission.status == AdmitStatus.KICKED:
        return texts.admit_kicked(settings.contact_text, config.default_contact)
    if admission.status == AdmitStatus.RETRY:
        return texts.ADMIT_RETRY
    return None


def _link_keyboard(link: str | None) -> InlineKeyboardMarkup | None:
    if not link:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(texts.BTN_GROUP, url=link)]])


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> tuple[Config, Repo]:
    return context.bot_data[CONFIG_KEY], context.bot_data[REPO_KEY]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, repo = _ctx(context)
    user, msg = update.effective_user, update.effective_message
    if user is None or msg is None:
        return
    if config.group_id is None:
        await msg.reply_text(texts.START)
        return
    if repo.get_binding_by_user(config.group_id, user.id) is not None:
        # registered earlier but maybe still restricted (old member, failed unrestrict): retry
        context.user_data.pop(AWAIT_CODE, None)
        admission = await admit(context, config.group_id, user.id)
        problem = admission_problem(admission, load_settings(repo, config.group_id), config)
        await msg.reply_text(f"{texts.ALREADY_VERIFIED}\n\n{problem}" if problem
                             else texts.ALREADY_VERIFIED,
                             reply_markup=_link_keyboard(admission.link))
        return
    context.user_data[AWAIT_CODE] = True
    await msg.reply_text(f"{texts.START}\n\n{texts.ASK_CODE}")


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, repo = _ctx(context)
    user: User | None = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return
    is_admin = is_bot_admin(config, repo, user.id)
    if not should_check(msg.text, is_admin, bool(context.user_data.get(AWAIT_CODE))):
        return
    if config.group_id is None:
        await msg.reply_text(texts.NO_GROUP if is_admin else texts.START)
        return
    group_id = config.group_id
    settings = load_settings(repo, group_id)
    result = verify_code(repo, group_id, user, msg.text, now(), settings)
    log.info("verify user=%s result=%s", user.id, result.status.value)

    reply = result_text(result, settings, config)
    markup = None
    if result.status in (VerifyStatus.OK, VerifyStatus.ALREADY_YOURS):
        context.user_data.pop(AWAIT_CODE, None)
        admission = await admit(context, group_id, user.id)
        markup = _link_keyboard(admission.link)
        problem = admission_problem(admission, settings, config)
        if problem:  # saved, but "you can chat now" would be wrong
            base = (texts.VERIFIED_SAVED.format(code=texts.fa_num(result.code))
                    if result.status == VerifyStatus.OK else texts.ALREADY_VERIFIED)
            reply = f"{base}\n\n{problem}"
    await msg.reply_text(reply, reply_markup=markup)

    targets = report_targets(settings.report_chat_id, config.admin_ids)
    if result.status == VerifyStatus.OK:
        await notify(context.bot, targets, texts.verified_report(
            result.code or "", result.student_name, user.full_name, user.username, user.id))
    if result.just_locked:
        await notify(context.bot, targets, texts.locked_report(
            user.id, user.username, user.full_name, result.code or "", settings.lock_minutes))


def register(app: Application) -> None:
    """/start is registered here; the text handler must come AFTER admin handlers in the
    same group so it only sees what admin handlers didn't take (see main.py)."""
    app.add_handler(CommandHandler("start", cmd_start, filters=PRIVATE))


def register_text(app: Application) -> None:
    app.add_handler(MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, on_private_text))
