"""Transaction use cases that do not depend on aiogram objects."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re

import db
from categorizer import categorize_smart
from formatting import parse_positive_amount


@dataclass(frozen=True)
class QuickAddResult:
    transaction_id: int
    tx_type: str
    amount: int
    description: str | None
    category_id: int
    category_name: str


@dataclass(frozen=True)
class QuickAddRequest:
    amount_text: str
    description: str
    op_date: str


QUICK_ADD_RE = re.compile(
    r"^\s*("
    r"[+-]?\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?"
    r"|[+-]?\d+(?:[.,]\d+)?"
    r")\s*(.*?)\s*$",
    re.DOTALL,
)
TRAILING_DATE_RE = re.compile(r"\s+(\d{2}\.\d{2}\.\d{4})\s*$")
YESTERDAY_PREFIXES = ("вчера", "кеше", "yesterday")


def _strip_yesterday_prefix(raw: str, today: date) -> tuple[str, date]:
    folded = raw.casefold()
    for prefix in YESTERDAY_PREFIXES:
        if folded.startswith(prefix):
            rest = raw[len(prefix):]
            if rest == "" or rest[:1].isspace():
                return rest.lstrip(), today - timedelta(days=1)
    return raw, today


def parse_quick_add_request(text: str, today: date) -> QuickAddRequest | None:
    raw = (text or "").strip()
    raw, operation_date = _strip_yesterday_prefix(raw, today)

    date_match = TRAILING_DATE_RE.search(raw)
    if date_match:
        try:
            operation_date = datetime.strptime(
                date_match.group(1),
                "%d.%m.%Y",
            ).date()
        except ValueError:
            return None
        raw = raw[:date_match.start()].rstrip()

    match = QUICK_ADD_RE.match(raw)
    if not match:
        return None
    amount_text, description = match.groups()
    return QuickAddRequest(
        amount_text=amount_text,
        description=description.strip(),
        op_date=operation_date.isoformat(),
    )


def create_quick_transaction(
    user_id: int,
    amount_text: str,
    description_text: str,
    op_date: str | None = None,
) -> QuickAddResult | None:
    tx_type = "income" if amount_text.strip().startswith("+") else "expense"
    amount = parse_positive_amount(amount_text.lstrip("+"))
    if amount is None:
        return None

    description = description_text.strip() or None
    category_name = (
        categorize_smart(user_id, description)
        if description
        else "Прочее"
    )
    category_id = db.get_category_id_by_name(user_id, category_name)
    payment_method_id = db.get_default_payment_method_id(user_id)
    now = db.user_now(user_id)
    transaction_id = db.add_transaction(
        user_id=user_id,
        tx_type=tx_type,
        amount=amount,
        category_id=category_id,
        payment_method_id=payment_method_id,
        store=None,
        description=description,
        op_date=op_date or now.date().isoformat(),
        op_time=now.strftime("%H:%M"),
    )
    db.touch_activity(user_id, now.date().isoformat())
    return QuickAddResult(
        transaction_id=transaction_id,
        tx_type=tx_type,
        amount=amount,
        description=description,
        category_id=category_id,
        category_name=category_name,
    )
