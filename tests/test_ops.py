"""Logging, the error handler's privacy, startup backup and the deploy files."""
import asyncio
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

from telegram import Chat, Message, Update, User

from bot import main as bot_main
from bot.config import PROJECT_ROOT, Config
from tests.conftest import CHAT


def test_setup_logging_rotating_file_and_stderr(tmp_path):
    handlers = bot_main.setup_logging(tmp_path / "logs")
    try:
        files = [h for h in handlers if isinstance(h, RotatingFileHandler)]
        assert len(files) == 1 and len(handlers) == 2
        assert files[0].maxBytes == 5 * 1024 * 1024 and files[0].backupCount == 5
        logging.getLogger("bot.test").warning("hello log")
        files[0].flush()
        assert "hello log" in (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8")
    finally:
        for h in handlers:
            logging.getLogger().removeHandler(h)
            h.close()


def test_error_handler_logs_only_ids(caplog):
    user = User(7, "Secret Name", False, username="secretuser")
    msg = Message(1, datetime(2026, 1, 1, tzinfo=timezone.utc), Chat(7, Chat.PRIVATE),
                  from_user=user, text="400111222 my private text")
    upd = Update(123, message=msg)
    ctx = SimpleNamespace(error=RuntimeError("boom"))
    with caplog.at_level(logging.ERROR):
        asyncio.run(bot_main.on_error(upd, ctx))
    assert "update_id=123" in caplog.text and "user_id=7" in caplog.text
    for secret in ("private text", "Secret Name", "secretuser", "400111222"):
        assert secret not in caplog.text
    assert bot_main.describe_update(None) == "NoneType"


def test_startup_backup(repo, tmp_path):
    config = Config(bot_token="x", admin_ids=frozenset(), db_path=str(tmp_path / "bot.db"),
                    group_id=CHAT)
    bot_main.startup_backup(config, repo)
    assert len(list((tmp_path / "backups").glob("bot-*.db"))) == 1


def test_run_bot_bat():
    bat = (PROJECT_ROOT / "run_bot.bat").read_text(encoding="utf-8")
    assert 'cd /d "%~dp0"' in bat and r".venv\Scripts\python.exe" in bat
    assert "timeout /t 10" in bat and "goto loop" in bat and "-m bot.main" in bat


def test_pytest_only_in_dev_requirements():
    prod = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    dev = (PROJECT_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "pytest" not in prod
    assert "-r requirements.txt" in dev and "pytest==" in dev


def test_gitignore_protects_student_data():
    lines = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("*.xlsx", "*.xlsm", "*.csv", "*.db-journal", "*.db-wal", "*.db-shm",
                    "logs/", ".env.*", "!.env.example"):
        assert pattern in lines
