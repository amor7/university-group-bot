"""Entry point: python -m bot.main"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes

from bot import jobs
from bot.config import PROJECT_ROOT, Config, ConfigError, load_config
from bot.exporter import backup_db
from bot.db import connect
from bot.handlers import admin, group, join, manage, private
from bot.repo import Repo

log = logging.getLogger(__name__)


# Explicit list (instead of ALL_TYPES): chat_member and chat_join_request are only
# delivered when requested. channel_post: only for the /set_report hint in channels.
ALLOWED_UPDATES = [
    Update.MESSAGE, Update.EDITED_MESSAGE, Update.CALLBACK_QUERY,
    Update.CHAT_MEMBER, Update.MY_CHAT_MEMBER, Update.CHAT_JOIN_REQUEST, Update.CHANNEL_POST,
]


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5


def setup_logging(log_dir: Path = PROJECT_ROOT / "logs") -> list[logging.Handler]:
    """logs/bot.log (rotating, 5 MB x 5) plus stderr. Returns the added handlers."""
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_dir / "bot.log", maxBytes=LOG_MAX_BYTES,
                            backupCount=LOG_BACKUPS, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log URLs containing the token
    return handlers


def describe_update(update: object) -> str:
    """Only ids: the full update would put students' messages and names into the log."""
    if not isinstance(update, Update):
        return type(update).__name__
    user = update.effective_user
    return f"update_id={update.update_id} user_id={user.id if user else None}"


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error for %s", describe_update(update), exc_info=context.error)


def startup_backup(config: Config, repo: Repo) -> None:
    try:
        path = backup_db(repo.conn, admin.backups_dir(config), jobs.now())
        log.info("startup backup written: %s", path.name)
    except Exception:  # never block startup on a backup problem
        log.exception("startup backup failed")


async def _post_init(app: Application) -> None:
    startup_backup(app.bot_data[admin.CONFIG_KEY], app.bot_data[admin.REPO_KEY])
    await group.check_bot_rights(app)
    await manage.setup_commands(app)


async def _close_db(app: Application) -> None:
    repo: Repo | None = app.bot_data.get(admin.REPO_KEY)
    if repo is not None:
        repo.conn.close()


def build_application(config: Config) -> Application:
    # concurrent_updates stays off: one SQLite connection, updates handled one at a time.
    app = (ApplicationBuilder().token(config.bot_token)
           .post_init(_post_init).post_shutdown(_close_db).build())
    app.bot_data[admin.CONFIG_KEY] = config
    app.bot_data[admin.REPO_KEY] = Repo(connect(config.db_path))
    register_handlers(app, config)
    jobs.register(app)
    app.add_error_handler(on_error)
    return app


def register_handlers(app: Application, config: Config) -> None:
    """Order matters.
    group -1 (group.GATE_GROUP): the group gate; stops further handling of gated messages.
    group 0, first match wins: /start, admin commands/files/buttons, then the private
    code-text handler LAST, so it only gets plain text no admin handler took.
    Group-only handlers (chat_member, my_chat_member, join requests) see other update types.
    """
    if config.group_id is not None:
        group.register(app, config.group_id)
        join.register(app, config.group_id)
    private.register(app)
    admin.register(app)
    manage.register(app)
    private.register_text(app)


def main() -> None:
    setup_logging()
    try:
        config = load_config()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)
    if config.group_id is None:
        log.warning("GROUP_ID is not set; the group gate is disabled and admin commands "
                    "will ask for it.")
    build_application(config).run_polling(allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    main()
