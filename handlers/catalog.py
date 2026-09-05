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
from formatting import hx, is_single_emoji, money, parse_positive_amount
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




router = Router(name="catalog")

PROTECTED_CATEGORY = "Прочее"
CATALOG_PAGE_SIZE = 8


def _categories_view(
    user_id: int,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    lang = db.get_user_language(user_id)
    cats = db.get_categories(user_id)
    total_pages = max(1, (len(cats) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    visible = cats[page * CATALOG_PAGE_SIZE:(page + 1) * CATALOG_PAGE_SIZE]
    text = t("catalog_categories_hint", lang)
    if total_pages > 1:
        text += "\n" + t("page_of", lang, page=page + 1, total=total_pages)

    buttons = []
    for c in visible:
        if c["name"] == PROTECTED_CATEGORY:
            buttons.append([InlineKeyboardButton(text=f"{c['emoji']} {c['name']} 🔒", callback_data="noop")])
        else:
            buttons.append([
                InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data="noop"),
                InlineKeyboardButton(text="🎨", callback_data=f"cat_emoji:{c['id']}"),
                InlineKeyboardButton(text="✏️", callback_data=f"cat_edit:{c['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"cat_del:{c['id']}"),
            ])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="◀️",
            callback_data=f"catalog_cat_page:{page - 1}",
        ))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(
            text="▶️",
            callback_data=f"catalog_cat_page:{page + 1}",
        ))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton(text=t("btn_add_category", lang), callback_data="cat_add")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)

