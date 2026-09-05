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
from i18n import format_money, t, type_label
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




router = Router(name="recurring")


def _recurring_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    lang = db.get_user_language(user_id)
    items = db.get_recurring_list(user_id)
    lines = [t("recurring_title", lang), ""]
    buttons = []
    if not items:
        lines.append(t("no_recurring", lang))
    for r in items:
        status = "✅" if r["active"] else "⏸"
        emoji = "💸" if r["type"] == "expense" else "💰"
        label = hx(r["description"] or r["category_name"]) or t("unnamed", lang)
        lines.append(t(
            "recurring_line",
            lang,
            status=status,
            emoji=emoji,
            amount=format_money(r["amount"], lang),
            label=label,
            day=r["day_of_month"],
        ))
        buttons.append([
            InlineKeyboardButton(
                text=f"✏️ {r['description'] or r['category_name'] or t('payment_fallback', lang)}"[:60],
                callback_data=f"rec_open:{r['id']}",
            ),
        ])
    buttons.append([InlineKeyboardButton(text=t("add_button", lang), callback_data="rec_add")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

def _recurring_detail_view(user_id: int, rec_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    lang = db.get_user_language(user_id)
    r = db.get_recurring_by_id(user_id, rec_id)
    if not r:
        return None
    status = t("rec_status_on", lang) if r["active"] else t("rec_status_off", lang)
    cat_display = f"{r['category_emoji']} {hx(r['category_name'])}" if r["category_name"] else t("dash", lang)
    lines = [
        f"🔁 <b>{hx(r['description']) or t('unnamed', lang)}</b>",
        t("rec_status_label", lang, status=status),
        t("tx_type_label", lang, name=type_label(r["type"], lang)),
        t("rec_amount_label", lang, amount=format_money(r["amount"], lang)),
        t("rec_day_label", lang, day=r["day_of_month"]),
        t("tx_category_label", lang, name=cat_display),
        t("receipt_pay", lang, name=hx(r["payment_name"]) or t("dash", lang)),
    ]
    toggle_label = t("btn_pause", lang) if r["active"] else t("btn_resume", lang)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t("btn_amount", lang), callback_data=f"rec_amount:{rec_id}"),
            InlineKeyboardButton(text=t("btn_day", lang), callback_data=f"rec_day:{rec_id}"),
        ],
        [
            InlineKeyboardButton(text=t("btn_category", lang), callback_data=f"rec_editcat:{rec_id}"),
            InlineKeyboardButton(text=t("btn_payment", lang), callback_data=f"rec_editpay:{rec_id}"),
        ],
        [InlineKeyboardButton(text=t("btn_description", lang), callback_data=f"rec_desc:{rec_id}")],
        [InlineKeyboardButton(text=toggle_label, callback_data=f"rec_toggle:{rec_id}")],
        [InlineKeyboardButton(text=t("btn_delete", lang), callback_data=f"rec_del:{rec_id}")],
        [InlineKeyboardButton(text=t("btn_to_list", lang), callback_data="rec_list")],
    ])
    return "\n".join(lines), keyboard

@router.message(Command("recurring"))
async def cmd_recurring(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _recurring_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data == "rec_list")
async def rec_list_back(callback: CallbackQuery):
    text, keyboard = _recurring_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("rec_open:"))
async def rec_open(callback: CallbackQuery):
    rec_id = int(callback.data.split(":", 1)[1])
    view = _recurring_detail_view(callback.from_user.id, rec_id)
    if not view:
        await callback.answer(t("rec_not_found", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "rec_add")
async def rec_add_start(callback: CallbackQuery, state: FSMContext):
    lang = db.get_user_language(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("type_expense", lang), callback_data="rec_type:expense"),
        InlineKeyboardButton(text=t("type_income", lang), callback_data="rec_type:income"),
    ]])
    await callback.message.edit_text(t("rec_what", lang), reply_markup=keyboard)
    await state.set_state(RecurringEntry.choosing_type)
    await callback.answer()

