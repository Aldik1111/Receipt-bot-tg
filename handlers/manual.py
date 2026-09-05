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
from i18n import LANGUAGES, t
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


from handlers.receipt import _default_payment_fields, _receipt_draft_view
from handlers.stats import _budget_warnings_text
from services.transactions import (
    create_quick_transaction,
    parse_quick_add_request,
)


router = Router(name="manual")

QUICK_ADD_RE = re.compile(
    r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*(.*)$",
    re.DOTALL,
)


def _add_type_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("type_expense", lang), callback_data="type:expense"),
                InlineKeyboardButton(text=t("type_income", lang), callback_data="type:income"),
            ]
        ]
    )


def _add_back_keyboard(lang: str, step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=t("back_button", lang), callback_data=f"add_back:{step}")
        ]]
    )


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    db.ensure_user(message.from_user.id, message.from_user.username)
    lang = db.get_user_language(message.from_user.id)
    await message.answer(t("add_what", lang), reply_markup=_add_type_keyboard(lang))
    await state.set_state(ManualEntry.choosing_type)

@router.callback_query(ManualEntry.choosing_type, F.data.startswith("type:"))
async def add_choose_type(callback: CallbackQuery, state: FSMContext):
    tx_type = callback.data.split(":", 1)[1]
    lang = db.get_user_language(callback.from_user.id)
    await state.update_data(tx_type=tx_type)
    await callback.message.edit_text(
        t("enter_amount", lang),
        reply_markup=_add_back_keyboard(lang, "type"),
    )
    await state.set_state(ManualEntry.entering_amount)
    await callback.answer()

