"""Stage-5 admin commands: users (/unbind, /whois, /unlock, /verify_user, /stats), settings
(/set_contact, /set_deadline, /set_attempts, /set_report, /settings), bot admins
(/add_admin, /remove_admin, /admins), /logs and /help, plus the bot command menus.

Everything is private-chat only and bot-admin only (admin._guard), except:
- /help answers everyone (short help for students);
- /set_report also works when a bot admin sends it inside another group (the report chat).
Every change is written to admin_log and reported to the report chat (see _audit).
"""
from __future__ import annotations

import enum
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from bot import texts
from bot.actions import notify, report_targets
from bot.config import Config
from bot.handlers.admin import (
    CONFIG_KEY, PRIVATE, REPO_KEY, Ctx, _command_body, _guard, _keyboard, _revoke_in_background,
    is_bot_admin,
)
from bot.handlers.private import admit
from bot.normalize import normalize_code, normalize_digits
from bot.repo import CodeAlreadyBound, Repo, Row
from bot.verify import load_settings, looks_like_code, pending_deadline

log = logging.getLogger(__name__)

DEADLINE_MAX_HOURS = 720
PENDING_DEADLINE = "pending_deadline"  # user_data key: a /set_deadline waiting for confirmation
ATTEMPTS_RANGE = (1, 20)
LOCK_MINUTES_RANGE = (1, 7 * 24 * 60)
CONTACT_MAX_LEN = 1000
LOGS_DEFAULT, LOGS_MAX = 20, 100
MESSAGE_LIMIT = 4000  # Telegram allows 4096 characters per message

GROUP_MESSAGES = filters.ChatType.GROUPS & filters.UpdateType.MESSAGE
_CHANNEL_SET_REPORT = re.compile(r"^/set_report(?:@(\w+))?(?:\s|$)", re.IGNORECASE)


def now() -> int:
    return int(time.time())


# ====================== pure helpers ======================
class UsageError(ValueError):
    """Bad command arguments; str(error) is the Persian message shown to the admin."""


def _digits(raw: str) -> str | None:
    """Persian/Arabic digits → ASCII; None unless the whole (stripped) input is digits."""
    s = normalize_digits((raw or "").strip())
    return s if s.isascii() and s.isdigit() else None


def parse_int(raw: str, lo: int, hi: int, usage: str) -> int:
    d = _digits(raw)
    if d is None:
        raise UsageError(usage)
    value = int(d)
    if not lo <= value <= hi:
        raise UsageError(usage)
    return value


def parse_user_id(raw: str) -> int | None:
    d = _digits(raw)
    return int(d) if d is not None and int(d) > 0 else None


def parse_deadline(args: list[str]) -> int:
    """Hours: 0 (kick off) or 1..720."""
    if len(args) != 1:
        raise UsageError(texts.SET_DEADLINE_USAGE)
    return parse_int(args[0], 0, DEADLINE_MAX_HOURS, texts.SET_DEADLINE_USAGE)


def parse_attempts(args: list[str]) -> tuple[int, int | None]:
    """(max_attempts, lock_minutes or None = unchanged)."""
    if len(args) not in (1, 2):
        raise UsageError(texts.SET_ATTEMPTS_USAGE)
    attempts = parse_int(args[0], *ATTEMPTS_RANGE, texts.SET_ATTEMPTS_USAGE)
    minutes = parse_int(args[1], *LOCK_MINUTES_RANGE, texts.SET_ATTEMPTS_USAGE) \
        if len(args) == 2 else None
    return attempts, minutes


def parse_contact(body: str) -> tuple[str, str | None]:
    """('show', None) | ('reset', None) | ('set', text)."""
    text = (body or "").strip()
    if not text:
        return "show", None
    if text.casefold() == "reset":
        return "reset", None
    if len(text) > CONTACT_MAX_LEN:
        raise UsageError(texts.CONTACT_TOO_LONG.format(max=texts.fa_num(CONTACT_MAX_LEN)))
    return "set", text


