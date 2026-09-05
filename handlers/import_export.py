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


from handlers.stats import _period_bounds


router = Router(name="import_export")


@router.message(Command("export"))
async def cmd_export(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    lang = db.get_user_language(message.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("period_month", lang), callback_data="exp_period:month"),
         InlineKeyboardButton(text=t("period_3months", lang), callback_data="exp_period:3months")],
        [InlineKeyboardButton(text=t("period_year", lang), callback_data="exp_period:year"),
         InlineKeyboardButton(text=t("period_all", lang), callback_data="exp_period:all")],
    ])
    await message.answer(t("export_prompt", lang), reply_markup=keyboard)

@router.callback_query(F.data.startswith("exp_period:"))
async def export_choose_format(callback: CallbackQuery, state: FSMContext):
    period = callback.data.split(":", 1)[1]
    await state.update_data(export_period=period)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="CSV", callback_data="exp_format:csv"),
        InlineKeyboardButton(text="Excel", callback_data="exp_format:xlsx"),
    ]])
    await callback.message.edit_text(
        t("export_format_prompt", db.get_user_language(callback.from_user.id)),
        reply_markup=keyboard,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("exp_format:"))
async def export_generate(callback: CallbackQuery, state: FSMContext):
    fmt = callback.data.split(":", 1)[1]
    data = await state.get_data()
    period = data.get("export_period", "month")
    user_id = callback.from_user.id

    if period == "all":
        date_from, date_to = "2000-01-01", db.user_today(user_id).isoformat()
    else:
        date_from, date_to, _ = _period_bounds(period, db.user_today(user_id))

    rows = [dict(r) for r in db.get_transactions(user_id, date_from, date_to)]
    await state.clear()

    lang = db.get_user_language(user_id)
    if not rows:
        await callback.message.edit_text(t("export_empty", lang))
        await callback.answer()
        return

    if len(rows) >= 1000:
        await callback.message.edit_text(t("export_large", lang, count=len(rows)))

    if fmt == "csv":
        buf = await asyncio.to_thread(export.export_csv, rows)
        filename = f"operations_{date_from}_{date_to}.csv"
    else:
        # openpyxl на выгрузке за год - это секунды CPU, держать на них loop нельзя
        buf = await asyncio.to_thread(export.export_xlsx, rows)
        filename = f"operations_{date_from}_{date_to}.xlsx"

    await callback.message.edit_text(t("export_ready", lang, count=len(rows)))
    await callback.message.answer_document(BufferedInputFile(buf.read(), filename=filename))
    await callback.answer()

@router.callback_query(F.data == "settings_restore_start")
async def settings_restore_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RestoreEntry.awaiting_file)
    await callback.message.answer(
        t("restore_prompt", db.get_user_language(callback.from_user.id))
    )
    await callback.answer()

@router.message(RestoreEntry.awaiting_file, F.document)
async def restore_receive_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    lang = db.get_user_language(message.from_user.id)
    if doc.file_size and doc.file_size > MAX_RESTORE_BYTES:
        await message.answer(t("file_too_big_20", lang))
        return
    file = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    raw = buf.getvalue()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await message.answer(t("restore_not_json", lang))
        return
    if not isinstance(data, dict):
        await message.answer(t("restore_bad_format", lang))
        return

    draft_id = f"{message.from_user.id}_{uuid.uuid4().hex[:10]}"
    db.save_state(RESTORE_SCOPE, draft_id, data, user_id=message.from_user.id)
    await state.clear()

    counts = t(
        "restore_preview",
        lang,
        receipts=len(data.get("receipts") or []),
        transactions=len(data.get("transactions") or []),
        recurring=len(data.get("recurring") or []),
        goals=len(data.get("goals") or []),
        categories=len(data.get("categories") or []),
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("btn_replace_data", lang), callback_data=f"restore_go:{draft_id}"),
        InlineKeyboardButton(text=t("btn_no", lang), callback_data=f"restore_no:{draft_id}"),
    ]])
    await message.answer(t("restore_confirm_q", lang, counts=counts), reply_markup=keyboard)

@router.message(RestoreEntry.awaiting_file)
async def restore_awaiting_wrong_content(message: Message):
    await message.answer(t("restore_need_file", db.get_user_language(message.from_user.id)))

