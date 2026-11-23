"""Importer tests. All data is synthetic, built in the test (never real students)."""
import csv
import io

import openpyxl
import pytest

from bot.importer import ImportFileError, normalize_header, parse_codes_text, parse_file


def make_xlsx(*sheets: tuple[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets:
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_csv(rows: list[list], delimiter: str = ",", encoding: str = "utf-8") -> bytes:
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode(encoding)


def test_normalize_header():
    assert normalize_header("شماره_دانشجويي") == normalize_header("شماره‌ی دانشجویی".replace("ی", "", 1)) \
        or normalize_header("شماره_دانشجويي") == "شمارهدانشجویی"
    assert normalize_header(" Student_ID ") == "studentid"
    assert normalize_header("نام-و-نام خانوادگي") == "نامونامخانوادگی"


def test_xlsx_arabic_yeh_and_underscore_headers():
    data = make_xlsx(("Sheet1", [
        ["شماره_دانشجويي", "نام_و_نام_خانوادگي"],
        ["40011122233", "  آزمایشی-  یک  "],
        ["400111222", "آزمایشی-دو"],
    ]))
    r = parse_file("list.XLSX", data)
    assert r.rows == [("40011122233", "آزمایشی- یک"), ("400111222", "آزمایشی-دو")]
    assert r.has_header and r.sheet == "Sheet1"
    assert r.length_counts == {9: 1, 11: 1} and r.mixed_lengths


def test_xlsx_numeric_and_float_cells_and_persian_digits():
    data = make_xlsx(("s", [
        ["کد دانشجویی", "نام"],
        [400111222, "الف"],
        [40011122233.0, "ب"],
        ["۴۰۰۱۱۱۲۲۴", "ج"],
        [4001.5, "د"],
    ]))
    r = parse_file("a.xlsx", data)
    assert r.codes == ["400111222", "40011122233", "400111224"]
    assert [(b.row, b.value) for b in r.bad] == [(5, "4001.5")]


def test_xlsx_picks_first_sheet_with_code_header():
    data = make_xlsx(("info", [["گزارش"], ["چیزی"]]),
                     ("data", [["عنوان فایل"], [], ["ردیف", "Student Code", "Name"],
                               [1, "500000001", "x"]]))
    r = parse_file("a.xlsx", data)
    assert r.sheet == "data" and r.rows == [("500000001", "x")]


def test_empty_bad_and_duplicate_rows():
    data = make_xlsx(("s", [
        ["شماره دانشجویی", "نام و نام خانوادگی"],
        ["500000001", "یک"],
        [None, None],
        ["", "  "],
        ["12345", "کوتاه"],
        ["50000000A", "حرف"],
        [None, "بی‌کد"],
        ["500000001", "تکراری"],
        ["1234567890123456", "بلند"],
    ]))
    r = parse_file("a.xlsx", data)
    assert r.rows == [("500000001", "یک")]
    assert r.empty_rows == 2
    assert [b.row for b in r.bad] == [5, 6, 7, 9]
    assert "طول" in r.bad[0].reason and "رقم" in r.bad[1].reason and "خالی" in r.bad[2].reason
    assert r.duplicates == [(8, "500000001")]
    assert not r.mixed_lengths


def test_csv_bom_and_semicolon():
    data = b"\xef\xbb\xbf" + make_csv([["student_id", "name"], ["500000001", "Ali"],
                                       ["500000002", "Sara Test"]], delimiter=";")
    r = parse_file("a.csv", data)
    assert r.rows == [("500000001", "Ali"), ("500000002", "Sara Test")]


def test_csv_cp1256_and_tab():
    data = make_csv([["كد دانشجويي", "نام خانوادگي"], ["500000001", "آزمايشي"]],
                    delimiter="\t", encoding="cp1256")
    r = parse_file("a.csv", data)
    assert r.rows == [("500000001", "آزمايشي")]


def test_headerless_file():
    data = make_csv([["500000001", "یک"], ["50000000002", "دو"], [], ["bad", "x"]])
    r = parse_file("a.csv", data)
    assert not r.has_header
    assert r.rows == [("500000001", "یک"), ("50000000002", "دو")]
    assert [b.row for b in r.bad] == [4]
    assert r.length_counts == {9: 1, 11: 1}

    only_codes = make_xlsx(("s", [[500000001], [500000002]]))
    assert parse_file("a.xlsx", only_codes).rows == [("500000001", None), ("500000002", None)]


def test_separate_name_columns():
    data = make_xlsx(("s", [
        ["نام", "نام خانوادگی", "شماره‌ی دانشجویی"],
        ["اسم", "فامیل", "500000001"],
        [None, "فقط‌فامیل", "500000002"],
        [None, None, "500000003"],
    ]))
    r = parse_file("a.xlsx", data)
    assert r.rows == [("500000001", "فامیل - اسم"), ("500000002", "فقط‌فامیل"),
                      ("500000003", None)]


def test_file_errors():
    with pytest.raises(ImportFileError):
        parse_file("a.pdf", b"x")
    with pytest.raises(ImportFileError):
        parse_file("a.xlsx", b"not a zip")
    with pytest.raises(ImportFileError):
        parse_file("a.xlsx", make_xlsx(("s", [["foo", "bar"], ["x", "y"]])))
    with pytest.raises(ImportFileError):
        parse_file("a.csv", make_csv([["foo", "bar"], ["x", "y"]]))


def test_parse_codes_text():
    r = parse_codes_text("500000001 500000002,500000003\n۵۰۰۰۰۰۰۰۴ علی  رضایی\n\n"
                         "500000005، 500000006 نام تست\n123 500000001\nفقط‌نام")
    assert r.rows == [("500000001", None), ("500000002", None), ("500000003", None),
                      ("500000004", "علی رضایی"), ("500000005", None),
                      ("500000006", "نام تست")]
    assert [(b.row, b.value) for b in r.bad] == [(5, "123"), (6, "فقط‌نام")]
    assert r.duplicates == [(5, "500000001")]
    assert parse_codes_text("").rows == []
