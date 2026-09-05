import asyncio
import io
import json
import logging
import os
import re
import tempfile
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

import backup
import bank_import
import charts
import db
import export
from categorizer import categorize, categorize_many, categorize_smart
from config import ADMIN_USER_ID, GEMINI_DAILY_LIMIT
from fingerprints import (
    bank_fingerprint, photo_sha256_fingerprint, photo_telegram_fingerprint,
)
from formatting import hx, money, parse_positive_amount
from gemini_engine import generate_insight_text
from i18n import format_date, format_money, t, type_label
from image_prep import (
    MAX_RECEIPT_BYTES, ReceiptImageError, prepare_receipt_image, sha256_file,
)
from money import tiyn_to_tenge
from receipt_pipeline import extract_receipt
from timeutil import TIMEZONE_CHOICES

from handlers.common import *
from keyboards.common import (
    categories_keyboard, payments_keyboard, period_keyboard, with_back_button,
)

_with_back_button = with_back_button
logger = logging.getLogger(__name__)


from handlers.stats import _period_bounds


router = Router(name="recent")

PAGE_SIZE = 8
TYPE_EMOJI = {"expense": "💸", "income": "💰", "transfer": "🔄"}


def _type_labels(lang: str) -> dict[str, str]:
    return {
        "expense": type_label("expense", lang),
        "income": type_label("income", lang),
        "transfer": type_label("transfer", lang),
    }


@router.callback_query(F.data.startswith("recent_from:"))
async def recent_from_stats(callback: CallbackQuery):
    _, date_from, date_to = callback.data.split(":")
    text, keyboard = _recent_view(callback.from_user.id, date_from, date_to, 0)
    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()

def _get_list_context(
    user_id: int,
) -> tuple[str | None, str | None, int, str | None, int | None, str | None]:
    saved = db.load_state(LIST_SCOPE, str(user_id))
    if not saved:
        return None, None, 0, None, None, None
    if isinstance(saved, list):
        date_from, date_to, page = saved
        return date_from, date_to, page, None, None, None
    return (
        saved.get("date_from"),
        saved.get("date_to"),
        int(saved.get("page", 0)),
        saved.get("query"),
        saved.get("category_id"),
        saved.get("tx_type"),
    )

