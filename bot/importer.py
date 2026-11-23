"""Pure parsing of student list files (xlsx / csv) and of free text typed by an admin.

Nothing here touches the network or the database. Row numbers in reports are 1-based
and match what the admin sees in Excel (sheet row) or in a text editor (csv / text line).
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from bot.normalize import normalize_digits, normalize_name

CODE_MIN_LEN = 6
CODE_MAX_LEN = 15
HEADER_SCAN_ROWS = 10  # header may sit below a title row or two
MAX_ROWS = 100_000

_CODE_RE = re.compile(r"[0-9]+")


class ImportFileError(Exception):
    """The file as a whole cannot be used (bad type, unreadable, no code column)."""


@dataclass
class BadRow:
    row: int
    value: str
    reason: str


@dataclass
class ImportResult:
    rows: list[tuple[str, str | None]] = field(default_factory=list)  # valid (code, name)
    bad: list[BadRow] = field(default_factory=list)
    duplicates: list[tuple[int, str]] = field(default_factory=list)  # (row, code), later copies
    empty_rows: int = 0
    has_header: bool = True
    sheet: str | None = None

    @property
    def length_counts(self) -> dict[int, int]:
        """{code length: how many valid codes} sorted by length."""
        return dict(sorted(Counter(len(c) for c, _ in self.rows).items()))

    @property
    def mixed_lengths(self) -> bool:
        """Informational only: real lists legitimately mix 9- and 11-digit codes."""
        return len(self.length_counts) > 1

    @property
    def codes(self) -> list[str]:
        return [c for c, _ in self.rows]


# ---------- headers ----------
def normalize_header(text: Any) -> str:
    """ي→ی, ك→ک, digits/letters unified, and spaces, '_', '-', ZWNJ removed; casefolded."""
    text = normalize_name("" if text is None else str(text))
    return re.sub(r"[_\-‐-―.:/\\()]", "", text)


def _keys(*names: str) -> frozenset[str]:
    return frozenset(normalize_header(n) for n in names)


CODE_HEADERS = _keys(
    "شماره دانشجویی", "شماره‌ی دانشجویی", "شماره دانشجو", "کد دانشجویی", "کد دانشجو",
    "شماره‌ دانشجويي", "شماره_دانشجويي", "student_id", "student id", "student code",
    "student no", "student number", "code", "id",
)
FULL_NAME_HEADERS = _keys(
    "نام و نام خانوادگی", "نام خانوادگی و نام", "نام‌ونام‌خانوادگی", "name", "full name",
    "full_name", "fullname", "student name",
)
LAST_NAME_HEADERS = _keys("نام خانوادگی", "فامیل", "فامیلی", "last name", "family name", "surname")
FIRST_NAME_HEADERS = _keys("نام", "first name", "firstname")


@dataclass(frozen=True)
class _Layout:
    header_index: int | None  # index into rows of the header row; None = no header
    code_col: int
    name_col: int | None = None
    last_col: int | None = None
    first_col: int | None = None


def _find_header(rows: list[list[Any]]) -> _Layout | None:
    for i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        heads = [normalize_header(v) for v in row]
        code_col = next((j for j, h in enumerate(heads) if h in CODE_HEADERS), None)
        if code_col is None:
            continue

        def find(keys: frozenset[str]) -> int | None:
            return next((j for j, h in enumerate(heads) if h in keys and j != code_col), None)

        return _Layout(i, code_col, find(FULL_NAME_HEADERS), find(LAST_NAME_HEADERS),
                       find(FIRST_NAME_HEADERS))
    return None


def _headerless_layout(rows: list[list[Any]]) -> _Layout | None:
    """No header found: accept the file if the first non-empty row starts with a code."""
    for row in rows:
        if _is_empty(row):
            continue
        code, err = clean_code(row[0] if row else None)
        if err is not None:
            return None
        name_col = 1 if any(len(r) > 1 and cell_text(r[1]) for r in rows) else None
        return _Layout(None, 0, name_col)
    return None


# ---------- cells ----------
def cell_text(value: Any) -> str:
    """Cell → stripped text. 4001.0 → '4001'; None → ''."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value).strip()


def clean_code(value: Any) -> tuple[str, str | None]:
    """Returns (code, None) when valid, else (raw text, reason)."""
    raw = cell_text(value)
    text = normalize_digits(unicodedata.normalize("NFKC", raw))
    text = "".join(ch for ch in text if not ch.isspace() and unicodedata.category(ch) != "Cf")
    if not text:
        return raw, "کد خالی است"
    if not (text.isascii() and _CODE_RE.fullmatch(text)):
        return raw, "کد فقط باید رقم باشد"
    if not CODE_MIN_LEN <= len(text) <= CODE_MAX_LEN:
        return raw, f"طول کد {len(text)} رقم است (مجاز: {CODE_MIN_LEN} تا {CODE_MAX_LEN})"
    return text, None


