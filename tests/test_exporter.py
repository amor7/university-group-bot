import io
import sqlite3

import openpyxl

from bot.exporter import KEEP_BACKUPS, backup_db, export_xlsx
from tests.conftest import CHAT

# 2026-11-23 08:00:00 UTC == 11:30 Tehran (UTC+3:30, no DST)
T = 1795420800


def test_export_xlsx_roundtrip(repo):
    repo.add_students(CHAT, [("012345678", "نام یک"), ("40011122233", None), ("500000001", "سه")])
    repo.add_students(-999, [("777777777", "گروه دیگر")])
    repo.bind(CHAT, 42, "40011122233", "@tester", "T", now=T)
    repo.set_status(CHAT, 42, "verified", now=T)

    wb = openpyxl.load_workbook(io.BytesIO(export_xlsx(repo, CHAT)))
    assert wb.sheetnames == ["دانشجوها", "ثبت‌نشده‌ها"]
    rows = list(wb["دانشجوها"].iter_rows(values_only=True))
    assert rows[0][0] == "کد دانشجویی"
    assert rows[1] == ("012345678", "نام یک", "ثبت‌نشده", None, None, None)
    assert rows[2] == ("40011122233", None, "ثبت‌شده", 42, "@tester", "2026-11-23 11:30")
    assert len(rows) == 4
    unreg = list(wb["ثبت‌نشده‌ها"].iter_rows(values_only=True))[1:]
    assert unreg == [("012345678", "نام یک"), ("500000001", "سه")]


def test_backup_db_and_rotation(repo, tmp_path):
    repo.add_students(CHAT, [("500000001", "x")])
    first = backup_db(repo.conn, tmp_path / "backups", now=T)
    copy = sqlite3.connect(first)
    assert copy.execute("SELECT code FROM students").fetchall() == [("500000001",)]
    copy.close()

    same_second = backup_db(repo.conn, tmp_path / "backups", now=T)
    assert same_second != first and same_second.exists()

    paths = [backup_db(repo.conn, tmp_path / "backups", now=T + i) for i in range(1, 35)]
    left = sorted(p.name for p in (tmp_path / "backups").glob("*.db"))
    assert len(left) == KEEP_BACKUPS
    assert left == sorted(p.name for p in paths[-KEEP_BACKUPS:])
    assert not first.exists()