def _recent_view(
    user_id: int,
    date_from: str | None,
    date_to: str | None,
    page: int,
    query: str | None = None,
    category_id: int | None = None,
    tx_type: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    db.save_state(
        LIST_SCOPE,
        str(user_id),
        {
            "date_from": date_from,
            "date_to": date_to,
            "page": page,
            "query": query,
            "category_id": category_id,
            "tx_type": tx_type,
        },
        user_id=user_id,
    )

    total = db.count_all_transactions(
        user_id,
        date_from,
        date_to,
        query=query,
        category_id=category_id,
        tx_type=tx_type,
    )
    rows = db.get_recent_transactions(
        user_id,
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
        date_from=date_from,
        date_to=date_to,
        query=query,
        category_id=category_id,
        tx_type=tx_type,
    )

    lang = db.get_user_language(user_id)
    labels = _type_labels(lang)

    if date_from and date_to:
        header = t(
            "recent_period_title",
            lang,
            start=format_date(date_from, lang),
            end=format_date(date_to, lang),
        )
    else:
        header = t("recent_title", lang)

    filters = []
    if query:
        filters.append(t("filter_search", lang, query=hx(query)))
    if category_id is not None:
        filters.append(
            t("filter_category", lang, name=hx(db.get_category_name(user_id, category_id)) or "?")
        )
    if tx_type:
        filters.append(t("filter_type", lang, name=labels.get(tx_type, tx_type)))
    if filters:
        header += "\n" + t("filters_line", lang, filters=", ".join(filters))

    lines = [header, ""]
    buttons = [
        [
            InlineKeyboardButton(text=t("btn_search", lang), callback_data="recent_search"),
            InlineKeyboardButton(text=t("btn_filters", lang), callback_data="recent_filters"),
        ]
    ]
    if filters:
        buttons.append([
            InlineKeyboardButton(
                text=t("btn_clear_filters", lang),
                callback_data="recent_clear",
            )
        ])
    if not rows:
        lines.append(t("nothing_found", lang))
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
    for r in rows:
        emoji = TYPE_EMOJI.get(r["type"], "•")
        label = hx(r["description"] or r["store"]) or t("no_description", lang)
        cat = f" [{r['category_emoji']} {hx(r['category_name'])}]" if r["category_name"] else ""
        display_date = format_date(r["op_date"], lang)
        amount = format_money(r["amount"], lang)
        lines.append(f"{emoji} {display_date} · {amount} · {label}{cat}")
        buttons.append([InlineKeyboardButton(
            text=f"✏️ {display_date} · {amount} · {label}"[:60],
            callback_data=f"tx_open:{r['id']}",
        )])

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines.append("\n" + t("page_of", lang, page=page + 1, total=total_pages))

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text=t("nav_earlier", lang), callback_data="recent_page:prev"))
    if (page + 1) * PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton(text=t("nav_later", lang), callback_data="recent_page:next"))
    if nav_row:
        buttons.append(nav_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

@router.message(Command("recent"))
async def cmd_recent(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _recent_view(message.from_user.id, None, None, 0)
    await message.answer(text, reply_markup=keyboard)

@router.message(Command("today"))
async def cmd_today(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    today = db.user_today(message.from_user.id).isoformat()
    text, keyboard = _recent_view(
        message.from_user.id,
        today,
        today,
        0,
    )
    await message.answer(text, reply_markup=keyboard)

@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    db.ensure_user(message.from_user.id, message.from_user.username)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        text, keyboard = _recent_view(
            message.from_user.id,
            None,
            None,
            0,
            query=parts[1].strip()[:100],
        )
        await message.answer(text, reply_markup=keyboard)
        return
    await state.set_state(RecentSearch.entering_query)
    await message.answer(t("search_prompt", db.get_user_language(message.from_user.id)))

@router.callback_query(F.data == "recent_search")
async def recent_search_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RecentSearch.entering_query)
    await callback.message.answer(t("search_prompt", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(RecentSearch.entering_query)
async def recent_search_apply(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if not query:
        await message.answer(t("search_empty", db.get_user_language(message.from_user.id)))
        return
    await state.clear()
    date_from, date_to, _, _, category_id, tx_type = _get_list_context(
        message.from_user.id
    )
    text, keyboard = _recent_view(
        message.from_user.id,
        date_from,
        date_to,
        0,
        query=query[:100],
        category_id=category_id,
        tx_type=tx_type,
    )
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data == "recent_filters")
async def recent_filters(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t("type_expense", lang), callback_data="recent_type:expense"),
            InlineKeyboardButton(text=t("type_income", lang), callback_data="recent_type:income"),
        ],
        [InlineKeyboardButton(text=t("type_transfer", lang), callback_data="recent_type:transfer")],
        [InlineKeyboardButton(text=t("btn_category", lang), callback_data="recent_catpage:0")],
        [InlineKeyboardButton(text=t("btn_period", lang), callback_data="recent_periods")],
        [InlineKeyboardButton(text=t("back_button", lang), callback_data="recent_back")],
    ])
    await callback.message.edit_text(t("choose_filter", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "recent_periods")
async def recent_periods(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t("period_day", lang), callback_data="recent_period:day"),
            InlineKeyboardButton(text=t("period_week", lang), callback_data="recent_period:week"),
        ],
        [
            InlineKeyboardButton(text=t("period_month", lang), callback_data="recent_period:month"),
            InlineKeyboardButton(text=t("period_year", lang), callback_data="recent_period:year"),
        ],
        [InlineKeyboardButton(text=t("period_all", lang), callback_data="recent_period:all")],
        [InlineKeyboardButton(text=t("back_button", lang), callback_data="recent_filters")],
    ])
    await callback.message.edit_text(t("ask_period_show", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("recent_period:"))
async def recent_period_apply(callback: CallbackQuery):
    period = callback.data.split(":", 1)[1]
    if period == "all":
        date_from = date_to = None
    else:
        date_from, date_to, _ = _period_bounds(
            period,
            db.user_today(callback.from_user.id),
        )
    _, _, _, query, category_id, tx_type = _get_list_context(
        callback.from_user.id
    )
    text, keyboard = _recent_view(
        callback.from_user.id,
        date_from,
        date_to,
        0,
        query=query,
        category_id=category_id,
        tx_type=tx_type,
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("recent_type:"))
async def recent_type_apply(callback: CallbackQuery):
    tx_type = callback.data.split(":", 1)[1]
    date_from, date_to, _, query, category_id, _ = _get_list_context(
        callback.from_user.id
    )
    text, keyboard = _recent_view(
        callback.from_user.id,
        date_from,
        date_to,
        0,
        query=query,
        category_id=category_id,
        tx_type=tx_type,
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("recent_catpage:"))
async def recent_category_page(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    page = max(0, int(callback.data.split(":", 1)[1]))
    categories = db.get_categories(callback.from_user.id)
    page_size = 8
    start = page * page_size
    rows = [
        [InlineKeyboardButton(
            text=f"{category['emoji']} {category['name']}"[:60],
            callback_data=f"recent_cat:{category['id']}",
        )]
        for category in categories[start:start + page_size]
    ]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="◀️",
            callback_data=f"recent_catpage:{page - 1}",
        ))
    if start + page_size < len(categories):
        navigation.append(InlineKeyboardButton(
            text="▶️",
            callback_data=f"recent_catpage:{page + 1}",
        ))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text=t("back_button", lang), callback_data="recent_filters")])
    await callback.message.edit_text(
        t("choose_category", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()

@router.callback_query(F.data.startswith("recent_cat:"))
async def recent_category_apply(callback: CallbackQuery):
    category_id = int(callback.data.split(":", 1)[1])
    date_from, date_to, _, query, _, tx_type = _get_list_context(
        callback.from_user.id
    )
    text, keyboard = _recent_view(
        callback.from_user.id,
        date_from,
        date_to,
        0,
        query=query,
        category_id=category_id,
        tx_type=tx_type,
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "recent_clear")
async def recent_clear_filters(callback: CallbackQuery):
    text, keyboard = _recent_view(callback.from_user.id, None, None, 0)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(t("filters_cleared", db.get_user_language(callback.from_user.id)))

@router.callback_query(F.data.startswith("recent_page:"))
async def recent_page_nav(callback: CallbackQuery):
    direction = callback.data.split(":", 1)[1]
    date_from, date_to, page, query, category_id, tx_type = _get_list_context(
        callback.from_user.id
    )
    page = page + 1 if direction == "next" else max(0, page - 1)
    text, keyboard = _recent_view(
        callback.from_user.id,
        date_from,
        date_to,
        page,
        query=query,
        category_id=category_id,
        tx_type=tx_type,
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "recent_back")
async def recent_back(callback: CallbackQuery):
    date_from, date_to, page, query, category_id, tx_type = _get_list_context(
        callback.from_user.id
    )
    text, keyboard = _recent_view(
        callback.from_user.id,
        date_from,
        date_to,
        page,
        query=query,
        category_id=category_id,
        tx_type=tx_type,
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

def _tx_detail_view(user_id: int, tx_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    lang = db.get_user_language(user_id)
    r = db.get_transaction_by_id(user_id, tx_id)
    if not r:
        return None

    emoji = TYPE_EMOJI.get(r["type"], "•")
    if r["type"] == "transfer":
        goal_name = hx(r["goal_name"]) or t("goal_fallback", lang)
        if (r["goal_delta"] or 0) >= 0:
            type_name = t("transfer_to_goal", lang, name=goal_name)
        else:
            type_name = t("transfer_from_goal", lang, name=goal_name)
    else:
        type_name = type_label(r["type"], lang)
    display_date = format_date(r["op_date"], lang) if r["op_date"] else t("dash", lang)
    cat_display = f"{r['category_emoji']} {hx(r['category_name'])}" if r["category_name"] else t("dash", lang)
    date_line = t("tx_date_label", lang, date=display_date)
    if r["op_time"]:
        date_line += t("tx_time_label", lang, time=r["op_time"])
    lines = [
        f"{emoji} <b>{format_money(r['amount'], lang)}</b>",
        t("tx_type_label", lang, name=type_name),
        date_line,
        t("tx_category_label", lang, name=cat_display),
        t("tx_payment_label", lang, name=hx(r["payment_name"]) or t("dash", lang)),
        t("tx_store_label", lang, name=hx(r["store"]) or t("dash", lang)),
        t("tx_desc_label", lang, name=hx(r["description"]) or t("dash", lang)),
    ]
    text = "\n".join(lines)

    if r["type"] == "transfer":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=t("btn_payment", lang), callback_data=f"tx_pay:{tx_id}"),
                    InlineKeyboardButton(text=t("btn_description", lang), callback_data=f"tx_desc:{tx_id}"),
                ],
                [InlineKeyboardButton(text=t("btn_date", lang), callback_data=f"tx_date:{tx_id}")],
                [InlineKeyboardButton(text=t("btn_delete", lang), callback_data=f"tx_del:{tx_id}")],
                [InlineKeyboardButton(text=t("btn_to_list", lang), callback_data="recent_back")],
            ]
        )
    else:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=t("btn_amount", lang), callback_data=f"tx_amount:{tx_id}"),
                    InlineKeyboardButton(text=t("btn_category", lang), callback_data=f"tx_cat:{tx_id}"),
                ],
                [
                    InlineKeyboardButton(text=t("btn_payment", lang), callback_data=f"tx_pay:{tx_id}"),
                    InlineKeyboardButton(text=t("btn_description", lang), callback_data=f"tx_desc:{tx_id}"),
                ],
                [
                    InlineKeyboardButton(text=t("btn_date", lang), callback_data=f"tx_date:{tx_id}"),
                    InlineKeyboardButton(text=t("btn_store", lang), callback_data=f"tx_store:{tx_id}"),
                ],
                [InlineKeyboardButton(text=t("btn_tx_type", lang), callback_data=f"tx_type:{tx_id}")],
                [InlineKeyboardButton(text=t("btn_delete", lang), callback_data=f"tx_del:{tx_id}")],
                [InlineKeyboardButton(text=t("btn_to_list", lang), callback_data="recent_back")],
            ]
        )
    return text, keyboard

