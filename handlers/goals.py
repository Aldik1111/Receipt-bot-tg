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
from i18n import format_date, format_money, t
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




router = Router(name="goals")


def _progress_bar(pct: float) -> str:
    filled = round(min(pct, 100) / 10)
    return "🟩" * filled + "⬜" * (10 - filled)

def _format_deadline(value, lang: str = "ru") -> str:
    if not value:
        return t("deadline_none", lang)
    return format_date(value, lang)

def _goals_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    lang = db.get_user_language(user_id)
    goals = db.get_goals(user_id)
    lines = [t("goals_title", lang), ""]
    buttons = []
    if not goals:
        lines.append(t("no_goals", lang))
    for g in goals:
        pct = (g["current_amount"] / g["target_amount"] * 100) if g["target_amount"] > 0 else 0
        deadline = t("until_deadline", lang, date=_format_deadline(g["deadline"], lang)) if g["deadline"] else ""
        lines.append(
            f"<b>{hx(g['name'])}</b>: {format_money(g['current_amount'], lang)} / {format_money(g['target_amount'], lang)}{deadline}\n"
            f"{_progress_bar(pct)} {pct:.0f}%"
        )
        buttons.append([
            InlineKeyboardButton(text=f"✏️ {g['name']}"[:60], callback_data=f"goal_open:{g['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text=t("btn_new_goal", lang), callback_data="goal_new")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

def _goal_detail_view(user_id: int, goal_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    lang = db.get_user_language(user_id)
    g = db.get_goal_by_id(user_id, goal_id)
    if not g:
        return None
    pct = (g["current_amount"] / g["target_amount"] * 100) if g["target_amount"] > 0 else 0
    lines = [
        f"🎯 <b>{hx(g['name'])}</b>",
        f"{_progress_bar(pct)} {pct:.0f}%",
        t("goal_saved", lang, current=format_money(g["current_amount"], lang), target=format_money(g["target_amount"], lang)),
        t("goal_deadline", lang, value=_format_deadline(g["deadline"], lang)),
        "",
        t("goal_note", lang),
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t("btn_contribute", lang), callback_data=f"goal_add:{goal_id}"),
            InlineKeyboardButton(text=t("btn_withdraw", lang), callback_data=f"goal_wd:{goal_id}"),
        ],
        [
            InlineKeyboardButton(text=t("btn_name", lang), callback_data=f"goal_name:{goal_id}"),
            InlineKeyboardButton(text=t("btn_target", lang), callback_data=f"goal_target:{goal_id}"),
        ],
        [InlineKeyboardButton(text=t("btn_deadline", lang), callback_data=f"goal_deadline:{goal_id}")],
        [InlineKeyboardButton(text=t("btn_delete", lang), callback_data=f"goal_askdel:{goal_id}")],
        [InlineKeyboardButton(text=t("btn_to_list", lang), callback_data="goal_list")],
    ])
    return "\n".join(lines), keyboard

@router.message(Command("goals"))
async def cmd_goals(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _goals_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data == "goal_list")
async def goal_list_back(callback: CallbackQuery):
    text, keyboard = _goals_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("goal_open:"))
