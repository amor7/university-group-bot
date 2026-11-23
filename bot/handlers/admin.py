"""Admin commands (private chat only): file import, /add, /remove, /clear_list, /export, /backup."""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from bot import texts
from bot.actions import revoke_users
from bot.config import Config
from bot.exporter import TEHRAN, backup_db, export_xlsx
from bot.importer import ImportFileError, ImportResult, parse_codes_text, parse_file
from bot.normalize import normalize_code
from bot.repo import Repo

log = logging.getLogger(__name__)

CONFIG_KEY = "config"
REPO_KEY = "repo"
PENDING_IMPORT = "pending_import"
PENDING_CLEAR = "pending_clear"
MAX_FILE_MB = 10
ALLOWED_EXT = (".xlsx", ".xlsm", ".csv")

# New messages in private chats only (edited messages must not re-run a command).
PRIVATE = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE


# ---------- pure helpers ----------
def is_bot_admin(config: Config, repo: Repo, user_id: int | None) -> bool:
    """Admin = listed in ADMIN_IDS (.env) or added to bot_admins in the DB."""
    if user_id is None:
        return False
    return user_id in config.admin_ids or repo.is_admin(user_id)


def backups_dir(config: Config) -> Path:
    return Path(config.db_path).resolve().parent / "backups"


def now() -> int:
    return int(time.time())


def build_preview(repo: Repo, chat_id: int, result: ImportResult, filename: str) -> str:
    current = repo.list_codes(chat_id)
    incoming = set(result.codes)
    to_remove = current - incoming
    return texts.import_preview(
        filename=filename, valid=len(incoming), new=len(incoming - current), in_db=len(current),
        to_remove=len(to_remove), to_revoke=repo.count_bound_codes(chat_id, to_remove),
        duplicates=result.duplicates, bad=result.bad, length_counts=result.length_counts,
        has_header=result.has_header,
    )


def add_rows(repo: Repo, chat_id: int, rows: list[tuple[str, str | None]]) -> tuple[list[str], list[str]]:
    """Returns (added_codes, already_listed_codes)."""
    before = repo.list_codes(chat_id)
    repo.add_students(chat_id, rows)
    added = [c for c, _ in rows if c not in before]
    existing = [c for c, _ in rows if c in before]
    return added, existing


def split_codes(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,،;]+", text or "") if t]


def remove_codes(repo: Repo, chat_id: int, raw_codes: list[str]) -> tuple[list[str], list[str], list[Any]]:
    """Returns (removed_codes, missing_codes, removed_bindings)."""
    removed: list[str] = []
    missing: list[str] = []
    bindings: list[Any] = []
    seen: set[str] = set()
    for raw in raw_codes:
        code = normalize_code(raw)
        if not code or code in seen:
            continue
        seen.add(code)
        ok, b = repo.remove_student(chat_id, code)
        (removed if ok else missing).append(code)
        bindings += b
    return removed, missing, bindings


# ---------- guards ----------
@dataclass
class Ctx:
    config: Config
    repo: Repo
    group_id: int  # None only when _guard(need_group=False) and GROUP_ID is unset
    admin_id: int


async def _say(update: Update, text: str) -> None:
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)


def _revoke_in_background(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          chat_id: int, user_ids: list[int]) -> None:
    """Restricting many users is slow (rate-limited); don't block other updates meanwhile."""
    if user_ids:
        context.application.create_task(
            revoke_users(context.bot, chat_id, user_ids,
                         admin_ids=context.bot_data[CONFIG_KEY].admin_ids),
            update=update, name="revoke_users")


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE,
                 need_group: bool = True) -> Ctx | None:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or chat.type != ChatType.PRIVATE or user is None:
        return None
    config: Config = context.bot_data[CONFIG_KEY]
    repo: Repo = context.bot_data[REPO_KEY]
    if not is_bot_admin(config, repo, user.id):
        await _say(update, texts.NO_ACCESS)
        return None
    if need_group and config.group_id is None:
        await _say(update, texts.NO_GROUP)
        return None
    return Ctx(config, repo, config.group_id, user.id)  # type: ignore[arg-type]