def parse_report(args: list[str]) -> tuple[str, int | None]:
    """('show', None) | ('off', None) | ('set', chat_id)."""
    if not args:
        return "show", None
    if len(args) != 1:
        raise UsageError(texts.SET_REPORT_USAGE)
    arg = normalize_digits(args[0].strip()).lstrip("‎‏")  # LRM/RLM from copy-paste
    if arg.casefold() == "off":
        return "off", None
    if not re.fullmatch(r"-?[0-9]+", arg) or int(arg) == 0:
        raise UsageError(texts.REPORT_BAD_ID)
    return "set", int(arg)


def parse_verify_user(args: list[str]) -> tuple[int, str]:
    if len(args) != 2:
        raise UsageError(texts.VERIFY_USER_USAGE)
    user_id = parse_user_id(args[0])
    if user_id is None:
        raise UsageError(texts.VERIFY_USER_BAD_ID)
    if not looks_like_code(args[1]):
        raise UsageError(texts.VERIFY_USER_BAD_CODE)
    return user_id, normalize_code(args[1])


def parse_logs_n(args: list[str]) -> int:
    if not args:
        return LOGS_DEFAULT
    if len(args) != 1:
        raise UsageError(texts.LOGS_USAGE)
    return min(parse_int(args[0], 1, 10**6, texts.LOGS_USAGE), LOGS_MAX)


def registration_percent(stats: dict[str, int]) -> float:
    return 100.0 * stats["bound"] / stats["students"] if stats["students"] else 0.0


