"""
Parse Arabic legal-article ordinal labels (feminine form, since المادة is
feminine) into integers, e.g. "الثانية والسبعون" -> 72, "السادسة بعد المائة" -> 106.

Best-effort: returns None if the label doesn't match a known pattern, so
callers should always keep the raw label around for citation purposes.
"""

import re
import unicodedata

STANDALONE_UNITS = {
    "الأولى": 1, "الثانية": 2, "الثالثة": 3, "الرابعة": 4, "الخامسة": 5,
    "السادسة": 6, "السابعة": 7, "الثامنة": 8, "التاسعة": 9, "العاشرة": 10,
}

# used as the first part of a compound like "الحادية والعشرون" (21)
COMPOUND_UNITS = {
    "الحادية": 1, "الثانية": 2, "الثالثة": 3, "الرابعة": 4, "الخامسة": 5,
    "السادسة": 6, "السابعة": 7, "الثامنة": 8, "التاسعة": 9,
}

TEENS = {
    "الحادية عشرة": 11, "الثانية عشرة": 12, "الثالثة عشرة": 13,
    "الرابعة عشرة": 14, "الخامسة عشرة": 15, "السادسة عشرة": 16,
    "السابعة عشرة": 17, "الثامنة عشرة": 18, "التاسعة عشرة": 19,
}

TENS = {
    "العشرون": 20, "الثلاثون": 30, "الأربعون": 40, "الخمسون": 50,
    "الستون": 60, "السبعون": 70, "الثمانون": 80, "التسعون": 90,
    # accusative/genitive forms (…ين), common in running legal text
    "العشرين": 20, "الثلاثين": 30, "الأربعين": 40, "الخمسين": 50,
    "الستين": 60, "السبعين": 70, "الثمانين": 80, "التسعين": 90,
}

HUNDRED_OFFSETS = [
    ("بعد الثلاثمائة", 300),
    ("بعد المائتين", 200),
    ("بعد المئتين", 200),
    ("بعد المائة", 100),
    ("بعد المئة", 100),
]


def _parse_base(text: str):
    text = text.strip()
    if text in TEENS:
        return TEENS[text]
    if text in TENS:
        return TENS[text]
    if text in STANDALONE_UNITS:
        return STANDALONE_UNITS[text]
    if text in HUNDRED_100:
        return 100
    if text in HUNDRED_200:
        return 200
    # compound like "الحادية والعشرون"
    m = re.match(r"^(\S+)\s+و(\S+)$", text)
    if m:
        unit_word, tens_word = m.group(1), m.group(2)
        if unit_word in COMPOUND_UNITS and tens_word in TENS:
            return COMPOUND_UNITS[unit_word] + TENS[tens_word]
    return None


def _normalize_keys(d: dict) -> dict:
    return {normalize_arabic(k): v for k, v in d.items()}


def strip_bidi(text: str) -> str:
    """Remove Unicode format characters (bidi marks, embeddings, isolates).
    Text copied out of an Arabic PDF wraps *every letter* in U+202B/U+202C,
    which splits words into single letters and destroys retrieval; removing
    them rejoins the letters into words."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def normalize_arabic(text: str) -> str:
    """Normalize common spelling variants: ة/ه endings, hamza forms, ى/ي,
    and strip tashkeel — so casual typing still matches the dictionaries."""
    text = strip_bidi(text)
    text = re.sub(r"[ً-ْٰ]", "", text)  # tashkeel
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    # hamza on waw/ya: people routinely type مؤقت as موقت, مسئول as مسؤول
    text = text.replace("ؤ", "و").replace("ئ", "ي")
    text = re.sub(r"ه\b", "ة", text)  # word-final ه -> ة (الثالثه -> الثالثة)
    text = text.replace("ى", "ي")
    return text


STANDALONE_UNITS = _normalize_keys(STANDALONE_UNITS)
COMPOUND_UNITS = _normalize_keys(COMPOUND_UNITS)
TEENS = _normalize_keys(TEENS)
TENS = _normalize_keys(TENS)
# "المائة" contains ئ, so these must pass through the same normalisation
HUNDRED_OFFSETS = [(normalize_arabic(sfx), val) for sfx, val in HUNDRED_OFFSETS]
HUNDRED_100 = {normalize_arabic(w) for w in ("المائة", "المئة")}
HUNDRED_200 = {normalize_arabic(w) for w in
               ("المائتان", "المئتان", "المائتين", "المئتين")}


def parse_article_ordinal(label: str):
    """
    label: raw Arabic ordinal text with 'المادة' and trailing ':' already
    stripped, e.g. "الثانية والسبعون" or "السادسة بعد المائة".
    Returns an int article number, or None if unparseable.
    """
    label = normalize_arabic(label.strip()).strip(".:،؛؟!\"'«»®")
    label = re.sub(r"\s*مكرر.*$", "", label)  # drop "مكرر (1)" suffixes
    label = label.strip()

    offset = 0
    for suffix, value in HUNDRED_OFFSETS:
        if label.endswith(suffix):
            offset = value
            label = label[: -len(suffix)].strip()
            break

    base = _parse_base(label)
    if base is None:
        return None
    return offset + base


def parse_regulation_own_number(label: str):
    """
    label: text inside 'المادة (...)' e.g. "1", "4 مكرر", "16 مكرر (2)".
    Returns (number, suffix) where suffix captures 'مكرر ...' if present.
    """
    m = re.match(r"^\s*(\d+)\s*(.*)$", label.strip())
    if not m:
        return None, None
    return int(m.group(1)), (m.group(2).strip() or None)
