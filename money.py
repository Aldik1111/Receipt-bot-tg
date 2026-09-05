"""Деньги: хранение целыми тиынами (1 ₸ = 100 тиын).

Ввод пользователя, Gemini и файлы импорта говорят на тенге. В базе, черновиках
после распознавания и JSON-бэкапе v4 суммы — int тиын. Округление везде
ROUND_HALF_UP, без float-арифметики.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

TIYN_PER_TENGE = 100
_TIYN_QUANT = Decimal("1")
_TENGE_QUANT = Decimal("0.01")
MAX_TIYN = 10**15


def _require_tiyn_range(tiyn: int) -> int:
    if tiyn > MAX_TIYN:
        raise MoneyError("amount too large")
    return tiyn


class MoneyError(ValueError):
    """Некорректная денежная величина."""


def _as_decimal(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MoneyError("invalid amount")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise MoneyError("invalid amount")
        amount = Decimal(str(value))
    else:
        text = (
            str(value)
            .strip()
            .replace(" ", "")
            .replace("\u00a0", "")
            .replace(",", ".")
        )
        if not text:
            raise MoneyError("invalid amount")
        try:
            amount = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise MoneyError("invalid amount") from exc
    if not amount.is_finite():
        raise MoneyError("invalid amount")
    return amount


def tenge_to_tiyn(value) -> int:
    """«350.50» / 100.40 / Decimal → целые тиыны с ROUND_HALF_UP."""
    amount = _as_decimal(value)
    if amount <= 0:
        raise MoneyError("amount must be positive")
    tiyn = int((amount * TIYN_PER_TENGE).quantize(_TIYN_QUANT, rounding=ROUND_HALF_UP))
    return _require_tiyn_range(tiyn)


def tiyn_to_tenge(tiyn: int) -> Decimal:
    return Decimal(int(tiyn)) / TIYN_PER_TENGE


def tenge_export_value(tiyn: int):
    """Значение для CSV/Excel: Decimal с двумя знаками, без float."""
    return tiyn_to_tenge(int(tiyn)).quantize(_TENGE_QUANT, rounding=ROUND_HALF_UP)


def as_stored_tiyn(value) -> int:
    """int уже тиыны; float/str/Decimal — тенге (ввод, Gemini, старый JSON)."""
    if isinstance(value, bool) or value is None:
        raise MoneyError("invalid amount")
    if isinstance(value, int):
        if value <= 0:
            raise MoneyError("amount must be positive")
        return _require_tiyn_range(value)
    return tenge_to_tiyn(value)


def parse_positive_amount(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return tenge_to_tiyn(text)
    except MoneyError:
        return None


def backup_amount_to_tiyn(value, amount_unit: str | None) -> int:
    """v4+ хранит тиыны; v2/v3 — тенге (часто float в JSON). 0 допустим для накоплений."""
    unit = (amount_unit or "").strip().lower()
    amount = _as_decimal(value)
    if amount == 0:
        return 0
    if amount < 0:
        raise MoneyError("amount must be positive")
    if unit == "tiyn":
        tiyn = int(amount.quantize(_TIYN_QUANT, rounding=ROUND_HALF_UP))
        if tiyn <= 0:
            raise MoneyError("amount must be positive")
        return _require_tiyn_range(tiyn)
    return tenge_to_tiyn(value)


def backup_signed_amount_to_tiyn(value, amount_unit: str | None) -> int:
    """Как backup_amount_to_tiyn, но сохраняет знак (goal_delta перевода)."""
    amount = _as_decimal(value)
    if amount == 0:
        return 0
    sign = -1 if amount < 0 else 1
    return sign * backup_amount_to_tiyn(abs(amount), amount_unit)


def signed_tenge_to_tiyn(value) -> tuple[int, int] | None:
    """Разбор ячейки импорта: (тиыны, знак). 0 и мусор → None."""
    try:
        amount = _as_decimal(value)
    except MoneyError:
        return None
    if amount == 0:
        return None
    sign = -1 if amount < 0 else 1
    try:
        return tenge_to_tiyn(abs(amount)), sign
    except MoneyError:
        return None
