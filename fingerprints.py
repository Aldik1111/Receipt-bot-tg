"""Нормализованные отпечатки источников, без сырых фото."""

from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")


def photo_telegram_fingerprint(file_unique_id: str) -> str:
    return f"tg:{file_unique_id}"


def photo_sha256_fingerprint(digest: str) -> str:
    return f"sha256:{digest}"


def bank_fingerprint(amount_tiyn: int, store: str | None, op_date: str, text: str) -> str:
    store_n = (store or "").strip().lower()
    snippet = _WS.sub(" ", (text or "")[:80].strip().lower())
    raw = f"{int(amount_tiyn)}|{store_n}|{op_date}|{snippet}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def import_row_fingerprint(op_date: str | None, amount_tiyn: int, description: str | None) -> str:
    desc = _WS.sub(" ", (description or "").strip().lower())
    raw = f"{op_date or ''}|{int(amount_tiyn)}|{desc}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