@router.callback_query(F.data.startswith("tx_open:"))
async def tx_open(callback: CallbackQuery):
    tx_id = int(callback.data.split(":", 1)[1])
    view = _tx_detail_view(callback.from_user.id, tx_id)
    if not view:
        await callback.answer(t("tx_not_found", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("tx_amount:"))
async def tx_edit_amount_start(callback: CallbackQuery, state: FSMContext):
    tx_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_tx_id=tx_id)
    await state.set_state(TransactionEdit.entering_amount)
    await callback.message.edit_text(t("enter_new_amount", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(TransactionEdit.entering_amount)
async def tx_edit_amount_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    data = await state.get_data()
    tx_id = data["edit_tx_id"]
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer(t("amount_positive", lang))
        return
    if not db.update_transaction_amount(message.from_user.id, tx_id, amount):
        await state.clear()
        await message.answer(t("transfer_amount_via_goals", lang))
        return
    await state.clear()
    view = _tx_detail_view(message.from_user.id, tx_id)
    if not view:
        await message.answer(t("tx_not_found", lang))
        await state.clear()
        return
    text, keyboard = view
    await message.answer(t("amount_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("tx_date:"))
async def tx_edit_date_start(callback: CallbackQuery, state: FSMContext):
    tx_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_tx_id=tx_id)
    await state.set_state(TransactionEdit.entering_date)
    await callback.message.edit_text(t("enter_new_date", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(TransactionEdit.entering_date)
async def tx_edit_date_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    try:
        new_date = datetime.strptime(message.text.strip(), "%d.%m.%Y").date().isoformat()
    except ValueError:
        await message.answer(t("bad_date", lang))
        return
    data = await state.get_data()
    tx_id = data["edit_tx_id"]
    if not db.update_transaction_date(message.from_user.id, tx_id, new_date):
        await state.clear()
        await message.answer(t("tx_not_found", lang))
        return
    await state.clear()
    text, keyboard = _tx_detail_view(message.from_user.id, tx_id)
    await message.answer(t("date_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("tx_store:"))
async def tx_edit_store_start(callback: CallbackQuery, state: FSMContext):
    tx_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_tx_id=tx_id)
    await state.set_state(TransactionEdit.entering_store)
    await callback.message.edit_text(t("enter_store", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(TransactionEdit.entering_store)
async def tx_edit_store_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    value = message.text.strip()
    if len(value) > 128:
        await message.answer(t("store_too_long", lang))
        return
    new_store = None if value == "-" else value
    data = await state.get_data()
    tx_id = data["edit_tx_id"]
    if not db.update_transaction_store(message.from_user.id, tx_id, new_store):
        await state.clear()
        await message.answer(t("tx_not_found", lang))
        return
    await state.clear()
    text, keyboard = _tx_detail_view(message.from_user.id, tx_id)
    await message.answer(t("store_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("tx_type:"))
async def tx_edit_type_start(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    tx_id = int(callback.data.split(":", 1)[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t("type_expense", lang), callback_data=f"tx_settype:{tx_id}:expense"),
            InlineKeyboardButton(text=t("type_income", lang), callback_data=f"tx_settype:{tx_id}:income"),
        ],
        [InlineKeyboardButton(text=t("cancel_button", lang), callback_data=f"tx_open:{tx_id}")],
    ])
    await callback.message.edit_text(t("choose_tx_type", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("tx_settype:"))
async def tx_edit_type_apply(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    _, tx_id_raw, tx_type = callback.data.split(":")
    tx_id = int(tx_id_raw)
    if not db.update_transaction_type(callback.from_user.id, tx_id, tx_type):
        await callback.answer(t("tx_type_bad", lang), show_alert=True)
        return
    view = _tx_detail_view(callback.from_user.id, tx_id)
    if not view:
        await callback.answer(t("tx_not_found", lang), show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(t("tx_type_updated", lang, text=text), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("tx_desc:"))
async def tx_edit_desc_start(callback: CallbackQuery, state: FSMContext):
    tx_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_tx_id=tx_id)
    await state.set_state(TransactionEdit.entering_description)
    await callback.message.edit_text(t("enter_new_description", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(TransactionEdit.entering_description)
async def tx_edit_desc_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    data = await state.get_data()
    tx_id = data["edit_tx_id"]
    new_desc = None if message.text.strip() == "-" else message.text.strip()
    db.update_transaction_description(message.from_user.id, tx_id, new_desc)
    await state.clear()
    view = _tx_detail_view(message.from_user.id, tx_id)
    if not view:
        await message.answer(t("tx_not_found", lang))
        return
    text, keyboard = view
    await message.answer(t("description_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("tx_cat:"))
async def tx_edit_category_start(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    tx_id = int(callback.data.split(":", 1)[1])
    keyboard = _with_back_button(
        categories_keyboard(callback.from_user.id, f"tx_setcat:{tx_id}"),
        back_callback=f"tx_open:{tx_id}",
        lang=lang,
    )
    await callback.message.edit_text(t("choose_new_category", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("tx_setcat:"))
async def tx_edit_category_apply(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    _, tx_id, cat_id = callback.data.split(":")
    user_id = callback.from_user.id
    db.update_transaction_category(user_id, int(tx_id), int(cat_id))

    # Запоминаем: то же самое описание/товар в следующий раз сразу пойдёт
    # в эту категорию, без словаря и без ИИ
    tx = db.get_transaction_by_id(user_id, int(tx_id))
    if tx and tx["description"]:
        db.learn_category(user_id, tx["description"], int(cat_id))

    view = _tx_detail_view(user_id, int(tx_id))
    if not view:
        await callback.answer(t("tx_not_found", lang), show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(t("category_updated_learned", lang, text=text), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("tx_pay:"))
async def tx_edit_payment_start(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    tx_id = int(callback.data.split(":", 1)[1])
    keyboard = _with_back_button(
        payments_keyboard(callback.from_user.id, f"tx_setpay:{tx_id}"),
        back_callback=f"tx_open:{tx_id}",
        lang=lang,
    )
    await callback.message.edit_text(t("choose_new_payment", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("tx_setpay:"))
async def tx_edit_payment_apply(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    _, tx_id, pm_id = callback.data.split(":")
    db.update_transaction_payment_method(callback.from_user.id, int(tx_id), int(pm_id))
    view = _tx_detail_view(callback.from_user.id, int(tx_id))
    if not view:
        await callback.answer(t("tx_not_found", lang), show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(t("payment_updated", lang, text=text), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("tx_del:"))
async def tx_delete_confirm(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    tx_id = int(callback.data.split(":", 1)[1])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("confirm_delete", lang), callback_data=f"tx_del_yes:{tx_id}"),
                InlineKeyboardButton(text=t("cancel_button", lang), callback_data=f"tx_open:{tx_id}"),
            ]
        ]
    )
    await callback.message.edit_text(t("delete_tx_confirm", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("tx_del_yes:"))
async def tx_delete_apply(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    tx_id = int(callback.data.split(":", 1)[1])
    if not db.delete_transaction(callback.from_user.id, tx_id):
        tx = db.get_transaction_by_id(callback.from_user.id, tx_id)
        if tx and tx["type"] == "transfer":
            await callback.answer(
                t("delete_tx_goal_block", lang),
                show_alert=True,
            )
            return
        await callback.answer(t("tx_not_found", lang), show_alert=True)
        return
    date_from, date_to, page, query, category_id, tx_type = _get_list_context(
        callback.from_user.id
    )
    text, keyboard = _recent_view(
        callback.from_user.id,
        date_from,
        date_to,
        page,
        query=query,
        category_id=category_id,
        tx_type=tx_type,
    )
    await callback.message.edit_text(t("deleted", lang, text=text), reply_markup=keyboard)
    await callback.answer()
