"""Форматирование денежных сумм. Валюта фиксирована - тенге, копейки не
показываем (для повседневного бюджета они не нужны и только загромождают
текст): 50000.0 -> "50 000 ₸", а не "50000.00 KZT"."""

import math
from html import escape

from config import CURRENCY_SYMBOL


def money(amount: float) -> str:
    rounded = round(amount)
    formatted = f"{rounded:,}".replace(",", " ")
    return f"{formatted} {CURRENCY_SYMBOL}"


def hx(value) -> str:
    """Экранирование пользовательского текста для Telegram HTML."""
    if value is None:
        return ""
    return escape(str(value), quote=True)


def parse_positive_amount(text: str | None) -> float | None:
    if not text:
        return None
    try:
        value = float(text.replace(",", ".").replace(" ", "").replace("\u00a0", ""))
    except ValueError:
        return None
    if math.isfinite(value) and value > 0:
        return value
    return None
