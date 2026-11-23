from bot.normalize import normalize_code, normalize_digits, normalize_name


def test_digits_persian_and_arabic():
    assert normalize_digits("۰۱۲۳۴۵۶۷۸۹") == "0123456789"
    assert normalize_digits("٠١٢٣٤٥٦٧٨٩") == "0123456789"
    assert normalize_digits("abc ۱2٣") == "abc 123"


def test_code_keeps_leading_zero_and_strips_noise():
    assert normalize_code("۰۱۲۳۴۵۶۷۸") == "012345678"
    assert normalize_code(" 012-345 678 ") == "012345678"
    assert normalize_code("٠١٢‌٣٤") == "01234"
    assert normalize_code("AB12") == "ab12"
    assert normalize_code("") == ""


def test_name_arabic_letters_unified():
    assert normalize_name("علي") == normalize_name("علی")
    assert normalize_name("كريمي") == normalize_name("کریمی")
    assert normalize_name("فاطمة") == normalize_name("فاطمه")
    assert normalize_name("أحمد") == normalize_name("احمد") == normalize_name("إحمد")
    assert normalize_name("آرش") == "ارش"


def test_name_zwnj_tatweel_diacritics_spaces():
    assert normalize_name("محمدی‌پور") == normalize_name("محمدیپور")
    assert normalize_name("محـــمد") == "محمد"
    assert normalize_name("مُحَمَّد") == "محمد"
    assert normalize_name("  نام   و   نام خانوادگی ") == "نامونامخانوادگی"
    assert normalize_name("Test Name") == "testname"