@router.message(Command("categories"))
async def cmd_categories(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _categories_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data.startswith("catalog_cat_page:"))
async def categories_page(callback: CallbackQuery):
    page = int(callback.data.split(":", 1)[1])
    text, keyboard = _categories_view(callback.from_user.id, page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer(t("protected_category", db.get_user_language(callback.from_user.id)))

@router.callback_query(F.data == "cat_add")
async def cat_add_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(t("enter_category_name", db.get_user_language(callback.from_user.id)))
    await state.set_state(CategoryEntry.entering_name)
    await callback.answer()

@router.message(CategoryEntry.entering_name)
async def add_category_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(text_hint(db.get_user_language(message.from_user.id)))
        return
    name = message.text.strip()
    db.add_category(message.from_user.id, name)
    await state.clear()
    lang = db.get_user_language(message.from_user.id)
    text, keyboard = _categories_view(message.from_user.id)
    await message.answer(t("category_added", lang, name=hx(name), text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("cat_edit:"))
async def cat_edit_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split(":", 1)[1])
    if db.get_category_name(callback.from_user.id, cat_id) == PROTECTED_CATEGORY:
        await callback.answer(t("protected_rename", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(CategoryEntry.editing_name)
    await callback.message.edit_text(t("enter_category_rename", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.callback_query(F.data.startswith("cat_emoji:"))
async def cat_emoji_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(CategoryEntry.editing_emoji)
    await callback.message.edit_text(t("enter_category_emoji", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(CategoryEntry.editing_emoji)
async def cat_emoji_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    data = await state.get_data()
    emoji = message.text.strip()
    if not is_single_emoji(emoji):
        await message.answer(t("emoji_bad", lang))
        return
    db.set_category_emoji(message.from_user.id, data["edit_cat_id"], emoji)
    await state.clear()
    text, keyboard = _categories_view(message.from_user.id)
    await message.answer(t("emoji_updated", lang, text=text), reply_markup=keyboard)

@router.message(CategoryEntry.editing_name)
async def edit_category_name(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    data = await state.get_data()
    cat_id = data["edit_cat_id"]
    new_name = message.text.strip()
    ok = db.rename_category(message.from_user.id, cat_id, new_name)
    await state.clear()

    text, keyboard = _categories_view(message.from_user.id)
    key = "renamed_to" if ok else "category_exists"
    await message.answer(t(key, lang, name=hx(new_name), text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("cat_del:"))
async def cat_delete_confirm(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    cat_id = int(callback.data.split(":", 1)[1])
    count = db.count_transactions_for_category(callback.from_user.id, cat_id)
    note = (
        t("category_has_tx", lang, count=count, fallback=PROTECTED_CATEGORY)
        if count
        else t("category_no_tx", lang)
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("confirm_delete", lang), callback_data=f"cat_del_yes:{cat_id}"),
                InlineKeyboardButton(text=t("cancel_button", lang), callback_data="cat_del_no"),
            ]
        ]
    )
    await callback.message.edit_text(t("delete_category_q", lang, note=note), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("cat_del_yes:"))
async def cat_delete_apply(callback: CallbackQuery):
    cat_id = int(callback.data.split(":", 1)[1])
    ok = db.delete_category(callback.from_user.id, cat_id, fallback_name=PROTECTED_CATEGORY)
    lang = db.get_user_language(callback.from_user.id)
    text, keyboard = _categories_view(callback.from_user.id)
    key = "deleted" if ok else "delete_failed"
    await callback.message.edit_text(t(key, lang, text=text), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "cat_del_no")
async def cat_delete_cancel(callback: CallbackQuery):
    text, keyboard = _categories_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

def _payments_view(
    user_id: int,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    lang = db.get_user_language(user_id)
    pms = db.get_payment_methods(user_id)
    total_pages = max(1, (len(pms) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    visible = pms[page * CATALOG_PAGE_SIZE:(page + 1) * CATALOG_PAGE_SIZE]
    text = t("catalog_payments_hint", lang)
    if total_pages > 1:
        text += "\n" + t("page_of", lang, page=page + 1, total=total_pages)

    buttons = []
    for p in visible:
        buttons.append([
            InlineKeyboardButton(text=p["name"], callback_data="noop_pay"),
            InlineKeyboardButton(text="✏️", callback_data=f"pay_edit:{p['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"pay_del:{p['id']}"),
        ])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="◀️",
            callback_data=f"catalog_pay_page:{page - 1}",
        ))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(
            text="▶️",
            callback_data=f"catalog_pay_page:{page + 1}",
        ))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton(text=t("btn_add_payment", lang), callback_data="pay_add")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)

@router.message(Command("payments"))
async def cmd_payments(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _payments_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data.startswith("catalog_pay_page:"))
async def payments_page(callback: CallbackQuery):
    page = int(callback.data.split(":", 1)[1])
    text, keyboard = _payments_view(callback.from_user.id, page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "noop_pay")
async def noop_pay_callback(callback: CallbackQuery):
    await callback.answer()

@router.callback_query(F.data == "pay_add")
async def pay_add_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(t("enter_payment_name", db.get_user_language(callback.from_user.id)))
    await state.set_state(PaymentEntry.entering_name)
    await callback.answer()

@router.message(PaymentEntry.entering_name)
async def add_payment_name(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    name = message.text.strip()
    db.add_payment_method(message.from_user.id, name)
    await state.clear()
    text, keyboard = _payments_view(message.from_user.id)
    await message.answer(t("payment_added", lang, name=hx(name), text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("pay_edit:"))
async def pay_edit_start(callback: CallbackQuery, state: FSMContext):
    pm_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_pm_id=pm_id)
    await state.set_state(PaymentEntry.editing_name)
    await callback.message.edit_text(t("enter_payment_rename", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(PaymentEntry.editing_name)
async def edit_payment_name(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    data = await state.get_data()
    pm_id = data["edit_pm_id"]
    new_name = message.text.strip()
    ok = db.rename_payment_method(message.from_user.id, pm_id, new_name)
    await state.clear()

    text, keyboard = _payments_view(message.from_user.id)
    key = "renamed_to" if ok else "payment_exists"
    await message.answer(t(key, lang, name=hx(new_name), text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("pay_del:"))
async def pay_delete_confirm(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    pm_id = int(callback.data.split(":", 1)[1])
    count = db.count_transactions_for_payment_method(callback.from_user.id, pm_id)
    note = (
        t("payment_has_tx", lang, count=count)
        if count
        else t("payment_no_tx", lang)
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("confirm_delete", lang), callback_data=f"pay_del_yes:{pm_id}"),
                InlineKeyboardButton(text=t("cancel_button", lang), callback_data="pay_del_no"),
            ]
        ]
    )
    await callback.message.edit_text(t("delete_payment_q", lang, note=note), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("pay_del_yes:"))
async def pay_delete_apply(callback: CallbackQuery):
    pm_id = int(callback.data.split(":", 1)[1])
    ok = db.delete_payment_method(callback.from_user.id, pm_id)
    lang = db.get_user_language(callback.from_user.id)
    text, keyboard = _payments_view(callback.from_user.id)
    key = "deleted" if ok else "last_payment_block"
    await callback.message.edit_text(t(key, lang, text=text), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "pay_del_no")
async def pay_delete_cancel(callback: CallbackQuery):
    text, keyboard = _payments_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()
