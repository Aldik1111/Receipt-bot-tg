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
from telegram_ui import apply_user_ui
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




router = Router(name="settings")


@router.message(Command("language"))
async def cmd_language(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"lang:{code}")]
        for code, name in LANGUAGES.items()
    ])
    await message.answer(t("language_prompt", db.get_user_language(message.from_user.id)), reply_markup=keyboard)

@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery, bot: Bot):
    lang = callback.data.split(":", 1)[1]
    if lang not in LANGUAGES:
        await callback.answer()
        return
    db.ensure_user(callback.from_user.id, callback.from_user.username)
    db.set_user_language(callback.from_user.id, lang)
    await apply_user_ui(bot, callback.from_user.id, callback.message.chat.id)
    if db.is_onboarded(callback.from_user.id):
        text, keyboard = _settings_view(callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        from handlers.base import _onboarding_keyboard
        await callback.message.edit_text(
            t("onboarding_text", lang),
            reply_markup=_onboarding_keyboard(lang),
        )
    await callback.answer(t("language_set", lang))

@router.message(Command("digest"))
async def cmd_digest(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    lang = db.get_user_language(message.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(key, lang), callback_data=f"digest_freq:{freq}")]
        for freq, key in DIGEST_FREQ_LABELS.items()
    ])
    await message.answer(t("digest_prompt", lang), reply_markup=keyboard)

@router.callback_query(F.data.startswith("digest_freq:"))
async def set_digest_freq(callback: CallbackQuery):
    freq = callback.data.split(":", 1)[1]
    db.ensure_user(callback.from_user.id, callback.from_user.username)
    lang = db.get_user_language(callback.from_user.id)
    db.set_digest_frequency(callback.from_user.id, freq)
    await callback.message.edit_text(t("digest_set", lang, freq=t(DIGEST_FREQ_LABELS[freq], lang)))
    await callback.answer()

def _settings_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    s = db.get_settings(user_id)
    lang = s["language"]
    lang_name = LANGUAGES.get(lang, lang)
    digest_label = t(DIGEST_FREQ_LABELS.get(s["digest_frequency"], "digest_off"), lang)
    idle_on = bool(s["idle_reminder_enabled"])
    backup_on = bool(s["backup_enabled"])
    bank_on = bool(s["bank_import_enabled"])

    tz_name = s["timezone"] if s["timezone"] else "Asia/Almaty"
    tz_label = TIMEZONE_CHOICES.get(tz_name, tz_name)
    on_off = lambda enabled: t("state_on" if enabled else "state_off", lang)
    action = lambda enabled: t("action_disable" if enabled else "action_enable", lang)

    text = "\n".join((
        t("settings_title", lang),
        "",
        t("settings_language", lang, name=lang_name),
        t("settings_timezone", lang, name=tz_label),
        t("settings_digest", lang, name=digest_label),
        t("settings_idle", lang, state=on_off(idle_on)),
        t("settings_backup", lang, state=on_off(backup_on)),
        t("settings_bank", lang, state=on_off(bank_on)),
    ))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_change_language", lang), callback_data="settings_lang")],
        [InlineKeyboardButton(text=t("btn_timezone", lang), callback_data="settings_tz")],
        [InlineKeyboardButton(text=t("btn_digest_freq", lang), callback_data="settings_digest")],
        [InlineKeyboardButton(
            text=t("btn_idle_toggle", lang, action=action(idle_on)),
            callback_data="settings_toggle_idle",
        )],
        [InlineKeyboardButton(
            text=t("btn_backup_toggle", lang, action=action(backup_on)),
            callback_data="settings_toggle_backup",
        )],
        [InlineKeyboardButton(
            text=t("btn_bank_toggle", lang, action=action(bank_on)),
            callback_data="settings_toggle_bank",
        )],
        [InlineKeyboardButton(text=t("btn_backup_now", lang), callback_data="settings_backup_now")],
        [InlineKeyboardButton(text=t("btn_restore", lang), callback_data="settings_restore_start")],
        [InlineKeyboardButton(text=t("btn_danger", lang), callback_data="danger_start")],
    ])
    return text, keyboard

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _settings_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data == "settings_lang")
async def settings_lang(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"lang:{code}")]
        for code, name in LANGUAGES.items()
    ])
    await callback.message.edit_text(t("language_prompt", db.get_user_language(callback.from_user.id)), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "settings_tz")
