"""Форматирование денежных сумм. Валюта фиксирована — тенге.

Суммы приходят целыми тиынами (1 ₸ = 100 тиын). Целые тенге показываем без
дроби, ненулевые тиыны — с двумя знаками: 10040 → "100,40 ₸".
"""

import unicodedata
from html import escape

from config import CURRENCY_SYMBOL
from money import parse_positive_amount as parse_positive_amount  # noqa: F401


def money(amount_tiyn) -> str:
    try:
        tiyn = int(amount_tiyn)
    except (TypeError, ValueError):
        tiyn = 0
    sign = "-" if tiyn < 0 else ""
    tiyn = abs(tiyn)
    tenge, frac = divmod(tiyn, 100)
    whole = f"{tenge:,}".replace(",", " ")
    if frac:
        return f"{sign}{whole},{frac:02d} {CURRENCY_SYMBOL}"
    return f"{sign}{whole} {CURRENCY_SYMBOL}"


def hx(value) -> str:
    """Экранирование пользовательского текста для Telegram HTML."""
    if value is None:
        return ""
    return escape(str(value), quote=True)


def grapheme_count(text: str) -> int:
    """Считает видимые символы: ZWJ-эмодзи вроде 👨‍👩‍👧‍👦 — это один."""
    count = 0
    index = 0
    while index < len(text):
        count += 1
        index += 1
        while index < len(text):
            char = text[index]
            if unicodedata.combining(char) or char in "\ufe0f\u20e3":
                index += 1
                continue
            if char == "\u200d" and index + 1 < len(text):
                index += 2
                continue
            break
    return count


def is_single_emoji(text: str) -> bool:
    value = (text or "").strip()
    if not value or any(char.isspace() for char in value):
        return False
    return grapheme_count(value) == 1
