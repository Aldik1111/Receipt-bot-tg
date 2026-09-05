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

REQUEST_TIMEOUT = 60  # Gemini 3 сначала «думает», 30с часто рвёт vision-запрос
# Если алиас 404 — пробуем актуальные Flash-модели по очереди.
GEMINI_MODEL_FALLBACKS = (
    GEMINI_MODEL,
    "gemini-flash-latest",
    "gemini-3.6-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_CURRENCY_TAIL_RE = re.compile(r"(₸|тг\.?|тенге|kzt|tg)\s*$", re.IGNORECASE)

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
    if isinstance(value, str):
        value = _CURRENCY_TAIL_RE.sub("", value.strip())
    try:
        return tenge_to_tiyn(value)
    except MoneyError:
        return None


def _candidate_text(payload: dict) -> str | None:
    """Собирает видимый текст, пропуская thought-части Gemini 3."""
    if not isinstance(payload, dict):
        return None
    feedback = payload.get("promptFeedback") or payload.get("prompt_feedback") or {}
    if feedback.get("blockReason") or feedback.get("block_reason"):
        logger.error("Gemini заблокировал запрос: %s", feedback)
        return None
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        logger.error("Gemini вернул пустой candidates: %s", str(payload)[:400])
        return None
    content = candidates[0].get("content") or {}
    parts = content.get("parts")
    if not isinstance(parts, list):
        logger.error(
            "Gemini без parts, finishReason=%s",
            candidates[0].get("finishReason") or candidates[0].get("finish_reason"),
        )
        return None
    chunks: list[str] = []
    thoughts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if not text:
            continue
        # thoughtSignature живёт на финальном ответе, не на «думании».
        if part.get("thought") is True:
            thoughts.append(str(text))
            continue
        chunks.append(str(text))
    if chunks:
        return "".join(chunks).strip()
    if thoughts:
        return thoughts[-1].strip()
    return None


def _loads_model_json(text: str):
    raw = _JSON_FENCE_RE.sub("", (text or "").strip())
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


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


def _gemini_models() -> list[str]:
    seen: list[str] = []
    for name in GEMINI_MODEL_FALLBACKS:
        if name and name not in seen:
            seen.append(name)
    return seen


def _post_gemini(body: dict) -> tuple[dict | None, str | None]:
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    last_error = None
    models = _gemini_models()
    for model in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        for attempt in range(3):
            try:
                response = requests.post(
                    url, headers=headers, json=body, timeout=REQUEST_TIMEOUT
                )
                if response.status_code == 429:
                    last_error = "rate_limit"
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if response.status_code in (500, 502, 503, 504):
                    last_error = "network"
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if response.status_code == 404:
                    logger.warning("Gemini модель %s не найдена, пробую следующую", model)
                    last_error = "unavailable"
                    break
                if not response.ok:
                    logger.error(
                        "Gemini API %s вернул %s: %s",
                        model,
                        response.status_code,
                        response.text[:500],
                    )
                    if (
                        response.status_code == 400
                        and isinstance(body.get("generationConfig"), dict)
                        and "thinkingConfig" in body["generationConfig"]
                    ):
                        body = dict(body)
                        gen = dict(body["generationConfig"])
                        gen.pop("thinkingConfig", None)
                        body["generationConfig"] = gen
                        last_error = "unavailable"
                        continue
                    if response.status_code in (400, 403) and model != models[-1]:
                        last_error = "unavailable"
                        break
                    return None, "unavailable"
                return response.json(), None
            except requests.RequestException:
                logger.exception("Сеть Gemini, модель %s попытка %s", model, attempt + 1)
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
                    {
                        "inline_data": {
                            "mime_type": _guess_mime_type(image_path),
                            "data": image_b64,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,
            # MINIMAL thinkingLevel на flash-latest даёт 400; budget=0 проходит.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    payload, err = _post_gemini(body)
    if err:
        return None, err
    if not payload:
        return None, "unavailable"
    try:
        text = _candidate_text(payload)
        if not text:
            return None, "bad_json"
        data = _loads_model_json(text)
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
    return _candidate_text(payload)


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
        data = _loads_model_json(result)
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