async def settings_tz(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"tz:{code}")]
        for code, label in TIMEZONE_CHOICES.items()
    ] + [[InlineKeyboardButton(text=t("back_button", db.get_user_language(callback.from_user.id)), callback_data="settings_home")]])
    await callback.message.edit_text(t("tz_prompt", db.get_user_language(callback.from_user.id)), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "settings_home")
async def settings_home(callback: CallbackQuery):
    text, keyboard = _settings_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("tz:"))
async def set_timezone(callback: CallbackQuery):
    tz_name = callback.data.split(":", 1)[1]
    db.ensure_user(callback.from_user.id, callback.from_user.username)
    if not db.set_user_timezone(callback.from_user.id, tz_name):
        await callback.answer(t("tz_invalid", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    text, keyboard = _settings_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(t("tz_updated", db.get_user_language(callback.from_user.id)))

@router.callback_query(F.data == "settings_digest")
async def settings_digest(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(key, lang), callback_data=f"digest_freq:{freq}")]
        for freq, key in DIGEST_FREQ_LABELS.items()
    ])
    await callback.message.edit_text(t("digest_prompt", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "settings_toggle_idle")
async def settings_toggle_idle(callback: CallbackQuery):
    s = db.get_settings(callback.from_user.id)
    db.set_idle_reminder_enabled(callback.from_user.id, not s["idle_reminder_enabled"])
    text, keyboard = _settings_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "settings_toggle_backup")
async def settings_toggle_backup(callback: CallbackQuery):
    s = db.get_settings(callback.from_user.id)
    new_state = not s["backup_enabled"]
    db.set_backup_enabled(callback.from_user.id, new_state)
    text, keyboard = _settings_view(callback.from_user.id)
    note = t("backup_note_on", db.get_user_language(callback.from_user.id)) if new_state else ""
    await callback.message.edit_text(text + note, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "settings_toggle_bank")
async def settings_toggle_bank(callback: CallbackQuery):
    s = db.get_settings(callback.from_user.id)
    new_state = not s["bank_import_enabled"]
    db.set_bank_import_enabled(callback.from_user.id, new_state)
    text, keyboard = _settings_view(callback.from_user.id)
    note = t("bank_note_on", db.get_user_language(callback.from_user.id)) if new_state else ""
    await callback.message.edit_text(text + note, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "settings_backup_now")
async def settings_backup_now(callback: CallbackQuery):
    buf = await asyncio.to_thread(backup.build_backup, callback.from_user.id)
    payload = buf.read()
    if len(payload) > 45 * 1024 * 1024:
        await callback.answer(t("backup_too_big", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    today = db.user_today(callback.from_user.id).isoformat()
    try:
        await callback.message.answer_document(
            BufferedInputFile(payload, filename=f"backup_{today}.json")
        )
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
        await callback.message.answer_document(
            BufferedInputFile(payload, filename=f"backup_{today}.json")
        )
    except Exception:
        logger.exception("Не удалось отправить бэкап")
        await callback.answer(t("backup_send_fail", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    db.mark_backup_sent(callback.from_user.id, today)
    await callback.answer(t("backup_sent", db.get_user_language(callback.from_user.id)))

@router.callback_query(F.data == "danger_start")
async def danger_start(callback: CallbackQuery):
    user_id = callback.from_user.id
    count = db.count_user_data(user_id)
    lang = db.get_user_language(user_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("danger_confirm", lang), callback_data="danger_confirm1"),
        InlineKeyboardButton(text=t("cancel_button", lang), callback_data="danger_cancel"),
    ]])
    await callback.message.edit_text(
        t("danger_intro", lang, count=count),
        reply_markup=keyboard,
    )
    await callback.answer()

@router.callback_query(F.data == "danger_confirm1")
async def danger_confirm1(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DangerZone.entering_phrase)
    lang = db.get_user_language(callback.from_user.id)
    await callback.message.edit_text(
        t("danger_phrase_prompt", lang, phrase=t("danger_phrase", lang))
    )
    await callback.answer()

@router.message(DangerZone.entering_phrase)
async def danger_confirm2(message: Message, state: FSMContext):
    await state.clear()
    lang = db.get_user_language(message.from_user.id)
    if not message.text or message.text.strip() != t("danger_phrase", lang):
        await message.answer(t("danger_mismatch", lang))
        return
    db.delete_all_user_data(message.from_user.id)
    await message.answer(t("danger_done", lang))

@router.callback_query(F.data == "danger_cancel")
async def danger_cancel(callback: CallbackQuery):
    text, keyboard = _settings_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(t("cancelled", db.get_user_language(callback.from_user.id)))