def chunk_lines(lines: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Join lines into messages of at most `limit` characters (a longer line is cut)."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        line = line[:limit]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


# ---------- lookup (/whois, /unbind, /unlock) ----------
@dataclass(frozen=True)
class Lookup:
    query: str
    code: str | None          # set when the query matched a code (listed or bound)
    user_id: int | None
    student: Row | None
    binding: Row | None
    member: Row | None
    locked_until: int | None
    fails: int

    @property
    def found(self) -> bool:
        return any(x is not None for x in (self.student, self.binding, self.member,
                                           self.locked_until)) or self.fails > 0


def _is_username(raw: str) -> bool:
    return raw.startswith("@") or _digits(raw) is None


def lookup(repo: Repo, chat_id: int, raw: str, at: int) -> Lookup:
    """@username (or any non-digit text) → binding by username. Digits only → first a code
    (listed or bound), then a user_id."""
    query = raw.strip()
    code: str | None = None
    user_id: int | None = None
    if _is_username(query):
        binding = repo.get_binding_by_username(chat_id, query)
    else:
        digits = _digits(query) or ""
        binding = repo.get_binding_by_code(chat_id, digits)
        if binding is not None or repo.find_student(chat_id, digits) is not None:
            code = digits
        else:
            user_id = int(digits)
            binding = repo.get_binding_by_user(chat_id, user_id)
    if binding is not None:
        code, user_id = binding["code"], binding["user_id"]
    student = repo.find_student(chat_id, code) if code is not None else None
    member = repo.get_member(chat_id, user_id) if user_id is not None else None
    attempts = repo.get_attempts(chat_id, user_id) if user_id is not None else None
    locked = repo.is_locked(chat_id, user_id, at) if user_id is not None else None
    return Lookup(query, code, user_id, student, binding, member, locked,
                  attempts["fails"] if attempts and locked is None else 0)


def whois_text(lk: Lookup) -> str:
    if not lk.found:
        return texts.NOT_FOUND_TARGET.format(query=lk.query)
    return texts.whois(code=lk.code, student=lk.student, binding=lk.binding,
                       user_id=lk.user_id, member=lk.member, locked_until=lk.locked_until,
                       fails=lk.fails)


def unbind(repo: Repo, chat_id: int, raw: str, at: int, deadline_hours: int) -> Row | None:
    """Release the binding found by `raw`; its user goes back to pending with a fresh
    deadline (none when kicking is off). Returns the removed binding or None."""
    lk = lookup(repo, chat_id, raw, at)
    if lk.binding is None:
        return None
    return repo.release_binding(chat_id, lk.binding["code"], at,
                                pending_deadline(deadline_hours, at))


def resolve_user_id(repo: Repo, chat_id: int, raw: str) -> int | None:
    """/unlock: digits are a user_id; otherwise a bound @username."""
    raw = raw.strip()
    if _is_username(raw):
        b = repo.get_binding_by_username(chat_id, raw)
        return b["user_id"] if b else None
    return parse_user_id(raw)


# ---------- /verify_user ----------
class ManualStatus(enum.Enum):
    OK = "ok"
    ALREADY = "already"                # the user already held this code
    BOUND_TO_OTHER = "bound_to_other"  # someone else holds it: /unbind first


@dataclass(frozen=True)
class ManualVerify:
    status: ManualStatus
    code: str
    added_to_list: bool = False
    prev_code: str | None = None
    holder: int | None = None


def manual_verify(repo: Repo, chat_id: int, user_id: int, code: str, username: str | None,
                  full_name: str | None, at: int) -> ManualVerify:
    """Bind user_id to code (adding the code to the list first if needed) and verify."""
    code = normalize_code(code)
    holder = repo.get_binding_by_code(chat_id, code)
    if holder is not None and holder["user_id"] != user_id:
        return ManualVerify(ManualStatus.BOUND_TO_OTHER, code, holder=holder["user_id"])
    mine = repo.get_binding_by_user(chat_id, user_id)
    if mine is not None and username is None and full_name is None:
        username, full_name = mine["username"], mine["full_name"]  # don't lose known names
    added = False
    if repo.find_student(chat_id, code) is None:
        repo.add_students(chat_id, [(code, None)])
        added = True
    try:
        prev = repo.bind_and_verify(chat_id, user_id, code, username, full_name, at)
    except CodeAlreadyBound as e:  # lost a race
        return ManualVerify(ManualStatus.BOUND_TO_OTHER, code, added, holder=e.user_id)
    status = ManualStatus.ALREADY if holder is not None else ManualStatus.OK
    return ManualVerify(status, code, added, prev_code=prev)


# ---------- bot admins ----------
class AdminChange(enum.Enum):
    ADDED = "added"
    REMOVED = "removed"
    ALREADY = "already"
    NOT_ADMIN = "not_admin"
    IS_OWNER = "is_owner"       # target is in ADMIN_IDS: can't be added/removed here
    NOT_OWNER = "not_owner"     # actor is not in ADMIN_IDS


def change_admin(config: Config, repo: Repo, actor: int, target: int, add: bool,
                 at: int) -> AdminChange:
    if actor not in config.admin_ids:
        return AdminChange.NOT_OWNER
    if target in config.admin_ids:
        return AdminChange.IS_OWNER
    if add:
        return AdminChange.ADDED if repo.add_admin(target, actor, at) else AdminChange.ALREADY
    return AdminChange.REMOVED if repo.remove_admin(target) else AdminChange.NOT_ADMIN


def admin_change_text(result: AdminChange, target: int, add: bool) -> str:
    return {
        AdminChange.ADDED: texts.admin_added(target),
        AdminChange.REMOVED: texts.admin_removed(target),
        AdminChange.ALREADY: texts.ADMIN_ALREADY,
        AdminChange.NOT_ADMIN: texts.ADMIN_NOT_FOUND,
        AdminChange.IS_OWNER: texts.ADMIN_IS_OWNER_ADD if add else texts.ADMIN_IS_OWNER_REMOVE,
        AdminChange.NOT_OWNER: texts.ONLY_OWNERS,
    }[result]


def is_owner(config: Config, user_id: int) -> bool:
    return user_id in config.admin_ids


def commands_for(config: Config, user_id: int) -> list[tuple[str, str]]:
    return texts.OWNER_COMMANDS if is_owner(config, user_id) else texts.ADMIN_COMMANDS


# ====================== Telegram side ======================
def _args(update: Update) -> list[str]:
    return _command_body(update).split()


async def _reply(update: Update, text: str, html: bool = False) -> None:
    """html=True for usage/help texts (HTML-formatted, dynamic parts already escaped)."""
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML if html else None)