@router.callback_query(RecurringEntry.choosing_type, F.data.startswith("rec_type:"))
async def rec_choose_type(callback: CallbackQuery, state: FSMContext):
    tx_type = callback.data.split(":", 1)[1]
    await state.update_data(rec_type=tx_type)
    await state.set_state(RecurringEntry.entering_amount)
    await callback.message.edit_text(t("enter_amount", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(RecurringEntry.entering_amount)
async def rec_enter_amount(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer(t("amount_positive_short", lang))
        return
    await state.update_data(rec_amount=amount)
    await state.set_state(RecurringEntry.choosing_category)
    await message.answer(t("choose_category", lang), reply_markup=categories_keyboard(message.from_user.id, "rec_cat"))

@router.callback_query(RecurringEntry.choosing_category, F.data.startswith("rec_cat:"))
async def rec_choose_category(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split(":", 1)[1])
    await state.update_data(rec_cat_id=cat_id)
    await state.set_state(RecurringEntry.choosing_payment)
    await callback.message.edit_text(
        t("choose_payment", db.get_user_language(callback.from_user.id)),
        reply_markup=payments_keyboard(callback.from_user.id, "rec_pay"),
    )
    await callback.answer()

@router.callback_query(RecurringEntry.choosing_payment, F.data.startswith("rec_pay:"))
async def rec_choose_payment(callback: CallbackQuery, state: FSMContext):
    pm_id = int(callback.data.split(":", 1)[1])
    await state.update_data(rec_pay_id=pm_id)
    await state.set_state(RecurringEntry.entering_day)
    await callback.message.edit_text(t("enter_day_of_month", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(RecurringEntry.entering_day)
async def rec_enter_day(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    try:
        day = int(message.text.strip())
        if not (1 <= day <= 28):
            raise ValueError
    except ValueError:
        await message.answer(t("day_range", lang))
        return
    await state.update_data(rec_day=day)
    await state.set_state(RecurringEntry.entering_description)
    await message.answer(t("enter_rec_name", lang))

@router.message(RecurringEntry.entering_description)
async def rec_enter_description(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    data = await state.get_data()
    db.add_recurring(
        user_id=message.from_user.id,
        tx_type=data["rec_type"],
        amount=data["rec_amount"],
        category_id=data["rec_cat_id"],
        payment_method_id=data["rec_pay_id"],
        description=message.text.strip(),
        day_of_month=data["rec_day"],
    )
    await state.clear()
    text, keyboard = _recurring_view(message.from_user.id)
    await message.answer(t("rec_added", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("rec_toggle:"))
async def rec_toggle(callback: CallbackQuery):
    rec_id = int(callback.data.split(":", 1)[1])
    db.toggle_recurring_active(callback.from_user.id, rec_id)
    view = _recurring_detail_view(callback.from_user.id, rec_id)
    if not view:
        text, keyboard = _recurring_view(callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        text, keyboard = view
        await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("rec_del:"))
async def rec_delete(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    rec_id = int(callback.data.split(":", 1)[1])
    db.delete_recurring(callback.from_user.id, rec_id)
    text, keyboard = _recurring_view(callback.from_user.id)
    await callback.message.edit_text(t("deleted", lang, text=text), reply_markup=keyboard)
    await callback.answer(t("removed", lang))

@router.callback_query(F.data.startswith("rec_amount:"))
async def rec_edit_amount_start(callback: CallbackQuery, state: FSMContext):
    rec_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_rec_id=rec_id)
    await state.set_state(RecurringEdit.entering_amount)
    await callback.message.edit_text(t("enter_rec_amount", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(RecurringEdit.entering_amount)
async def rec_edit_amount_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer(t("amount_positive_short", lang))
        return
    data = await state.get_data()
    rec_id = data["edit_rec_id"]
    if not db.update_recurring(message.from_user.id, rec_id, amount=amount):
        await state.clear()
        await message.answer(t("rec_not_found_long", lang))
        return
    await state.clear()
    view = _recurring_detail_view(message.from_user.id, rec_id)
    text, keyboard = view or _recurring_view(message.from_user.id)
    await message.answer(t("rec_amount_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("rec_day:"))
async def rec_edit_day_start(callback: CallbackQuery, state: FSMContext):
    rec_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_rec_id=rec_id)
    await state.set_state(RecurringEdit.entering_day)
    await callback.message.edit_text(t("enter_day_of_month", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(RecurringEdit.entering_day)
async def rec_edit_day_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    try:
        day = int(message.text.strip())
        if not (1 <= day <= 28):
            raise ValueError
    except ValueError:
        await message.answer(t("day_range_short", lang))
        return
    data = await state.get_data()
    rec_id = data["edit_rec_id"]
    if not db.update_recurring(message.from_user.id, rec_id, day_of_month=day):
        await state.clear()
        await message.answer(t("rec_not_found_long", lang))
        return
    await state.clear()
    view = _recurring_detail_view(message.from_user.id, rec_id)
    text, keyboard = view or _recurring_view(message.from_user.id)
    await message.answer(t("day_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("rec_desc:"))
async def rec_edit_desc_start(callback: CallbackQuery, state: FSMContext):
    rec_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_rec_id=rec_id)
    await state.set_state(RecurringEdit.entering_description)
    await callback.message.edit_text(t("enter_rec_desc", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(RecurringEdit.entering_description)
async def rec_edit_desc_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text or not message.text.strip():
        await message.answer(text_hint(lang))
        return
    data = await state.get_data()
    rec_id = data["edit_rec_id"]
    if not db.update_recurring(message.from_user.id, rec_id, description=message.text.strip()):
        await state.clear()
        await message.answer(t("rec_not_found_long", lang))
        return
    await state.clear()
    view = _recurring_detail_view(message.from_user.id, rec_id)
    text, keyboard = view or _recurring_view(message.from_user.id)
    await message.answer(t("description_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("rec_editcat:"))
async def rec_edit_category_start(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    rec_id = int(callback.data.split(":", 1)[1])
    keyboard = _with_back_button(
        categories_keyboard(callback.from_user.id, f"rec_setcat:{rec_id}"),
        back_callback=f"rec_open:{rec_id}",
        lang=lang,
    )
    await callback.message.edit_text(t("enter_new_category", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("rec_setcat:"))
async def rec_edit_category_apply(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    _, rec_id, cat_id = callback.data.split(":")
    if not db.update_recurring(callback.from_user.id, int(rec_id), category_id=int(cat_id)):
        await callback.answer(t("rec_cat_fail", lang), show_alert=True)
        return
    view = _recurring_detail_view(callback.from_user.id, int(rec_id))
    if not view:
        await callback.answer(t("rec_not_found", lang), show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(t("category_updated", lang, text=text), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("rec_editpay:"))
async def rec_edit_payment_start(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    rec_id = int(callback.data.split(":", 1)[1])
    keyboard = _with_back_button(
        payments_keyboard(callback.from_user.id, f"rec_setpay:{rec_id}"),
        back_callback=f"rec_open:{rec_id}",
        lang=lang,
    )
    await callback.message.edit_text(t("enter_new_payment", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("rec_setpay:"))
async def rec_edit_payment_apply(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    _, rec_id, pm_id = callback.data.split(":")
    if not db.update_recurring(callback.from_user.id, int(rec_id), payment_method_id=int(pm_id)):
        await callback.answer(t("rec_pay_fail", lang), show_alert=True)
        return
    view = _recurring_detail_view(callback.from_user.id, int(rec_id))
    if not view:
        await callback.answer(t("rec_not_found", lang), show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(t("payment_updated", lang, text=text), reply_markup=keyboard)
    await callback.answer()
