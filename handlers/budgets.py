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
from i18n import format_money, t
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




router = Router(name="budgets")


def _budget_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    lang = db.get_user_language(user_id)
    budgets = db.get_budgets(user_id)
    today = db.user_today(user_id)
    start = today.replace(day=1)
    lines = [t("budgets_title", lang), ""]
    buttons = []
    has_overall = False
    if not budgets:
        lines.append(t("no_budgets", lang))
    for b in budgets:
        is_overall = b["category_id"] is None
        if is_overall:
            has_overall = True
            spent = db.get_total_expense(user_id, start.isoformat(), today.isoformat())
            name = t("overall_expenses", lang)
            edit_cb = "bud_overall"
        else:
            spent = db.get_category_spent(user_id, b["category_id"], start.isoformat(), today.isoformat())
            name = b["category_name"] or "?"
            edit_cb = f"bud_pick:{b['category_id']}"
        pct = (spent / b["monthly_limit"] * 100) if b["monthly_limit"] > 0 else 0
        emoji = "🔴" if pct >= 100 else ("🟡" if pct >= 80 else "🟢")
        lines.append(
            f"{emoji} {hx(name)}: {format_money(spent, lang)} / "
            f"{format_money(b['monthly_limit'], lang)} ({pct:.0f}%)"
        )
        buttons.append([
            InlineKeyboardButton(text=f"✏️ {name}", callback_data=edit_cb),
            InlineKeyboardButton(text=t("delete_button", lang), callback_data=f"bud_del:{b['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text=t("btn_cat_limit", lang), callback_data="bud_add")])
    if not has_overall:
        buttons.append([InlineKeyboardButton(text=t("btn_overall_budget", lang), callback_data="bud_overall")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

@router.message(Command("budget"))
async def cmd_budget(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _budget_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data == "bud_add")
async def bud_add_start(callback: CallbackQuery):
    keyboard = categories_keyboard(callback.from_user.id, "bud_pick")
    await callback.message.edit_text(
        t("budget_pick_cat", db.get_user_language(callback.from_user.id)),
        reply_markup=keyboard,
    )
    await callback.answer()

@router.callback_query(F.data == "bud_overall")
async def bud_overall_start(callback: CallbackQuery, state: FSMContext):
    await state.update_data(budget_cat_id=None, budget_overall=True)
    await state.set_state(BudgetEntry.entering_limit)
    await callback.message.edit_text(
        t("budget_enter_overall", db.get_user_language(callback.from_user.id))
    )
    await callback.answer()

@router.callback_query(F.data.startswith("bud_pick:"))
async def bud_pick_category(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split(":", 1)[1])
    await state.update_data(budget_cat_id=cat_id, budget_overall=False)
    await state.set_state(BudgetEntry.entering_limit)
    await callback.message.edit_text(
        t("budget_enter_cat", db.get_user_language(callback.from_user.id))
    )
    await callback.answer()

@router.message(BudgetEntry.entering_limit)
async def bud_enter_limit(message: Message, state: FSMContext):
    limit = parse_positive_amount(message.text)
    lang = db.get_user_language(message.from_user.id)
    if limit is None:
        await message.answer(t("amount_positive_big", lang))
        return
    data = await state.get_data()
    cat_id = None if data.get("budget_overall") else data.get("budget_cat_id")
    db.set_budget(message.from_user.id, cat_id, limit)
    await state.clear()
    text, keyboard = _budget_view(message.from_user.id)
    await message.answer(t("budget_set", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("bud_del:"))
async def bud_delete(callback: CallbackQuery):
    budget_id = int(callback.data.split(":", 1)[1])
    db.delete_budget(callback.from_user.id, budget_id)
    text, keyboard = _budget_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(t("removed", db.get_user_language(callback.from_user.id)))
