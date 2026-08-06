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

import requests

from config import GEMINI_API_KEY, GEMINI_MODEL

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
    lower = image_path.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def get_receipt_from_gemini(image_path: str) -> dict | None:
    if not GEMINI_API_KEY:
        logger.info("GEMINI_API_KEY не задан - шаг с Gemini пропускается")
        return None

    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    # Ключ передаём заголовком x-goog-api-key, а не ?key= в URL - это текущий
    # официальный способ, актуальный в том числе для новых Auth-ключей формата
    # "AQ.Ab..." (пришли на смену старым "AIzaSy...").
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
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
            "temperature": 0.1,  # низкая температура - меньше "фантазий", больше точности
        },
    }

    try:
        response = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        if not response.ok:
            # Логируем тело ответа - там обычно точная причина (неверный ключ,
            # не включён API, неверное имя модели и т.д.), а не просто код 404/403
            logger.error(
                "Gemini API вернул %s: %s", response.status_code, response.text[:500]
            )
            return None
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
    except Exception:
        logger.exception("Ошибка запроса к Gemini API")
        return None

    items = [
        {"name": str(it.get("name", "")).strip(), "price": float(it.get("price", 0))}
        for it in data.get("items", [])
        if it.get("name") and it.get("price") is not None
    ]
    if not items:
        return None

    return {
        "store": data.get("store"),
        "date": data.get("date"),
        "time": data.get("time"),
        "items": items,
        "total": data.get("total"),
        "raw_text": f"[распознано Gemini {GEMINI_MODEL}]",
        "source": "gemini",
    }