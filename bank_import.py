"""
Разбор пересланных push/SMS-уведомлений банковских приложений (Kaspi, Halyk
и подобные). Форматы у банков не документированы официально и периодически
меняются, поэтому парсер работает по эвристикам, а не по жёсткому шаблону -
и результат ВСЕГДА показывается пользователю на подтверждение перед
сохранением (как и с чеками), а не сохраняется втихую.
"""

import re

from money import MoneyError, tenge_to_tiyn

# Наличие любого из этих слов - сигнал, что сообщение похоже на банковское
# уведомление, и стоит попытаться его распарсить
BANK_KEYWORDS = re.compile(
    r"(kaspi|halyk|каспи|халык|kaspi\s*pay|kaspi\s*gold|списание|зачисление|"
    r"оплата картой|purchase|paid|тенге|kzt|₸)",
    re.IGNORECASE,
)

INCOME_KEYWORDS = re.compile(
    r"(зачислен|поступлен|пополнен|получен перевод|credited|deposit)", re.IGNORECASE
)
EXPENSE_KEYWORDS = re.compile(
    r"(оплата|покупка|списан|перевод отправлен|purchase|paid|withdrawal)",
    re.IGNORECASE,
)

AMOUNT_TOKEN = (
    r"\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?"
)
AMOUNT_RE = re.compile(
    rf"({AMOUNT_TOKEN})\s*(?:kzt|тг|тенге|₸|rub|руб|\$|usd)",
    re.IGNORECASE,
)
DIRECTION_AMOUNT_RE = re.compile(
    rf"(?:оплата|покупка|списан\w*|зачислен\w*|поступлен\w*|"
    rf"purchase|paid|credited|deposit)[^\d]{{0,32}}"
    rf"({AMOUNT_TOKEN})\s*(?:kzt|тг|тенге|₸|rub|руб|\$|usd)",
    re.IGNORECASE,
)

# После этих слов обычно идёт название магазина/получателя
MERCHANT_HINTS = re.compile(
    r"(?:в|at|merchant:|магазин:|тоо|ип)\s+"
    r"([A-ZА-ЯЁ][\w \"'\.\-]{2,40}?)"
    r"(?=[.!?;\n]|\s+(?:доступно|баланс|остаток)\b|$)",
    re.IGNORECASE,
)


def looks_like_bank_notification(text: str) -> bool:
    return bool(BANK_KEYWORDS.search(text))


def parse_bank_notification(text: str) -> dict | None:
    """Возвращает {"amount": int тиын, "store": str|None, "type": "expense"/"income"}
    или None, если не удалось распознать сумму (тогда даже не стоит показывать
    превью - значит это не банковское уведомление, а что-то другое)."""
    amount_match = DIRECTION_AMOUNT_RE.search(text) or AMOUNT_RE.search(text)
    if not amount_match:
        return None

    amount_str = amount_match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        amount = tenge_to_tiyn(amount_str)
    except (MoneyError, ValueError):
        return None

    has_income = bool(INCOME_KEYWORDS.search(text))
    has_expense = bool(EXPENSE_KEYWORDS.search(text))
    tx_type = "income" if has_income and not has_expense else "expense"

    store = None
    merchant_match = MERCHANT_HINTS.search(text)
    if merchant_match:
        store = merchant_match.group(1).strip(" .\"'")

    return {
        "amount": amount,
        "store": store,
        "type": tx_type,
        "ambiguous": has_income == has_expense,
    }