def _command_body(update: Update) -> str:
    """Text after the command, newlines preserved (context.args would lose them)."""
    text = update.effective_message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _ids(bindings: list[Any]) -> list[int]:
    return [b["user_id"] for b in bindings]


def _keyboard(*buttons: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in buttons]])


# ---------- file import ----------
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    msg = update.effective_message
    doc = msg.document
    filename = doc.file_name or ""
    if not filename.lower().endswith(ALLOWED_EXT):
        await msg.reply_text(texts.IMPORT_BAD_TYPE)
        return
    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await msg.reply_text(texts.IMPORT_TOO_BIG.format(max_mb=texts.fa_num(MAX_FILE_MB)))
        return
    tg_file = await doc.get_file()
    data = bytes(await tg_file.download_as_bytearray())
    try:
        result = parse_file(filename, data)
    except ImportFileError as e:
        await msg.reply_text(texts.IMPORT_FAILED.format(reason=e))
        return
    if not result.rows:
        await msg.reply_text(texts.IMPORT_NO_VALID)
        return
    token = secrets.token_hex(4)
    context.user_data[PENDING_IMPORT] = {
        "token": token, "rows": result.rows, "filename": filename, "chat_id": c.group_id,
    }
    await msg.reply_text(
        build_preview(c.repo, c.group_id, result, filename),
        reply_markup=_keyboard((texts.BTN_ADD, f"imp:add:{token}"),
                               (texts.BTN_REPLACE, f"imp:replace:{token}"),
                               (texts.BTN_CANCEL, f"imp:cancel:{token}")),
    )


