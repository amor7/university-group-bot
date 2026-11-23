"""Pure text normalization for student codes and last names."""
from __future__ import annotations

import unicodedata

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_LETTERS = str.maketrans({
    "ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه",
    "أ": "ا", "إ": "ا", "آ": "ا",
})
_TATWEEL = "ـ"


def normalize_digits(text: str) -> str:
    """Convert Persian and Arabic-Indic digits to ASCII digits."""
    return text.translate(_DIGITS)


def _strip_noise(text: str) -> str:
    """Drop invisible format chars (ZWNJ, ZWJ, RLM...), tatweel, harakat and whitespace."""
    out = []
    for ch in text:
        if ch == _TATWEEL or ch.isspace():
            continue
        if unicodedata.category(ch) == "Cf":  # ZWNJ U+200C, ZWJ, LRM/RLM, BOM
            continue
        if "ً" <= ch <= "ْ" or ch == "ٰ":
            continue
        out.append(ch)
    return "".join(out)


def normalize_code(text: str) -> str:
    """Student code: ASCII digits, only letters/digits kept, casefolded. Leading zeros kept."""
    text = normalize_digits(unicodedata.normalize("NFKC", str(text)))
    return "".join(ch for ch in text if ch.isalnum()).casefold()


def normalize_name(text: str) -> str:
    """Last name for comparison: unified Arabic/Persian letters, no spaces/diacritics, casefolded."""
    text = unicodedata.normalize("NFKC", str(text))
    text = normalize_digits(text).translate(_LETTERS)
    return _strip_noise(text).casefold()