@router.callback_query(F.data.startswith("restore_go:"))
async def restore_confirm(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    lang = db.get_user_language(user_id)
    if not draft_id.startswith(f"{user_id}_"):
        await callback.answer(t("not_your_draft", lang), show_alert=True)
        return
    data = db.load_state(RESTORE_SCOPE, draft_id)
    if data is None:
        await callback.answer(t("draft_stale_file", lang), show_alert=True)
        return
    try:
        result = await asyncio.to_thread(db.restore_user_backup, user_id, data)
    except Exception:
        logger.exception("Не удалось восстановить бэкап")
        await callback.message.edit_text(t("restore_failed", lang))
        await callback.answer()
        return
    db.delete_state(RESTORE_SCOPE, draft_id)
    await callback.message.edit_text(
        t(
            "restore_done",
            lang,
            transactions=result.get("transactions", 0),
            receipts=result.get("receipts", 0),
            recurring=result.get("recurring", 0),
            goals=result.get("goals", 0),
        )
    )
    await callback.answer()

@router.callback_query(F.data.startswith("restore_no:"))
async def restore_cancel(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    lang = db.get_user_language(callback.from_user.id)
    if not draft_id.startswith(f"{callback.from_user.id}_"):
        await callback.answer(t("not_your_draft", lang), show_alert=True)
        return
    db.delete_state(RESTORE_SCOPE, draft_id)
    await callback.message.edit_text(t("restore_cancelled", lang))
    await callback.answer()

@router.message(StateFilter(None), F.document, F.document.file_name.regexp(r"\.(csv|xlsx)$"))
async def handle_import_file(message: Message, bot: Bot):
    user_id = message.from_user.id
    db.ensure_user(user_id, message.from_user.username)
    lang = db.get_user_language(user_id)

    file_size = message.document.file_size or 0
    if file_size > MAX_IMPORT_BYTES:
        await message.answer(t("import_too_big", lang))
        return

    file = await bot.get_file(message.document.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)

    try:
        rows = await asyncio.to_thread(
            export.parse_import_file, buf.getvalue(), message.document.file_name
        )
    except ValueError:
        await message.answer(t("import_too_many", lang))
        return
    except Exception:
        logger.exception("Ошибка разбора импортируемого файла")
        await message.answer(t("import_unreadable", lang))
        return

    if not rows:
        await message.answer(t("import_no_amount", lang))
        return

    import_id = f"{user_id}_{message.message_id}"
    db.save_state(IMPORT_SCOPE, import_id, rows, user_id=user_id)
    expense_total = sum(r["amount"] for r in rows if r["type"] == "expense")
    income_total = sum(r["amount"] for r in rows if r["type"] == "income")
    dash = t("dash", lang)
    preview_lines = [
        f"• {hx(r['date']) or dash} — {'+' if r['type'] == 'income' else ('↔' if r['type'] == 'transfer' else '−')}{format_money(r['amount'], lang)}"
        f" — {hx(r['description']) or t('no_description', lang)}"
        for r in rows[:10]
    ]
    more = t("import_more", lang, count=len(rows) - 10) if len(rows) > 10 else ""
    header = t("import_header", lang, count=len(rows), expense=format_money(expense_total, lang))
    if income_total:
        header += t("import_income_line", lang, amount=format_money(income_total, lang))
    dup_n = db.count_import_duplicates(
        user_id, rows, db.user_today(user_id).isoformat()
    )
    warning = t("import_dups", lang, count=dup_n) if dup_n else ""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("btn_import_all", lang, count=len(rows)), callback_data=f"imp_ok:{message.message_id}"),
        InlineKeyboardButton(text=t("btn_no", lang), callback_data=f"imp_no:{message.message_id}"),
    ]])
    await message.answer(
        header + "\n\n" + "\n".join(preview_lines) + more + warning + t("import_ask", lang),
        reply_markup=keyboard,
    )

@router.callback_query(F.data.startswith("imp_ok:"))
async def import_confirm(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    message_id = callback.data.split(":", 1)[1]
    import_id = f"{user_id}_{message_id}"
    rows = db.load_state(IMPORT_SCOPE, import_id, default=[])
    if not rows:
        await callback.message.edit_text(t("draft_stale", db.get_user_language(user_id)))
        return
    # Категорию из файла уважаем, если такая категория у пользователя есть -
    # иначе своя же выгрузка теряла бы категории при обратном импорте.
    # Угадываем только там, где колонки не было или название незнакомое.
    known_categories = {c["name"] for c in db.get_categories(user_id)}
    need_guess = [
        index for index, row in enumerate(rows)
        if (row.get("category") or "") not in known_categories
    ]
    if need_guess:
        names = [rows[index]["description"] or "" for index in need_guess]
        cat_names = await asyncio.to_thread(categorize_many, user_id, names)
        for index, cat_name in zip(need_guess, cat_names):
            rows[index]["category"] = cat_name
    db.save_state(IMPORT_SCOPE, import_id, rows, user_id=user_id)
    pm_id = db.get_default_payment_method_id(user_id)
    count = await asyncio.to_thread(
        db.commit_import_draft,
        user_id,
        import_id,
        pm_id,
        db.user_today(user_id).isoformat(),
    )
    if count is None:
        await callback.message.edit_text(t("draft_stale", db.get_user_language(user_id)))
        return
    await callback.message.edit_text(t("imported", db.get_user_language(user_id), count=count))

@router.callback_query(F.data.startswith("imp_no:"))
async def import_cancel(callback: CallbackQuery):
    user_id = callback.from_user.id
    message_id = callback.data.split(":", 1)[1]
    db.delete_state(IMPORT_SCOPE, f"{user_id}_{message_id}", user_id=user_id)
    await callback.message.edit_text(t("import_cancelled", db.get_user_language(user_id)))
    await callback.answer()
