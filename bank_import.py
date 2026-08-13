"""
Разбор пересланных push/SMS-уведомлений банковских приложений (Kaspi, Halyk
и подобные). Форматы у банков не документированы официально и периодически
меняются, поэтому парсер работает по эвристикам, а не по жёсткому шаблону -
и результат ВСЕГДА показывается пользователю на подтверждение перед
сохранением (как и с чеками), а не сохраняется втихую.
"""

import re

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

AMOUNT_RE = re.compile(
    r"(\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{2})?|\d+(?:[.,]\d{2})?)\s*(?:kzt|тг|тенге|₸|rub|руб|\$|usd)",
    re.IGNORECASE,
)

# После этих слов обычно идёт название магазина/получателя
MERCHANT_HINTS = re.compile(
    r"(?:в|at|merchant:|магазин:|тоо|ип)\s+([A-ZА-ЯЁ][\w \"'\.\-]{2,40})", re.IGNORECASE
)


def looks_like_bank_notification(text: str) -> bool:
    return bool(BANK_KEYWORDS.search(text))


def parse_bank_notification(text: str) -> dict | None:
    """Возвращает {"amount": float, "store": str|None, "type": "expense"/"income"}
    или None, если не удалось распознать сумму (тогда даже не стоит показывать
    превью - значит это не банковское уведомление, а что-то другое)."""
    amount_match = AMOUNT_RE.search(text)
    if not amount_match:
        return None

    amount_str = amount_match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        amount = float(amount_str)
    except ValueError:
        return None
    if amount <= 0:
        return None

    tx_type = "income" if INCOME_KEYWORDS.search(text) else "expense"

    store = None
    merchant_match = MERCHANT_HINTS.search(text)
    if merchant_match:
        store = merchant_match.group(1).strip(" .\"'")

    return {"amount": amount, "store": store, "type": tx_type}