async def _audit(update: Update, context: ContextTypes.DEFAULT_TYPE, c: Ctx, action: str,
                 details: dict[str, Any] | None, report: str, skip: tuple[int, ...] = ()) -> None:
    """admin_log + report to the report chat (else to the other env admins)."""
    c.repo.log_admin(c.admin_id, c.group_id, action, details, now())
    report_chat = load_settings(c.repo, c.group_id).report_chat_id \
        if c.group_id is not None else None
    targets = [t for t in report_targets(report_chat, c.config.admin_ids)
               if t != c.admin_id and t not in skip]
    user = update.effective_user
    label = texts.user_label(c.admin_id, user.username if user else None)
    await notify(context.bot, targets, texts.admin_action(label, report))


# ---------- users ----------
async def cmd_whois(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    args = _args(update)
    if len(args) != 1:
        await _reply(update, texts.WHOIS_USAGE, html=True)
        return
    await _reply(update, whois_text(lookup(c.repo, c.group_id, args[0], now())))


async def cmd_unbind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    args = _args(update)
    if len(args) != 1:
        await _reply(update, texts.UNBIND_USAGE, html=True)
        return
    at = now()
    settings = load_settings(c.repo, c.group_id)
    b = unbind(c.repo, c.group_id, args[0], at, settings.deadline_hours)
    if b is None:
        await _reply(update, texts.UNBIND_NO_BINDING.format(query=args[0]))
        return
    user_id = b["user_id"]
    member = c.repo.get_member(c.group_id, user_id)
    text = texts.unbound(b["code"], user_id, b["username"], member["deadline_at"] if member else None)
    await _reply(update, text)
    _revoke_in_background(update, context, c.group_id, [user_id])
    try:
        await context.bot.send_message(user_id, texts.UNBOUND_USER_DM)
    except TelegramError as e:  # never started the bot / blocked it: silent
        log.info("unbind notice not delivered user=%s: %s", user_id, e)
    await _audit(update, context, c, "unbind",
                 {"code": b["code"], "user_id": user_id, "username": b["username"]}, text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    s = c.repo.stats(c.group_id, now())
    await _reply(update, texts.stats(s, registration_percent(s)))


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    args = _args(update)
    if len(args) != 1:
        await _reply(update, texts.UNLOCK_USAGE, html=True)
        return
    user_id = resolve_user_id(c.repo, c.group_id, args[0])
    if user_id is None:
        if _is_username(args[0]):
            await _reply(update, texts.UNLOCK_UNKNOWN_USERNAME.format(query=args[0]))
        else:
            await _reply(update, texts.UNLOCK_USAGE, html=True)
        return
    was_locked = c.repo.is_locked(c.group_id, user_id, now()) is not None
    c.repo.reset_attempts(c.group_id, user_id)
    text = texts.unlocked(user_id, was_locked)
    await _reply(update, text)
    await _audit(update, context, c, "unlock", {"user_id": user_id, "was_locked": was_locked},
                 text)


async def _user_names(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                      user_id: int) -> tuple[str | None, str | None]:
    """(username, full_name) from Telegram, or (None, None) if unknown."""
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError as e:
        log.info("get_chat_member failed chat=%s user=%s: %s", chat_id, user_id, e)
        return None, None
    return member.user.username, member.user.full_name


async def cmd_verify_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    try:
        user_id, code = parse_verify_user(_args(update))
    except UsageError as e:
        await _reply(update, str(e), html=True)
        return
    username, full_name = await _user_names(context, c.group_id, user_id)
    result = manual_verify(c.repo, c.group_id, user_id, code, username, full_name, now())
    if result.status == ManualStatus.BOUND_TO_OTHER:
        await _reply(update, texts.verify_user_bound_to_other(code, result.holder or -1))
        return
    await admit(context, c.group_id, user_id)
    text = texts.verify_user_done(code, user_id, result.added_to_list, result.prev_code,
                                  result.status == ManualStatus.ALREADY)
    await _reply(update, text)
    await _audit(update, context, c, "verify_user",
                 {"user_id": user_id, "code": code, "added_to_list": result.added_to_list,
                  "prev_code": result.prev_code}, text)


# ---------- settings ----------
async def cmd_set_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    try:
        action, text = parse_contact(_command_body(update))
    except UsageError as e:
        await _reply(update, str(e), html=True)
        return
    if action == "show":
        current = load_settings(c.repo, c.group_id).contact_text
        preview = texts.not_found(current, c.config.default_contact)
        await _reply(update, texts.contact_current(preview, custom=bool(current)), html=True)
        return
    c.repo.update_group_setting(c.group_id, "contact_text", text)
    reply = texts.contact_set(text) if text else texts.CONTACT_RESET
    await _reply(update, reply)
    await _audit(update, context, c, "set_contact", {"text": text}, reply)


async def cmd_set_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    args = _args(update)
    if not args:
        hours = load_settings(c.repo, c.group_id).deadline_hours
        await _reply(update, texts.deadline_current(hours), html=True)
        return
    try:
        hours = parse_deadline(args)
    except UsageError as e:
        await _reply(update, str(e), html=True)
        return
    if hours == 0:
        await _apply_deadline(update, context, c, 0)
        return
    # Removing members is opt-in: explain what happens and wait for an explicit "yes".
    token = secrets.token_hex(4)
    context.user_data[PENDING_DEADLINE] = {"token": token, "chat_id": c.group_id, "hours": hours}
    await update.effective_message.reply_text(
        texts.deadline_confirm(hours, c.repo.count_pending_without_deadline(c.group_id)),
        parse_mode=ParseMode.HTML,
        reply_markup=_keyboard((texts.BTN_DEADLINE_YES, f"ddl:yes:{token}"),
                               (texts.BTN_CANCEL, f"ddl:no:{token}")),
    )


async def on_deadline_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
        await query.answer()
        return
    c = await _guard(update, context)  # answers the query itself when it refuses
    if c is None:
        return
    _, action, token = (query.data.split(":") + ["", "", ""])[:3]
    pending = context.user_data.get(PENDING_DEADLINE)
    if not pending or pending["token"] != token or pending["chat_id"] != c.group_id:
        await query.answer(texts.EXPIRED, show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    context.user_data.pop(PENDING_DEADLINE, None)
    await query.answer()
    if action != "yes":
        await query.edit_message_text(texts.DEADLINE_CANCELLED)
        return
    await _apply_deadline(update, context, c, pending["hours"])


async def _apply_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE, c: Ctx,
                          hours: int) -> None:
    c.repo.update_group_setting(c.group_id, "deadline_hours", hours)
    if hours == 0:
        changed = c.repo.clear_pending_deadlines(c.group_id)
    else:
        changed = c.repo.fill_pending_deadlines(c.group_id, pending_deadline(hours, now()))
    text = texts.deadline_set(hours, changed)
    if update.callback_query is not None:  # confirmed with the button: show it in that message
        await update.callback_query.edit_message_text(text)
    else:
        await _reply(update, text)
    await _audit(update, context, c, "set_deadline", {"hours": hours, "members": changed}, text)


async def cmd_set_attempts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    args = _args(update)
    settings = load_settings(c.repo, c.group_id)
    if not args:
        await _reply(update, texts.attempts_current(settings.max_attempts, settings.lock_minutes),
                     html=True)
        return
    try:
        attempts, minutes = parse_attempts(args)
    except UsageError as e:
        await _reply(update, str(e), html=True)
        return
    c.repo.update_group_setting(c.group_id, "max_attempts", attempts)
    if minutes is not None:
        c.repo.update_group_setting(c.group_id, "lock_minutes", minutes)
    text = texts.attempts_set(attempts, minutes if minutes is not None else settings.lock_minutes)
    await _reply(update, text)
    await _audit(update, context, c, "set_attempts",
                 {"max_attempts": attempts, "lock_minutes": minutes}, text)


async def _apply_report(update: Update, context: ContextTypes.DEFAULT_TYPE, c: Ctx,
                        chat_id: int | None) -> None:
    c.repo.update_group_setting(c.group_id, "report_chat_id", chat_id)
    text = texts.report_set(chat_id) if chat_id else texts.REPORT_OFF
    await _reply(update, text)
    # the new report chat already got a message (the test / the reply): don't repeat
    await _audit(update, context, c, "set_report", {"chat_id": chat_id}, text,
                 skip=(chat_id,) if chat_id else ())


async def cmd_set_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Private: /set_report [chat_id|off]."""
    c = await _guard(update, context)
    if c is None:
        return
    try:
        action, chat_id = parse_report(_args(update))
    except UsageError as e:
        await _reply(update, str(e), html=True)
        return
    if action == "show":
        current = load_settings(c.repo, c.group_id).report_chat_id
        await _reply(update, texts.report_current(current), html=True)
        return
    if action == "set":
        if chat_id == c.group_id:
            await _reply(update, texts.REPORT_NOT_MAIN_GROUP)
            return
        try:  # proves the bot can post there before we rely on it
            await context.bot.send_message(chat_id, texts.REPORT_TEST)
        except TelegramError as e:
            await _reply(update, texts.report_send_failed(chat_id, str(e)))
            return
    await _apply_report(update, context, c, chat_id)


async def cmd_set_report_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A bot admin types /set_report [off] inside the admins' group. Others: silence."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or chat.type == ChatType.PRIVATE:
        return
    config: Config = context.bot_data[CONFIG_KEY]
    repo: Repo = context.bot_data[REPO_KEY]
    if not is_bot_admin(config, repo, user.id):
        return
    if config.group_id is None:
        await _reply(update, texts.NO_GROUP)
        return
    if chat.id == config.group_id:
        await _reply(update, texts.REPORT_NOT_MAIN_GROUP)
        return
    try:
        action, _ = parse_report(_args(update)[:1])
    except UsageError:
        action = "set"
    c = Ctx(config, repo, config.group_id, user.id)
    await _apply_report(update, context, c, None if action == "off" else chat.id)


async def on_channel_set_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Channel posts carry no sender, so we can't check admin rights there: just tell how
    to do it from private chat (the reply only reveals the channel's own id)."""
    post = update.channel_post
    if post is None or not post.text:
        return
    m = _CHANNEL_SET_REPORT.match(post.text)
    if m is None or (m.group(1) and context.bot.username
                     and m.group(1).casefold() != context.bot.username.casefold()):
        return
    try:
        await post.reply_text(texts.report_channel_hint(post.chat.id))
    except TelegramError as e:
        log.info("channel hint failed chat=%s: %s", post.chat.id, e)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    s = load_settings(c.repo, c.group_id)
    group = c.repo.get_group(c.group_id)
    await _reply(update, texts.settings(
        group_id=c.group_id, title=group["title"] if group else None,
        deadline_hours=s.deadline_hours, max_attempts=s.max_attempts,
        lock_minutes=s.lock_minutes, contact_text=s.contact_text,
        report_chat_id=s.report_chat_id, default_contact=c.config.default_contact))


# ---------- bot admins ----------
async def _set_commands(bot: Any, chat_id: int, commands: list[tuple[str, str]]) -> None:
    try:
        await bot.set_my_commands([BotCommand(n, d) for n, d in commands],
                                  scope=BotCommandScopeChat(chat_id))
    except TelegramError as e:
        log.info("set_my_commands failed chat=%s: %s", chat_id, e)


async def _change_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, add: bool) -> None:
    c = await _guard(update, context, need_group=False)
    if c is None:
        return
    args = _args(update)
    target = parse_user_id(args[0]) if len(args) == 1 else None
    if target is None:
        await _reply(update, texts.ADD_ADMIN_USAGE if add else texts.REMOVE_ADMIN_USAGE,
                     html=True)
        return
    result = change_admin(c.config, c.repo, c.admin_id, target, add, now())
    text = admin_change_text(result, target, add)
    await _reply(update, text)
    if result == AdminChange.ADDED:
        await _set_commands(context.bot, target, texts.ADMIN_COMMANDS)
    elif result == AdminChange.REMOVED:
        await _set_commands(context.bot, target, texts.USER_COMMANDS)
    else:
        return
    await _audit(update, context, c, "add_admin" if add else "remove_admin",
                 {"user_id": target}, text)


async def cmd_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _change_admin(update, context, add=True)


async def cmd_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _change_admin(update, context, add=False)


async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context, need_group=False)
    if c is None:
        return
    await _reply(update, texts.admins_list(c.config.admin_ids, c.repo.list_admins()))