def clean_name(value: Any) -> str:
    return " ".join(cell_text(value).split())


def _is_empty(row: list[Any]) -> bool:
    return all(not cell_text(v) for v in row)


def _get(row: list[Any], col: int | None) -> Any:
    return row[col] if col is not None and col < len(row) else None


def _name_of(row: list[Any], lay: _Layout) -> str | None:
    if lay.name_col is not None:
        name = clean_name(_get(row, lay.name_col))
    else:
        last = clean_name(_get(row, lay.last_col))
        first = clean_name(_get(row, lay.first_col))
        name = " - ".join(p for p in (last, first) if p)
    return name or None


# ---------- core ----------
class _Collector:
    def __init__(self, result: ImportResult) -> None:
        self.result = result
        self.seen: set[str] = set()

    def add(self, row_no: int, raw_code: Any, name: str | None) -> None:
        code, err = clean_code(raw_code)
        if err is not None:
            self.result.bad.append(BadRow(row_no, code, err))
        elif code in self.seen:
            self.result.duplicates.append((row_no, code))
        else:
            self.seen.add(code)
            self.result.rows.append((code, name))


def _parse_rows(rows: list[list[Any]], layout: _Layout, sheet: str | None) -> ImportResult:
    result = ImportResult(has_header=layout.header_index is not None, sheet=sheet)
    collector = _Collector(result)
    start = 0 if layout.header_index is None else layout.header_index + 1
    for idx in range(start, len(rows)):
        row = rows[idx]
        if _is_empty(row):
            result.empty_rows += 1
            continue
        collector.add(idx + 1, _get(row, layout.code_col), _name_of(row, layout))
    return result


def _layout_for(rows: list[list[Any]]) -> _Layout | None:
    return _find_header(rows) or _headerless_layout(rows)


def _read_xlsx(data: bytes) -> ImportResult:
    import openpyxl  # local import keeps csv-only use light

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as e:
        raise ImportFileError("فایل اکسل خراب است یا xlsx نیست") from e
    try:
        sheets: list[tuple[str, list[list[Any]]]] = []
        for ws in wb.worksheets:
            rows: list[list[Any]] = []
            for values in ws.iter_rows(values_only=True):
                rows.append(list(values))
                if len(rows) > MAX_ROWS:
                    raise ImportFileError("تعداد ردیف‌ها بیش از حد است")
            sheets.append((ws.title, rows))
    finally:
        wb.close()
    if not sheets:
        raise ImportFileError("فایل اکسل هیچ شیتی ندارد")
    # First sheet that has a code header; otherwise the first headerless sheet that looks right.
    for title, rows in sheets:
        layout = _find_header(rows)
        if layout:
            return _parse_rows(rows, layout, title)
    for title, rows in sheets:
        layout = _headerless_layout(rows)
        if layout:
            return _parse_rows(rows, layout, title)
    raise ImportFileError("ستون کد دانشجویی پیدا نشد")


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1256", errors="replace")


def _read_csv(data: bytes) -> ImportResult:
    text = _decode(data)
    sample = "\n".join(text.splitlines()[:20])
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    if len(rows) > MAX_ROWS:
        raise ImportFileError("تعداد ردیف‌ها بیش از حد است")
    layout = _layout_for(rows)
    if layout is None:
        raise ImportFileError("ستون کد دانشجویی پیدا نشد")
    return _parse_rows(rows, layout, None)


def parse_file(filename: str, data: bytes) -> ImportResult:
    """Parse an uploaded xlsx/csv. Raises ImportFileError if the file is unusable."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return _read_xlsx(data)
    if name.endswith((".csv", ".txt")):
        return _read_csv(data)
    raise ImportFileError("فقط فایل xlsx یا csv پذیرفته می‌شود")


# ---------- free text (/add) ----------
_TEXT_SEP = re.compile(r"[,،;]")


def _looks_numeric(token: str) -> bool:
    return bool(_CODE_RE.fullmatch(normalize_digits(token)))


def parse_codes_text(text: str) -> ImportResult:
    """Codes separated by spaces, commas or newlines. A line (or comma part) that starts with
    a code followed by non-numeric words is read as "code name"."""
    result = ImportResult(has_header=False)
    collector = _Collector(result)
    for line_no, line in enumerate((text or "").splitlines(), start=1):
        for part in _TEXT_SEP.split(line):
            tokens = part.split()
            if not tokens:
                continue
            if all(_looks_numeric(t) for t in tokens):
                for t in tokens:
                    collector.add(line_no, t, None)
            else:
                collector.add(line_no, tokens[0], " ".join(tokens[1:]) or None)
    return result
