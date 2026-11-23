"""Join requests: approve registered students; ask everyone else for their code in private."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ChatJoinRequestHandler, ContextTypes

from bot import texts
from bot.config import Config
from bot.handlers.admin import CONFIG_KEY, REPO_KEY
from bot.handlers.private import AWAIT_CODE
from bot.repo import Repo

log = logging.getLogger(__name__)


def is_registered(repo: Repo, chat_id: int, user_id: int) -> bool:
    """Has a binding whose code is still listed, and is not revoked."""
    binding = repo.get_binding_by_user(chat_id, user_id)
    if binding is None or repo.find_student(chat_id, binding["code"]) is None:
        return False
    member = repo.get_member(chat_id, user_id)
    return member is None or member["status"] != "revoked"


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data[CONFIG_KEY]
    repo: Repo = context.bot_data[REPO_KEY]
    req = update.chat_join_request
    if req is None or req.chat.id != config.group_id:
        return
    user = req.from_user
    if is_registered(repo, req.chat.id, user.id):
        try:
            await req.approve()
        except TelegramError as e:
            log.warning("approve failed chat=%s user=%s: %s", req.chat.id, user.id, e)
        return
    # Not a member yet: no pending row. The request stays open until verify approves it.
    context.user_data[AWAIT_CODE] = True
    try:
        await context.bot.send_message(req.user_chat_id, texts.JOIN_ASK_CODE)
    except TelegramError as e:
        log.warning("could not message join requester user=%s: %s", user.id, e)


def register(app: Application, group_id: int) -> None:
    app.add_handler(ChatJoinRequestHandler(on_join_request, chat_id=group_id))
