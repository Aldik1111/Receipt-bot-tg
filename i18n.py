"""
Мультиязычность интерфейса: ru / kk / en.

Тексты лежат в locales/{lang}.json. t(key, lang, **kwargs) подставляет
плейсхолдеры через str.format. Если ключа нет в выбранном языке — берётся
русский, иначе сам ключ.

Пользовательские названия (категории, магазины, цели, описания) не переводятся.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from aiogram.types import BotCommand

from config import CURRENCY_SYMBOL, PRIVACY_POLICY_VERSION
from money import tiyn_to_tenge

LANGUAGES = {"ru": "Русский", "kk": "Қазақша", "en": "English"}
PRIVACY_VERSION = PRIVACY_POLICY_VERSION
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

WEEKDAYS = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сн", "Жс"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
MONTHS = {
    "ru": ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"],
    "kk": ["Қаң", "Ақп", "Нау", "Сәу", "Мам", "Мау", "Шіл", "Там", "Қыр", "Қаз", "Қар", "Жел"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}

BOT_COMMAND_KEYS = (
    ("start", "cmd_start"),
    ("help", "cmd_help"),
    ("add", "cmd_add"),
    ("today", "cmd_today"),
    ("recent", "cmd_recent"),
    ("search", "cmd_search"),
    ("stats", "cmd_stats"),
    ("category_stats", "cmd_category_stats"),
    ("categories", "cmd_categories"),
    ("payments", "cmd_payments"),
    ("budget", "cmd_budget"),
    ("goals", "cmd_goals"),
    ("recurring", "cmd_recurring"),
    ("export", "cmd_export"),
    ("digest", "cmd_digest"),
    ("settings", "cmd_settings"),
    ("language", "cmd_language"),
    ("privacy", "cmd_privacy"),
    ("feedback", "cmd_feedback"),
    ("family", "cmd_family"),
    ("pro", "cmd_pro"),
    ("cancel", "cmd_cancel"),
)

MINIAPP_KEYS = (
    "app_title",
    "tab_overview",
    "tab_budgets",
    "tab_goals",
    "period_week",
    "period_month",
    "period_3months",
    "period_year",
    "period_custom",
    "custom_apply",
    "all_categories",
    "expenses",
    "income",
    "balance",
    "by_categories",
    "transactions_title",
    "back_button",
    "more_button",
    "no_transactions",
    "no_budgets",
    "no_goals",
    "no_description",
    "uncategorized",
    "overall_expenses",
    "load_error",
    "add_button",
    "save_button",
    "cancel_button",
    "edit_button",
    "form_amount",
    "form_type",
    "form_category",
    "form_date",
    "form_description",
    "form_store",
    "form_payment",
    "type_expense",
    "type_income",
    "created_ok",
    "updated_ok",
    "save_error",
    "validation_error",
)


@lru_cache(maxsize=8)
def _load_locale(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def supported_languages() -> tuple[str, ...]:
    return tuple(LANGUAGES)


def translation_gaps() -> dict[str, list[str]]:
    """Ключи, которых нет в каком-то языке относительно русского."""
    ru_keys = set(_load_locale("ru"))
    gaps: dict[str, list[str]] = {}
    for lang in LANGUAGES:
        missing = sorted(ru_keys - set(_load_locale(lang)))
        extra = sorted(set(_load_locale(lang)) - ru_keys)
        if missing or extra:
            gaps[lang] = [f"-{key}" for key in missing] + [f"+{key}" for key in extra]
    return gaps


def t(key: str, lang: str = "ru", **kwargs) -> str:
    text = _load_locale(lang).get(key) or _load_locale("ru").get(key) or key
    return text.format(**kwargs) if kwargs else text


def ut(user_id: int, key: str, **kwargs) -> str:
    import db

    return t(key, db.get_user_language(user_id), **kwargs)


def normalize_lang(lang: str | None) -> str:
    if lang in LANGUAGES:
        return lang
    if lang:
        prefix = lang.split("-", 1)[0].lower()
        if prefix in LANGUAGES:
            return prefix
    return "ru"


def bot_commands(lang: str) -> list[BotCommand]:
    return [
        BotCommand(command=command, description=t(key, lang))
        for command, key in BOT_COMMAND_KEYS
    ]


def miniapp_bundle(lang: str) -> dict[str, str]:
    return {key: t(key, lang) for key in MINIAPP_KEYS}


def format_date(value, lang: str = "ru") -> str:
    if value is None or value == "":
        return t("dash", lang)
    if isinstance(value, datetime):
        day = value.date()
    elif isinstance(value, date):
        day = value
    else:
        text = str(value)
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            return text
    if lang == "en":
        return f"{day.day} {MONTHS['en'][day.month - 1]} {day.year}"
    return day.strftime("%d.%m.%Y")


def format_money(amount_tiyn, lang: str = "ru") -> str:
    try:
        tiyn = int(amount_tiyn)
    except (TypeError, ValueError):
        tiyn = 0
    sign = "-" if tiyn < 0 else ""
    tiyn = abs(tiyn)
    tenge, frac = divmod(tiyn, 100)
    if lang == "en":
        whole = f"{tenge:,}"
        if frac:
            return f"{sign}{whole}.{frac:02d} {CURRENCY_SYMBOL}"
        return f"{sign}{whole} {CURRENCY_SYMBOL}"
    whole = f"{tenge:,}".replace(",", " ")
    if frac:
        return f"{sign}{whole},{frac:02d} {CURRENCY_SYMBOL}"
    return f"{sign}{whole} {CURRENCY_SYMBOL}"


def type_label(tx_type: str, lang: str = "ru") -> str:
    return {
        "expense": t("type_expense", lang),
        "income": t("type_income", lang),
        "transfer": t("type_transfer", lang),
    }.get(tx_type, t("type_expense", lang))


def weekday_short(day: date, lang: str = "ru") -> str:
    return WEEKDAYS.get(lang, WEEKDAYS["ru"])[day.weekday()]


def month_short(day: date, lang: str = "ru") -> str:
    return MONTHS.get(lang, MONTHS["ru"])[day.month - 1]


# Совместимость со старым импортом TRANSLATIONS в тестах/отладке.
TRANSLATIONS = {lang: _load_locale(lang) for lang in LANGUAGES}
