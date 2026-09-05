"""
Шаг 3 пайплайна (последний рубеж): если не сработал ни QR, ни Tesseract,
отправляем фото в Google Gemini - мультимодальную модель, которая понимает
фото целиком, а не только пиксели, и поэтому гораздо устойчивее к плохому
качеству, кривой съёмке и нестандартной вёрстке чека.

Требует бесплатный API-ключ с https://aistudio.google.com/apikey
(бесплатный лимит ~1500 запросов/день для Gemini Flash, карта не нужна).
"""

import base64
import json
import logging
import re
import time
from datetime import date, datetime

import requests

from config import GEMINI_API_KEY, GEMINI_MODEL
from money import MoneyError, tenge_to_tiyn

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # секунд - vision-запросы медленнее обычных текстовых

PROMPT = """Ты распознаёшь кассовый чек на фото. Верни ТОЛЬКО JSON без пояснений, строго в такой структуре:

{
  "store": "название магазина или null",
  "date": "YYYY-MM-DD или null",
  "time": "HH:MM или null",
  "items": [
    {"name": "название товара", "price": число}
  ],
  "total": число или null
}

Правила:
- price и total - это числа (не строки), с точкой как разделителем дробной части.
- Если чек нечитаемый или это не чек - верни items: [].
- Не придумывай данные, которых не видно на фото. Если поле не удаётся определить - null.
"""


def _guess_mime_type(image_path: str) -> str:
    with open(image_path, "rb") as f:
        header = f.read(12)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"\xff\xd8"):
        return "image/jpeg"
    lower = image_path.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _parse_item_price(value) -> int | None:
    """Цена из JSON Gemini — тенге; возвращаем целые тиыны."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return tenge_to_tiyn(value)
    except MoneyError:
        return None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}(?::\d{2})?$")
_MAX_NAME = 200
_MAX_STORE = 120
_MAX_ITEMS = 80


def _optional_str(value, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text[:max_len]


def _parse_iso_date(value) -> str | None:
    text = _optional_str(value, 32)
    if not text or not _DATE_RE.match(text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _parse_iso_time(value) -> str | None:
    text = _optional_str(value, 16)
    if not text or not _TIME_RE.match(text):
        return None
    try:
        datetime.strptime(text[:5], "%H:%M")
    except ValueError:
        return None
    return text[:5]


def sanitize_gemini_receipt(data: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return None
    items = []
    for it in raw_items[:_MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        name = _optional_str(it.get("name"), _MAX_NAME)
        price = _parse_item_price(it.get("price"))
        if name and price is not None:
            items.append({"name": name, "price": price})
    if not items:
        return None
    total = _parse_item_price(data.get("total"))
    item_sum = sum(item["price"] for item in items)
    if total is None:
        total = item_sum
    return {
        "store": _optional_str(data.get("store"), _MAX_STORE),
        "date": _parse_iso_date(data.get("date")),
        "time": _parse_iso_time(data.get("time")),
        "items": items,
        "total": total,
        "raw_text": f"[распознано Gemini {GEMINI_MODEL}]",
        "source": "gemini",
    }


def _post_gemini(body: dict) -> tuple[dict | None, str | None]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    last_error = None
    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429:
                last_error = "rate_limit"
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code in (500, 502, 503, 504):
                last_error = "network"
                time.sleep(1.5 * (attempt + 1))
                continue
            if not response.ok:
                logger.error(
                    "Gemini API вернул %s: %s", response.status_code, response.text[:500]
                )
                return None, "unavailable"
            return response.json(), None
        except requests.RequestException:
            logger.exception("Сеть Gemini, попытка %s", attempt + 1)
            last_error = "network"
            time.sleep(1.5 * (attempt + 1))
    if last_error:
        logger.error("Gemini недоступен после повторов, причина %s", last_error)
        return None, last_error
    return None, "unavailable"


def get_receipt_from_gemini(image_path: str) -> tuple[dict | None, str | None]:
    if not GEMINI_API_KEY:
        logger.info("GEMINI_API_KEY не задан - шаг с Gemini пропускается")
        return None, "no_key"

    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    body = {
        "contents": [
            {
                "parts": [
                    {"text": PROMPT},
                    {"inline_data": {"mime_type": _guess_mime_type(image_path), "data": image_b64}},
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }

    payload, err = _post_gemini(body)
    if err:
        return None, err
    if not payload:
        return None, "unavailable"
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        logger.exception("Некорректный ответ Gemini")
        return None, "bad_json"
    parsed = sanitize_gemini_receipt(data)
    if not parsed:
        return None, "empty"
    return parsed, None


def _text_request(prompt: str, max_tokens: int = 200) -> str | None:
    """Общая обёртка для текстовых (не по фото) запросов к Gemini."""
    if not GEMINI_API_KEY:
        return None
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
    }
    payload, err = _post_gemini(body)
    if err or not payload:
        return None
    try:
        return payload["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        return None


def guess_categories_ai(item_names: list[str], category_names: list[str]) -> dict[str, str]:
    """Один запрос на пачку неизвестных товаров вместо N+1."""
    if not item_names:
        return {}
    numbered = "\n".join(f"{i+1}. {name}" for i, name in enumerate(item_names[:80]))
    prompt = (
        f"Категории (только из этого списка): {', '.join(category_names)}\n"
        f"Товары:\n{numbered}\n"
        "Верни JSON-объект, где ключ - номер товара (строка), значение - "
        "название категории из списка. Без пояснений."
    )
    result = _text_request(prompt, max_tokens=400)
    if not result:
        return {}
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    allowed = set(category_names)
    mapped = {}
    for i, name in enumerate(item_names[:80], start=1):
        guess = data.get(str(i)) or data.get(i)
        if isinstance(guess, str) and guess in allowed:
            mapped[name] = guess
    return mapped


def guess_category_ai(item_name: str, category_names: list[str]) -> str | None:
    return guess_categories_ai([item_name], category_names).get(item_name)


def generate_insight_text(summary: dict, lang: str = "ru") -> str | None:
    """Короткий человеческий вывод по статистике для автосводки/дайджеста."""
    lang_names = {"ru": "русском", "kk": "казахском", "en": "английском"}
    prompt = (
        f"Вот сводка расходов пользователя бюджетного бота за период:\n"
        f"{summary}\n\n"
        f"Напиши 1-2 коротких предложения с наблюдением или советом на "
        f"{lang_names.get(lang, 'русском')} языке - по-дружески, без канцелярита, "
        f"без markdown-разметки, без вступлений вида 'вот наблюдение'."
    )
    return _text_request(prompt, max_tokens=150)