async def goal_open(callback: CallbackQuery):
    goal_id = int(callback.data.split(":", 1)[1])
    view = _goal_detail_view(callback.from_user.id, goal_id)
    if not view:
        await callback.answer(t("goal_not_found", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "goal_new")
async def goal_new_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GoalEntry.entering_name)
    await callback.message.edit_text(t("enter_goal_name", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(GoalEntry.entering_name)
async def goal_enter_name(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    await state.update_data(goal_name=message.text.strip())
    await state.set_state(GoalEntry.entering_target)
    await message.answer(t("enter_goal_target", lang))

@router.message(GoalEntry.entering_target)
async def goal_enter_target(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    target = parse_positive_amount(message.text)
    if target is None:
        await message.answer(t("amount_positive_goal", lang))
        return
    await state.update_data(goal_target=target)
    await state.set_state(GoalEntry.entering_deadline)
    await message.answer(t("enter_goal_deadline", lang))

@router.message(GoalEntry.entering_deadline)
async def goal_enter_deadline(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    raw = message.text.strip()
    deadline = None
    if raw != "-":
        try:
            deadline = datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
        except ValueError:
            await message.answer(t("bad_date_or_dash", lang))
            return
    data = await state.get_data()
    db.create_goal(message.from_user.id, data["goal_name"], data["goal_target"], deadline)
    await state.clear()
    text, keyboard = _goals_view(message.from_user.id)
    await message.answer(t("goal_created", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("goal_add:"))
async def goal_contribute_start(callback: CallbackQuery, state: FSMContext):
    goal_id = int(callback.data.split(":", 1)[1])
    await state.update_data(goal_id=goal_id)
    await state.set_state(GoalContribute.entering_amount)
    await callback.message.edit_text(t("enter_contribute", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(GoalContribute.entering_amount)
async def goal_contribute_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer(t("amount_positive_short", lang))
        return
    data = await state.get_data()
    tx_id = db.contribute_to_goal(message.from_user.id, data["goal_id"], amount)
    await state.clear()
    if tx_id is None:
        await message.answer(t("goal_not_found_long", lang))
        return
    goal = db.get_goal_by_id(message.from_user.id, data["goal_id"])
    view = _goal_detail_view(message.from_user.id, data["goal_id"])
    prefix = t("goal_reached", lang) if goal and goal["current_amount"] >= goal["target_amount"] else t("goal_contributed", lang)
    if view:
        text, keyboard = view
        await message.answer(prefix + text, reply_markup=keyboard)
    else:
        text, keyboard = _goals_view(message.from_user.id)
        await message.answer(prefix + text, reply_markup=keyboard)

@router.callback_query(F.data.startswith("goal_wd:"))
async def goal_withdraw_start(callback: CallbackQuery, state: FSMContext):
    lang = db.get_user_language(callback.from_user.id)
    goal_id = int(callback.data.split(":", 1)[1])
    goal = db.get_goal_by_id(callback.from_user.id, goal_id)
    if not goal:
        await callback.answer(t("goal_not_found", lang), show_alert=True)
        return
    if int(goal["current_amount"]) <= 0:
        await callback.answer(t("goal_empty", lang), show_alert=True)
        return
    await state.update_data(goal_id=goal_id)
    await state.set_state(GoalWithdraw.entering_amount)
    await callback.message.edit_text(
        t("enter_withdraw", lang, amount=format_money(goal["current_amount"], lang))
    )
    await callback.answer()

@router.message(GoalWithdraw.entering_amount)
async def goal_withdraw_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer(t("amount_positive_short", lang))
        return
    data = await state.get_data()
    tx_id, reason = db.withdraw_from_goal(message.from_user.id, data["goal_id"], amount)
    if reason == "insufficient":
        await message.answer(t("withdraw_too_much", lang))
        return
    await state.clear()
    if tx_id is None:
        await message.answer(t("goal_not_found_long", lang))
        return
    view = _goal_detail_view(message.from_user.id, data["goal_id"])
    if view:
        text, keyboard = view
        await message.answer(t("goal_withdrawn", lang) + text, reply_markup=keyboard)
    else:
        text, keyboard = _goals_view(message.from_user.id)
        await message.answer(t("goal_withdrawn", lang) + text, reply_markup=keyboard)

@router.callback_query(F.data.startswith("goal_name:"))
async def goal_edit_name_start(callback: CallbackQuery, state: FSMContext):
    goal_id = int(callback.data.split(":", 1)[1])
    await state.update_data(goal_id=goal_id)
    await state.set_state(GoalEdit.entering_name)
    await callback.message.edit_text(t("enter_goal_rename", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(GoalEdit.entering_name)
async def goal_edit_name_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text or not message.text.strip():
        await message.answer(text_hint(lang))
        return
    data = await state.get_data()
    if not db.update_goal(message.from_user.id, data["goal_id"], name=message.text.strip()):
        await state.clear()
        await message.answer(t("goal_not_found_long", lang))
        return
    await state.clear()
    view = _goal_detail_view(message.from_user.id, data["goal_id"])
    text, keyboard = view or _goals_view(message.from_user.id)
    await message.answer(t("name_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("goal_target:"))
async def goal_edit_target_start(callback: CallbackQuery, state: FSMContext):
    goal_id = int(callback.data.split(":", 1)[1])
    await state.update_data(goal_id=goal_id)
    await state.set_state(GoalEdit.entering_target)
    await callback.message.edit_text(t("enter_goal_new_target", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(GoalEdit.entering_target)
async def goal_edit_target_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    target = parse_positive_amount(message.text)
    if target is None:
        await message.answer(t("amount_positive_short", lang))
        return
    data = await state.get_data()
    if not db.update_goal(message.from_user.id, data["goal_id"], target_amount=target):
        await state.clear()
        await message.answer(t("goal_not_found_long", lang))
        return
    await state.clear()
    view = _goal_detail_view(message.from_user.id, data["goal_id"])
    text, keyboard = view or _goals_view(message.from_user.id)
    await message.answer(t("target_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("goal_deadline:"))
async def goal_edit_deadline_start(callback: CallbackQuery, state: FSMContext):
    goal_id = int(callback.data.split(":", 1)[1])
    await state.update_data(goal_id=goal_id)
    await state.set_state(GoalEdit.entering_deadline)
    await callback.message.edit_text(t("enter_goal_new_deadline", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(GoalEdit.entering_deadline)
async def goal_edit_deadline_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    raw = message.text.strip()
    data = await state.get_data()
    if raw == "-":
        ok = db.update_goal(message.from_user.id, data["goal_id"], clear_deadline=True)
    else:
        try:
            deadline = datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
        except ValueError:
            await message.answer(t("bad_date_or_dash", lang))
            return
        ok = db.update_goal(message.from_user.id, data["goal_id"], deadline=deadline)
    await state.clear()
    if not ok:
        await message.answer(t("goal_not_found_long", lang))
        return
    view = _goal_detail_view(message.from_user.id, data["goal_id"])
    text, keyboard = view or _goals_view(message.from_user.id)
    await message.answer(t("deadline_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("goal_askdel:"))
async def goal_delete_confirm(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    goal_id = int(callback.data.split(":", 1)[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("confirm_delete", lang), callback_data=f"goal_del_yes:{goal_id}"),
        InlineKeyboardButton(text=t("cancel_button", lang), callback_data=f"goal_open:{goal_id}"),
    ]])
    await callback.message.edit_text(
        t("delete_goal_confirm", lang),
        reply_markup=keyboard,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("goal_del_yes:"))
async def goal_delete_apply(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    goal_id = int(callback.data.split(":", 1)[1])
    db.delete_goal(callback.from_user.id, goal_id)
    text, keyboard = _goals_view(callback.from_user.id)
    await callback.message.edit_text(t("deleted", lang, text=text), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "goal_del_no")
async def goal_delete_cancel(callback: CallbackQuery):
    text, keyboard = _goals_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()
