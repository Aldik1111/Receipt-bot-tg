"""
Главная точка входа для распознавания чека. Один шаг: Google Gemini -
мультимодальная модель, которая по фото сразу возвращает магазин, дату,
товары и цены в виде JSON. Если Gemini не смог распознать (не настроен
ключ, ошибка сети, на фото нет чека и т.д.) - возвращается "пустой" чек,
и bot.py предлагает добавить трату вручную.
"""

import logging

from categorizer import categorize_many
from gemini_engine import get_receipt_from_gemini

logger = logging.getLogger(__name__)

EMPTY_RECEIPT = {
    "store": None,
    "date": None,
    "time": None,
    "items": [],
    "total": None,
    "raw_text": "",
    "source": "none",
}


def extract_receipt(image_path: str, user_id: int) -> dict:
    try:
        result = get_receipt_from_gemini(image_path)
    except Exception:
        logger.exception("Ошибка при распознавании чека через Gemini")
        result = None

    if result and result.get("items"):
        logger.info("Чек распознан через Gemini")
        _categorize_items(result, user_id)
        return result

    logger.info("Gemini не смог распознать чек")
    return dict(EMPTY_RECEIPT)


def _categorize_items(parsed: dict, user_id: int) -> None:
    names = [item.get("name") or "" for item in parsed["items"]]
    categories = categorize_many(user_id, names)
    for item, category in zip(parsed["items"], categories):
        item.setdefault("category", category)