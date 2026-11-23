"""Excel export of the student list and SQLite backups."""
from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from bot.repo import Repo

TEHRAN = ZoneInfo("Asia/Tehran")
KEEP_BACKUPS = 30
BACKUP_PREFIX = "bot-"

STUDENT_HEADERS = ["کد دانشجویی", "نام", "وضعیت ثبت", "user_id", "username", "زمان ثبت (تهران)"]
UNREGISTERED_HEADERS = ["کد دانشجویی", "نام"]


def tehran_time(epoch: int | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, TEHRAN).strftime("%Y-%m-%d %H:%M")


def _sheet(wb: Workbook, title: str, headers: list[str], first: bool):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.sheet_view.rightToLeft = True
    ws.append(headers)
    ws.freeze_panes = "A2"
    return ws


def export_xlsx(repo: Repo, chat_id: int) -> bytes:
    """Two sheets: all students with registration info, and the unregistered ones.
    Codes are written as text so leading zeros survive."""
    wb = Workbook()
    all_ws = _sheet(wb, "دانشجوها", STUDENT_HEADERS, first=True)
    un_ws = _sheet(wb, "ثبت‌نشده‌ها", UNREGISTERED_HEADERS, first=False)
    for r in repo.students_with_bindings(chat_id):
        bound = r["user_id"] is not None
        username = f"@{r['username']}" if r["username"] else ""
        all_ws.append([r["code"], r["full_name"] or "", "ثبت‌شده" if bound else "ثبت‌نشده",
                       r["user_id"], username, tehran_time(r["bound_at"])])
        if not bound:
            un_ws.append([r["code"], r["full_name"] or ""])
    for ws in (all_ws, un_ws):
        for cell in ws["A"]:
            cell.number_format = "@"
        ws.column_dimensions["A"].width = 16
        ws.column_dimensions["B"].width = 30
    for col, width in zip("CDEF", (12, 14, 20, 18)):
        all_ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def backup_db(conn: sqlite3.Connection, backups_dir: str | Path, now: int) -> Path:
    """Consistent copy via the sqlite backup API into backups_dir/bot-YYYYmmdd-HHMMSS.db (UTC).
    Only the newest KEEP_BACKUPS files are kept."""
    folder = Path(backups_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = folder / f"{BACKUP_PREFIX}{stamp}.db"
    n = 0
    while path.exists():  # several backups in the same second; '_' sorts after '.'
        n += 1
        path = folder / f"{BACKUP_PREFIX}{stamp}_{n:03d}.db"
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    prune_backups(folder)
    return path


def prune_backups(folder: Path, keep: int = KEEP_BACKUPS) -> list[Path]:
    files = sorted(folder.glob(f"{BACKUP_PREFIX}*.db"), key=lambda p: p.name)
    old = files[:-keep] if keep > 0 else files
    for p in old:
        p.unlink(missing_ok=True)
    return old
