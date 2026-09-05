"""
Главная точка входа для распознавания чека. Один шаг: Google Gemini -
мультимодальная модель, которая по фото сразу возвращает магазин, дату,
товары и цены в виде JSON. Если Gemini не смог распознать (не настроен
ключ, ошибка сети, на фото нет чека и т.д.) - возвращается "пустой" чек
и код ошибки для понятного сообщения пользователю.
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


def extract_receipt(image_path: str, user_id: int) -> tuple[dict, str | None]:
    try:
        result, err = get_receipt_from_gemini(image_path)
    except Exception:
        logger.exception("Ошибка при распознавании чека через Gemini")
        return dict(EMPTY_RECEIPT), "unavailable"

    if err:
        if err == "empty":
            logger.info("Gemini не нашёл товары на чеке")
        else:
            logger.info("Gemini не смог распознать чек: %s", err)
        return dict(EMPTY_RECEIPT), err

    if result and result.get("items"):
        logger.info("Чек распознан через Gemini")
        _categorize_items(result, user_id)
        return result, None

    logger.info("Gemini не смог распознать чек")
    return dict(EMPTY_RECEIPT), "empty"


def _categorize_items(parsed: dict, user_id: int) -> None:
    names = [item.get("name") or "" for item in parsed["items"]]
    categories = categorize_many(user_id, names)
    for item, category in zip(parsed["items"], categories):
        item.setdefault("category", category)