async def _on_import_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, c: Ctx,
                            action: str, token: str) -> None:
    query = update.callback_query
    pending = context.user_data.get(PENDING_IMPORT)
    if not pending or pending["token"] != token or pending["chat_id"] != c.group_id:
        await query.answer(texts.EXPIRED, show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    context.user_data.pop(PENDING_IMPORT, None)
    await query.answer()
    rows, filename = pending["rows"], pending["filename"]
    if action == "add":
        added, existing = add_rows(c.repo, c.group_id, rows)
        c.repo.log_admin(c.admin_id, c.group_id, "import_add",
                         {"file": filename, "added": len(added), "existing": len(existing)}, now())
        await query.edit_message_text(texts.import_added(len(added), len(existing)))
    elif action == "replace":
        backup = backup_db(c.repo.conn, backups_dir(c.config), now())
        count, _dups, removed = c.repo.replace_students(c.group_id, rows)
        c.repo.log_admin(c.admin_id, c.group_id, "import_replace",
                         {"file": filename, "count": count, "revoked": _ids(removed),
                          "backup": backup.name}, now())
        await query.edit_message_text(texts.import_replaced(count, removed, backup.name))
        _revoke_in_background(update, context, c.group_id, _ids(removed))
    else:
        c.repo.log_admin(c.admin_id, c.group_id, "import_cancel", {"file": filename}, now())
        await query.edit_message_text(texts.CANCELLED)


# ---------- /add, /remove ----------
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    body = _command_body(update)
    result = parse_codes_text(body)
    if not result.rows and not result.bad:
        await update.effective_message.reply_text(texts.ADD_USAGE, parse_mode=ParseMode.HTML)
        return
    added, existing = add_rows(c.repo, c.group_id, result.rows)
    c.repo.log_admin(c.admin_id, c.group_id, "add",
                     {"added": added, "existing": existing,
                      "bad": [b.value for b in result.bad]}, now())
    await update.effective_message.reply_text(texts.add_report(added, existing, result.bad))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    raw = split_codes(_command_body(update))
    if not raw:
        await update.effective_message.reply_text(texts.REMOVE_USAGE, parse_mode=ParseMode.HTML)
        return
    removed, missing, bindings = remove_codes(c.repo, c.group_id, raw)
    c.repo.log_admin(c.admin_id, c.group_id, "remove",
                     {"removed": removed, "missing": missing, "revoked": _ids(bindings)}, now())
    await update.effective_message.reply_text(texts.remove_report(removed, missing, bindings))
    _revoke_in_background(update, context, c.group_id, _ids(bindings))


# ---------- /clear_list ----------
async def cmd_clear_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    codes = c.repo.list_codes(c.group_id)
    if not codes:
        await update.effective_message.reply_text(texts.CLEAR_EMPTY)
        return
    token = secrets.token_hex(4)
    context.user_data[PENDING_CLEAR] = {"token": token, "chat_id": c.group_id}
    await update.effective_message.reply_text(
        texts.clear_confirm(len(codes), c.repo.count_bound_codes(c.group_id, codes)),
        reply_markup=_keyboard((texts.BTN_CLEAR_YES, f"clr:yes:{token}"),
                               (texts.BTN_CANCEL, f"clr:no:{token}")),
    )


async def _on_clear_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, c: Ctx,
                           action: str, token: str) -> None:
    query = update.callback_query
    pending = context.user_data.get(PENDING_CLEAR)
    if not pending or pending["token"] != token or pending["chat_id"] != c.group_id:
        await query.answer(texts.EXPIRED, show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    context.user_data.pop(PENDING_CLEAR, None)
    await query.answer()
    if action != "yes":
        c.repo.log_admin(c.admin_id, c.group_id, "clear_cancel", None, now())
        await query.edit_message_text(texts.CANCELLED)
        return
    backup = backup_db(c.repo.conn, backups_dir(c.config), now())
    count, removed = c.repo.clear_students(c.group_id)
    c.repo.log_admin(c.admin_id, c.group_id, "clear_list",
                     {"count": count, "revoked": _ids(removed), "backup": backup.name}, now())
    await query.edit_message_text(texts.cleared(count, removed, backup.name))
    _revoke_in_background(update, context, c.group_id, _ids(removed))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.type != ChatType.PRIVATE:
        await update.callback_query.answer()  # _guard stays silent outside private chats
        return
    c = await _guard(update, context)  # answers the query itself when it refuses
    if c is None:
        return
    kind, action, token = (update.callback_query.data.split(":") + ["", "", ""])[:3]
    if kind == "imp":
        await _on_import_choice(update, context, c, action, token)
    elif kind == "clr":
        await _on_clear_choice(update, context, c, action, token)


# ---------- /export, /backup ----------
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    data = export_xlsx(c.repo, c.group_id)
    count = len(c.repo.list_codes(c.group_id))
    stamp = datetime.fromtimestamp(now(), TEHRAN).strftime("%Y%m%d-%H%M")
    c.repo.log_admin(c.admin_id, c.group_id, "export", {"count": count}, now())
    await update.effective_message.reply_document(
        document=data, filename=f"students-{stamp}.xlsx",
        caption=texts.EXPORT_CAPTION.format(count=texts.fa_num(count)),
    )


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = await _guard(update, context)
    if c is None:
        return
    path = backup_db(c.repo.conn, backups_dir(c.config), now())
    c.repo.log_admin(c.admin_id, c.group_id, "backup", {"file": path.name}, now())
    with path.open("rb") as f:
        await update.effective_message.reply_document(
            document=f, filename=path.name, caption=texts.BACKUP_CAPTION)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("add", cmd_add, filters=PRIVATE))
    app.add_handler(CommandHandler("remove", cmd_remove, filters=PRIVATE))
    app.add_handler(CommandHandler("clear_list", cmd_clear_list, filters=PRIVATE))
    app.add_handler(CommandHandler("export", cmd_export, filters=PRIVATE))
    app.add_handler(CommandHandler("backup", cmd_backup, filters=PRIVATE))
    app.add_handler(MessageHandler(PRIVATE & filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(imp|clr):"))
