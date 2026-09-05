"""Keyboard builders shared by several domain routers."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
from i18n import t


def categories_keyboard(
    user_id: int,
    prefix: str,
    page: int = 0,
    page_size: int = 8,
) -> InlineKeyboardMarkup:
    categories = db.get_categories(user_id)
    total_pages = max(1, (len(categories) + page_size - 1) // page_size)
    page = min(max(0, page), total_pages - 1)
    start = page * page_size
    rows = [
            [
                InlineKeyboardButton(
                    text=f"{category['emoji']} {category['name']}",
                    callback_data=f"{prefix}:{category['id']}",
                )
            ]
            for category in categories[start:start + page_size]
    ]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="◀️",
            callback_data=f"pickcat:{page - 1}:{prefix}",
        ))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(
            text="▶️",
            callback_data=f"pickcat:{page + 1}:{prefix}",
        ))
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payments_keyboard(
    user_id: int,
    prefix: str,
    page: int = 0,
    page_size: int = 8,
) -> InlineKeyboardMarkup:
    payments = db.get_payment_methods(user_id)
    total_pages = max(1, (len(payments) + page_size - 1) // page_size)
    page = min(max(0, page), total_pages - 1)
    start = page * page_size
    rows = [
            [
                InlineKeyboardButton(
                    text=payment["name"],
                    callback_data=f"{prefix}:{payment['id']}",
                )
            ]
            for payment in payments[start:start + page_size]
    ]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="◀️",
            callback_data=f"pickpay:{page - 1}:{prefix}",
        ))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(
            text="▶️",
            callback_data=f"pickpay:{page + 1}:{prefix}",
        ))
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def with_back_button(
    keyboard: InlineKeyboardMarkup,
    back_callback: str,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    rows = list(keyboard.inline_keyboard) + [
        [InlineKeyboardButton(text=t("back_button", lang), callback_data=back_callback)]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def period_keyboard(lang: str = "ru", prefix: str = "stats") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("period_day", lang), callback_data=f"{prefix}:day"),
                InlineKeyboardButton(text=t("period_week", lang), callback_data=f"{prefix}:week"),
            ],
            [
                InlineKeyboardButton(text=t("period_month", lang), callback_data=f"{prefix}:month"),
                InlineKeyboardButton(
                    text=t("period_3months", lang),
                    callback_data=f"{prefix}:3months",
                ),
            ],
            [InlineKeyboardButton(text=t("period_year", lang), callback_data=f"{prefix}:year")],
            [
                InlineKeyboardButton(
                    text=t("period_custom", lang),
                    callback_data="stats_custom",
                )
            ],
        ]
    )
