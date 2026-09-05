"""
Проверка подлинности initData, которую Mini App присылает на каждый запрос.

Telegram подписывает initData HMAC-подписью на основе токена бота, поэтому
только наш бэкенд может её проверить - подделать запрос от чужого user_id
невозможно без знания токена. Алгоритм из официальной документации Telegram:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 3600  # старше часа initData не принимаем (защита от replay)
MAX_FUTURE_SKEW_SECONDS = 30  # небольшой допустимый рассинхрон часов сервера


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Возвращает dict с данными пользователя, если подпись верна, иначе None."""
    if not init_data:
        return None

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", 0))
    except (TypeError, ValueError):
        return None
    age = time.time() - auth_date
    if age > MAX_AGE_SECONDS or age < -MAX_FUTURE_SKEW_SECONDS:
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user
