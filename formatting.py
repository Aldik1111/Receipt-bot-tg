"""Форматирование денежных сумм. Валюта фиксирована - тенге, копейки не
показываем (для повседневного бюджета они не нужны и только загромождают
текст): 50000.0 -> "50 000 ₸", а не "50000.00 KZT"."""

from config import CURRENCY_SYMBOL


def money(amount: float) -> str:
    rounded = round(amount)
    formatted = f"{rounded:,}".replace(",", " ")
    return f"{formatted} {CURRENCY_SYMBOL}"