# ---------- /logs, /help ----------
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context, need_group=False)
    if c is None:
        return
    try:
        n = parse_logs_n(_args(update))
    except UsageError as e:
        await _reply(update, str(e), html=True)
        return
    rows = c.repo.recent_admin_log(n)
    if not rows:
        await _reply(update, texts.LOGS_EMPTY)
        return
    for chunk in chunk_lines([texts.log_line(r) for r in rows]):
        await _reply(update, chunk)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.effective_message is None:
        return
    config: Config = context.bot_data[CONFIG_KEY]
    repo: Repo = context.bot_data[REPO_KEY]
    if is_bot_admin(config, repo, user.id):
        await _reply(update, texts.HELP_ADMIN, html=True)
    else:
        await _reply(update, texts.HELP_USER)


async def setup_commands(app: Application) -> None:
    """post_init: short menu for everyone, full menu for every bot admin. Errors are silent."""
    config: Config = app.bot_data[CONFIG_KEY]
    repo: Repo = app.bot_data[REPO_KEY]
    try:
        await app.bot.set_my_commands([BotCommand(n, d) for n, d in texts.USER_COMMANDS],
                                      scope=BotCommandScopeDefault())
    except TelegramError as e:
        log.info("set_my_commands (default) failed: %s", e)
    admin_ids = set(config.admin_ids) | {r["user_id"] for r in repo.list_admins()}
    for admin_id in sorted(admin_ids):
        await _set_commands(app.bot, admin_id, commands_for(config, admin_id))


def register(app: Application) -> None:
    for name, callback in (
        ("help", cmd_help), ("whois", cmd_whois), ("unbind", cmd_unbind),
        ("stats", cmd_stats), ("unlock", cmd_unlock), ("verify_user", cmd_verify_user),
        ("set_contact", cmd_set_contact), ("set_deadline", cmd_set_deadline),
        ("set_attempts", cmd_set_attempts), ("set_report", cmd_set_report),
        ("settings", cmd_settings), ("add_admin", cmd_add_admin),
        ("remove_admin", cmd_remove_admin), ("admins", cmd_admins), ("logs", cmd_logs),
    ):
        app.add_handler(CommandHandler(name, callback, filters=PRIVATE))
    app.add_handler(CallbackQueryHandler(on_deadline_choice, pattern=r"^ddl:"))
    app.add_handler(CommandHandler("set_report", cmd_set_report_in_group, filters=GROUP_MESSAGES))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.TEXT,
                                   on_channel_set_report))
