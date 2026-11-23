"""Minute sweeper: kick members whose registration deadline passed; delete expired bot messages."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, ContextTypes

from bot.actions import retry_seconds
from bot.exporter import backup_db
from bot.handlers.admin import CONFIG_KEY, REPO_KEY, backups_dir
from bot.repo import Repo
from bot.verify import load_settings

log = logging.getLogger(__name__)

def now() -> int:
    return int(time.time())


SWEEP_INTERVAL = 60  # seconds
KICK_GAP = 0.2              # seconds between kicks (Telegram rate limits)
MAX_KICKS_PER_ROUND = 50
UNBAN_RETRIES = 3           # extra unban tries right after a kick
BACKUP_INTERVAL = 24 * 3600
BACKUP_FIRST = BACKUP_INTERVAL  # main._post_init already takes one at startup


def should_kick(member: Any, has_binding: bool, now: int) -> bool:
    """Only pending members past their deadline who still have no binding.
    Verified members are never kicked; revoked members are restricted, not kicked
    (decision 2 in PLAN.md), and the repo keeps their deadline NULL anyway."""
    if member is None or has_binding:
        return False
    return (member["status"] == "pending" and member["deadline_at"] is not None
            and member["deadline_at"] <= now)


def pick_kicks(repo: Repo, now: int) -> list[tuple[int, int]]:
    """(chat_id, user_id) pairs to kick now, re-checked against the current rows."""
    out: list[tuple[int, int]] = []
    kick_on: dict[int, bool] = {}  # deadline_hours = 0 turns kicking off for that group
    for row in repo.due_for_kick(now):
        chat_id, user_id = row["chat_id"], row["user_id"]
        if chat_id not in kick_on:
            kick_on[chat_id] = load_settings(repo, chat_id).deadline_hours > 0
        if not kick_on[chat_id]:
            continue
        if should_kick(row, repo.get_binding_by_user(chat_id, user_id) is not None, now):
            out.append((chat_id, user_id))
    return out


def _still_kickable(repo: Repo, chat_id: int, user_id: int, now: int) -> bool:
    """Re-check right before the API call: the user may have verified meanwhile."""
    return should_kick(repo.get_member(chat_id, user_id),
                       repo.get_binding_by_user(chat_id, user_id) is not None, now)


def _forget(repo: Repo, chat_id: int, user_id: int) -> None:
    """Drop the member row after a kick, re-reading it first: a user who verified while we
    awaited the API keeps their row (only the unban flag is cleared)."""
    member = repo.get_member(chat_id, user_id)
    if member is None:
        return
    if member["status"] == "verified" or repo.get_binding_by_user(chat_id, user_id) is not None:
        repo.set_unban_pending(chat_id, user_id, False)
    else:
        repo.delete_member(chat_id, user_id)


async def _unban(bot: Any, chat_id: int, user_id: int, tries: int) -> bool:
    for i in range(tries):
        if i:
            await asyncio.sleep(KICK_GAP)
        try:
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            return True
        except RetryAfter as e:
            await asyncio.sleep(retry_seconds(e))
        except TelegramError as e:
            log.warning("unban failed chat=%s user=%s (try %s/%s): %s",
                        chat_id, user_id, i + 1, tries, e)
    return False


async def retry_unbans(bot: Any, repo: Repo, now: int) -> int:
    """Members banned by an earlier round whose unban failed: try only the unban again."""
    done = 0
    for row in repo.due_unbans():
        chat_id, user_id = row["chat_id"], row["user_id"]
        try:
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        except RetryAfter as e:
            await asyncio.sleep(retry_seconds(e))
            return done
        except TelegramError as e:
            log.error("unban retry failed chat=%s user=%s (user stays banned!): %s",
                      chat_id, user_id, e)
            continue
        _forget(repo, chat_id, user_id)
        done += 1
        await asyncio.sleep(KICK_GAP)
    return done


async def kick_due(bot: Any, repo: Repo, now: int) -> int:
    """Kick (ban + unban) at most MAX_KICKS_PER_ROUND members, KICK_GAP apart. A flood
    error (RetryAfter) ends the round after waiting. Returns how many were kicked."""
    kicked = attempts = 0
    for chat_id, user_id in pick_kicks(repo, now):
        if attempts >= MAX_KICKS_PER_ROUND:
            break
        if not _still_kickable(repo, chat_id, user_id, now):
            continue
        if attempts:
            await asyncio.sleep(KICK_GAP)
        attempts += 1
        try:
            await bot.ban_chat_member(chat_id, user_id)
        except RetryAfter as e:
            log.warning("kick flood-limited; waiting %ss and ending this round",
                        retry_seconds(e))
            await asyncio.sleep(retry_seconds(e))
            break
        except (BadRequest, Forbidden) as e:
            # e.g. user already gone or no rights: forget the row so we don't retry forever
            log.warning("kick failed chat=%s user=%s: %s", chat_id, user_id, e)
            _forget(repo, chat_id, user_id)
            continue
        except TelegramError as e:  # network: try again next round
            log.warning("kick failed (will retry) chat=%s user=%s: %s", chat_id, user_id, e)
            continue
        if await _unban(bot, chat_id, user_id, 1 + UNBAN_RETRIES):
            _forget(repo, chat_id, user_id)
        else:
            log.error("unban after kick failed chat=%s user=%s; retrying next round",
                      chat_id, user_id)
            repo.set_unban_pending(chat_id, user_id, True)
        kicked += 1
    return kicked


async def cleanup_due(bot: Any, repo: Repo, now: int) -> int:
    deleted = 0
    for row in repo.due_cleanup(now):
        chat_id, message_id = row["chat_id"], row["message_id"]
        try:
            await bot.delete_message(chat_id, message_id)
            deleted += 1
        except RetryAfter as e:  # flood limit: wait, then leave the rest for the next round
            await asyncio.sleep(retry_seconds(e))
            break
        except (BadRequest, Forbidden) as e:  # already deleted / too old: give up on it
            log.info("cleanup delete failed chat=%s msg=%s: %s", chat_id, message_id, e)
        except TelegramError as e:
            log.warning("cleanup delete failed (will retry) chat=%s msg=%s: %s",
                        chat_id, message_id, e)
            continue
        repo.delete_cleanup(chat_id, message_id)
    return deleted


async def sweep(context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: Repo = context.bot_data[REPO_KEY]
    at = int(time.time())
    for name, step in (("unban", retry_unbans), ("kick", kick_due), ("cleanup", cleanup_due)):
        try:
            await step(context.bot, repo, at)
        except Exception:  # never let one bad round stop the sweeper
            log.exception("sweeper %s step failed", name)


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily database backup (the startup one is taken in main._post_init)."""
    repo: Repo = context.bot_data[REPO_KEY]
    try:
        path = backup_db(repo.conn, backups_dir(context.bot_data[CONFIG_KEY]), now())
        log.info("backup written: %s", path.name)
    except Exception:
        log.exception("scheduled backup failed")


def register(app: Application) -> None:
    if app.job_queue is None:
        raise RuntimeError("JobQueue missing: install python-telegram-bot[job-queue]")
    app.job_queue.run_repeating(sweep, interval=SWEEP_INTERVAL, first=10, name="sweeper")
    app.job_queue.run_repeating(backup_job, interval=BACKUP_INTERVAL, first=BACKUP_FIRST,
                                name="backup")