@router.message(ManualEntry.entering_amount)
async def add_enter_amount(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer(t("amount_example", lang))
        return

    await state.update_data(amount=amount)
    await message.answer(
        t("choose_category", lang),
        reply_markup=with_back_button(
            categories_keyboard(message.from_user.id, "cat"),
            "add_back:amount",
            lang,
        ),
    )
    await state.set_state(ManualEntry.choosing_category)

@router.callback_query(ManualEntry.choosing_category, F.data.startswith("cat:"))
async def add_choose_category(callback: CallbackQuery, state: FSMContext):
    category_id = int(callback.data.split(":", 1)[1])
    lang = db.get_user_language(callback.from_user.id)
    await state.update_data(category_id=category_id)
    await callback.message.edit_text(
        t("choose_payment", lang),
        reply_markup=with_back_button(
            payments_keyboard(callback.from_user.id, "pay"),
            "add_back:category",
            lang,
        ),
    )
    await state.set_state(ManualEntry.choosing_payment)
    await callback.answer()

@router.callback_query(ManualEntry.choosing_payment, F.data.startswith("pay:"))
async def add_choose_payment(callback: CallbackQuery, state: FSMContext):
    payment_id = int(callback.data.split(":", 1)[1])
    lang = db.get_user_language(callback.from_user.id)
    await state.update_data(payment_method_id=payment_id)
    await callback.message.edit_text(
        t("enter_description", lang),
        reply_markup=_add_back_keyboard(lang, "payment"),
    )
    await state.set_state(ManualEntry.entering_description)
    await callback.answer()


@router.callback_query(F.data.startswith("add_back:"))
async def add_go_back(callback: CallbackQuery, state: FSMContext):
    step = callback.data.split(":", 1)[1]
    lang = db.get_user_language(callback.from_user.id)
    user_id = callback.from_user.id
    if step == "type":
        await state.set_state(ManualEntry.choosing_type)
        await callback.message.edit_text(t("add_what", lang), reply_markup=_add_type_keyboard(lang))
    elif step == "amount":
        await state.set_state(ManualEntry.entering_amount)
        await callback.message.edit_text(
            t("enter_amount", lang),
            reply_markup=_add_back_keyboard(lang, "type"),
        )
    elif step == "category":
        await state.set_state(ManualEntry.choosing_category)
        await callback.message.edit_text(
            t("choose_category", lang),
            reply_markup=with_back_button(
                categories_keyboard(user_id, "cat"),
                "add_back:amount",
                lang,
            ),
        )
    elif step == "payment":
        await state.set_state(ManualEntry.choosing_payment)
        await callback.message.edit_text(
            t("choose_payment", lang),
            reply_markup=with_back_button(
                payments_keyboard(user_id, "pay"),
                "add_back:category",
                lang,
            ),
        )
    await callback.answer()

@router.message(ManualEntry.entering_description)
async def add_enter_description(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(text_hint(db.get_user_language(message.from_user.id)))
        return
    data = await state.get_data()
    description = None if message.text.strip() == "-" else message.text.strip()

    now = db.user_now(message.from_user.id)
    tx_id = db.add_transaction(
        user_id=message.from_user.id,
        tx_type=data["tx_type"],
        amount=data["amount"],
        category_id=data["category_id"],
        payment_method_id=data["payment_method_id"],
        store=None,
        description=description,
        op_date=now.date().isoformat(),
        op_time=now.strftime("%H:%M"),
    )
    db.touch_activity(message.from_user.id, now.date().isoformat())
    await state.clear()
    emoji = "💰" if data["tx_type"] == "income" else "💸"
    lang = db.get_user_language(message.from_user.id)
    text = f"{emoji} {t('recorded', lang, amount=money(data['amount']), desc=hx(description) or t('no_description', lang))}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("edit_button", lang), callback_data=f"tx_open:{tx_id}")]]
    )
    if data["tx_type"] == "expense":
        warning = _budget_warnings_text(message.from_user.id, [data["category_id"]])
        if warning:
            text = f"{text}\n\n{warning}"
    await message.answer(text, reply_markup=keyboard)

@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def quick_add(message: Message, state: FSMContext):
    db.ensure_user(message.from_user.id, message.from_user.username)

    if message.forward_date:
        user_id = message.from_user.id
        s = db.get_settings(user_id)
        if s and s["bank_import_enabled"] and bank_import.looks_like_bank_notification(message.text):
            parsed = bank_import.parse_bank_notification(message.text)
            lang = db.get_user_language(user_id)
            if not parsed:
                await message.answer(t("bank_unparsed", lang))
                return
            # message_id уникален только внутри чата; user_id не даёт
            # черновикам двух пользователей перезаписать друг друга.
            draft_id = f"bank_{user_id}_{message.message_id}"
            op_date = db.user_today(user_id).isoformat()
            fingerprint = bank_fingerprint(
                parsed["amount"], parsed["store"], op_date, message.text or ""
            )
            seen = db.find_fingerprint(user_id, "bank", [fingerprint])
            pm_id, pm_name = _default_payment_fields(user_id)
            draft = {
                "store": parsed["store"],
                "date": op_date,
                "time": None,
                "items": [{
                    "name": parsed["store"] or t("bank_item_fallback", lang),
                    "price": parsed["amount"],
                    "category": categorize(parsed["store"] or ""),
                }],
                "total": parsed["amount"],
                "raw_text": "",
                "source": "bank_notification",
                "tx_type": parsed["type"],
                "ambiguous_type": bool(parsed.get("ambiguous")),
                "payment_method_id": pm_id,
                "payment_name": pm_name,
                "fingerprints": [fingerprint],
            }
            if seen:
                draft["duplicate_warning"] = seen
            db.save_state(RECEIPT_SCOPE, draft_id, draft, user_id=user_id)
            text, keyboard = _receipt_draft_view(draft, draft_id, lang)
            if parsed.get("ambiguous"):
                text = t("bank_ambiguous", lang) + text
            await message.answer(text, reply_markup=keyboard)
            return
        # импорт выключен или не похоже на банк - падаем ниже, обрабатываем как обычный текст

    request = parse_quick_add_request(
        message.text,
        db.user_today(message.from_user.id),
    )
    if request is None:
        await message.answer(t("quick_add_hint", db.get_user_language(message.from_user.id)))
        return

    result = await asyncio.to_thread(
        create_quick_transaction,
        message.from_user.id,
        request.amount_text,
        request.description,
        request.op_date,
    )
    if result is None:
        await message.answer(t("quick_add_bad_amount", db.get_user_language(message.from_user.id)))
        return

    emoji = "💰" if result.tx_type == "income" else "💸"
    lang = db.get_user_language(message.from_user.id)
    text = (
        f"{emoji} {t('recorded', lang, amount=money(result.amount), desc=hx(result.description) or t('no_description', lang))}\n"
        f"{t('category_label', lang)}: {hx(result.category_name)}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=t("edit_button", lang),
                callback_data=f"tx_open:{result.transaction_id}",
            )
        ]]
    )
    if result.tx_type == "expense":
        warning = _budget_warnings_text(
            message.from_user.id,
            [result.category_id],
        )
        if warning:
            text = f"{text}\n\n{warning}"
    await message.answer(text, reply_markup=keyboard)
