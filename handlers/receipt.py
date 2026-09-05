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


from handlers.stats import _budget_warnings_text


router = Router(name="receipt")

def SOURCE_LABELS(lang: str = "ru") -> dict[str, str]:
    return {
        "gemini": t("receipt_source_gemini", lang),
        "bank_notification": t("receipt_source_bank", lang),
    }

@router.message(StateFilter(None), F.photo)
async def handle_receipt_photo(message: Message, bot: Bot):
    photo = message.photo[-1]  # берём максимальное качество из доступных превью
    await _process_receipt(
        message,
        bot,
        file_id=photo.file_id,
        unique_id=photo.file_unique_id,
        declared_size=photo.file_size,
    )

@router.message(StateFilter(None), F.document, F.document.mime_type.startswith("image/"))
async def handle_receipt_document(message: Message, bot: Bot):
    # Telegram сильно сжимает фото, отправленные как "фото" - это одна из
    # главных причин плохого распознавания. Если прислать снимок как файл
    # (документ), сжатия нет и OCR работает заметно точнее.
    doc = message.document
    await _process_receipt(
        message,
        bot,
        file_id=doc.file_id,
        unique_id=doc.file_unique_id,
        declared_size=doc.file_size,
    )

def _receipt_error_text(code: str, lang: str = "ru") -> str:
    key = {
        "too_large": "receipt_err_too_large",
        "unsupported": "receipt_err_unsupported",
        "corrupt": "receipt_err_corrupt",
        "rate_limit": "receipt_err_rate_limit",
        "network": "receipt_err_network",
        "bad_json": "receipt_err_bad_json",
        "no_key": "receipt_err_no_key",
        "unavailable": "receipt_err_unavailable",
        "quota": "receipt_err_quota",
        "empty": "receipt_err_empty",
    }.get(code, "receipt_err_unavailable")
    return t(key, lang, limit=GEMINI_DAILY_LIMIT)

def _default_payment_fields(user_id: int) -> tuple[int | None, str | None]:
    pm_id = db.get_default_payment_method_id(user_id)
    if pm_id is None:
        return None, None
    for method in db.get_payment_methods(user_id):
        if method["id"] == pm_id:
            return pm_id, method["name"]
    return pm_id, None

def _recognize_receipt_image(user_id: int, local_path: str, day: str, unique_id: str | None):
    prepared = prepare_receipt_image(local_path)
    digest = sha256_file(prepared)
    fingerprints = []
    if unique_id:
        fingerprints.append(photo_telegram_fingerprint(unique_id))
    fingerprints.append(photo_sha256_fingerprint(digest))
    seen = db.find_fingerprint(user_id, "photo", fingerprints)
    allowed, used = db.try_consume_gemini_quota(user_id, day)
    if not allowed:
        logger.info("gemini_quota denied user=%s day=%s used=%s", user_id, day, used)
        return {
            "error": "quota",
            "fingerprints": fingerprints,
            "seen": seen,
            "used": used,
            "prepared": prepared,
        }
    parsed, err = extract_receipt(prepared, user_id)
    if err in {"rate_limit", "network", "bad_json", "unavailable", "no_key"}:
        used = db.refund_gemini_quota(user_id, day)
    total = db.get_gemini_usage_total(day)
    logger.info(
        "gemini_quota user=%s day=%s used=%s limit=%s total_day=%s err=%s",
        user_id,
        day,
        used,
        GEMINI_DAILY_LIMIT,
        total,
        err,
    )
    return {
        "parsed": parsed,
        "error": err,
        "fingerprints": fingerprints,
        "seen": seen,
        "used": used,
        "prepared": prepared,
    }

async def _process_receipt(
    message: Message,
    bot: Bot,
    file_id: str,
    unique_id: str,
    declared_size: int | None,
    skip_duplicate_prompt: bool = False,
    user_id: int | None = None,
):
    user_id = user_id or message.from_user.id
    db.ensure_user(user_id, getattr(message.from_user, "username", None))
    lang = db.get_user_language(user_id)

    if declared_size and declared_size > MAX_RECEIPT_BYTES:
        await message.answer(_receipt_error_text("too_large", lang))
        return

    if unique_id and not skip_duplicate_prompt:
        seen = db.find_fingerprint(
            user_id, "photo", [photo_telegram_fingerprint(unique_id)]
        )
        if seen:
            dup_id = uuid.uuid4().hex[:16]
            db.save_state(
                DUP_SCOPE,
                dup_id,
                {
                    "user_id": user_id,
                    "file_id": file_id,
                    "unique_id": unique_id,
                    "declared_size": declared_size,
                },
                user_id=user_id,
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=t("btn_save_again", lang), callback_data=f"dup_yes:{dup_id}"),
                InlineKeyboardButton(text=t("btn_no_need", lang), callback_data=f"dup_no:{dup_id}"),
            ]])
            await message.answer(
                t("receipt_dup_prompt", lang, seen=hx(seen)),
                reply_markup=keyboard,
            )
            return

    status = await message.answer(t("receipt_recognizing", lang))

    file = await bot.get_file(file_id)
    actual_size = getattr(file, "file_size", None) or declared_size or 0
    if actual_size > MAX_RECEIPT_BYTES:
        await status.edit_text(_receipt_error_text("too_large", lang))
        return

    suffix = os.path.splitext(file.file_path or "")[1].lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    local_path = os.path.join(
        tempfile.gettempdir(),
        f"receipt_{user_id}_{uuid.uuid4().hex}{suffix}",
    )
    await bot.download_file(file.file_path, destination=local_path)
    if os.path.getsize(local_path) > MAX_RECEIPT_BYTES:
        os.remove(local_path)
        await status.edit_text(_receipt_error_text("too_large", lang))
        return

    cleanup = [local_path]
    result = None
    try:
        # Gemini ходит по сети синхронным requests; ресайз Pillow тоже тяжёлый.
        result = await asyncio.to_thread(
            _recognize_receipt_image,
            user_id,
            local_path,
            db.user_today(user_id).isoformat(),
            unique_id,
        )
        if result.get("prepared"):
            cleanup.append(result["prepared"])
    except ReceiptImageError as exc:
        await status.edit_text(_receipt_error_text(exc.code, lang))
        return
    except Exception:
        logger.exception("Receipt extraction error")
        await status.edit_text(_receipt_error_text("unavailable", lang))
        return
    finally:
        for path in cleanup:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    if not result:
        return
    if result.get("error"):
        await status.edit_text(_receipt_error_text(result["error"], lang))
        return

    parsed = result.get("parsed")
    if not parsed or not parsed.get("items"):
        await status.edit_text(_receipt_error_text("empty", lang))
        return

    pm_id, pm_name = _default_payment_fields(user_id)
    parsed["payment_method_id"] = pm_id
    parsed["payment_name"] = pm_name
    parsed["fingerprints"] = result.get("fingerprints") or []
    if result.get("seen"):
        parsed["duplicate_warning"] = result["seen"]

    draft_id = uuid.uuid4().hex[:16]
    db.save_state(RECEIPT_SCOPE, draft_id, parsed, user_id=user_id)

    text, keyboard = _receipt_draft_view(parsed, draft_id, lang)
    await status.edit_text(text, reply_markup=keyboard)

def _format_receipt_preview(parsed: dict, lang: str = "ru") -> str:
    lines = [t("receipt_title", lang)]
    source_label = SOURCE_LABELS(lang).get(parsed.get("source"))
    if source_label:
        lines.append(t("receipt_source", lang, name=source_label))
    if parsed.get("duplicate_warning"):
        lines.append(t("receipt_dup_inline", lang, seen=hx(parsed["duplicate_warning"])))
    lines.append("")
    lines.append(t("receipt_store", lang, name=hx(parsed["store"]) or t("dash", lang)))
    lines.append(t(
        "receipt_datetime",
        lang,
        date=format_date(parsed.get("date"), lang) if parsed.get("date") else t("dash", lang),
        time=hx(parsed.get("time")) or t("dash", lang),
    ))
    if parsed.get("source") == "bank_notification":
        lines.append(t("receipt_type", lang, name=type_label(parsed.get("tx_type") or "expense", lang)))
        if parsed.get("ambiguous_type"):
            lines.append(t("receipt_choose_type", lang))
    lines.append(t("receipt_pay", lang, name=hx(parsed.get("payment_name")) or t("dash", lang)) + "\n")
    total = 0
    for item in parsed["items"]:
        category = hx(item["category"]) if item.get("category") else t("uncategorized", lang)
        lines.append(f"• {hx(item['name'])} — {format_money(item['price'], lang)}  [{category}]")
        total += int(item["price"])
    lines.append("\n" + t("receipt_items_total", lang, amount=format_money(total, lang)))
    if parsed["total"] is not None:
        receipt_total = int(parsed["total"])
        lines.append(t("receipt_total", lang, amount=format_money(receipt_total, lang)))
        if abs(total - receipt_total) > db.RECEIPT_TOTAL_MISMATCH_TIYN:
            lines.append(
                t("receipt_mismatch", lang, amount=format_money(abs(total - receipt_total), lang))
            )
    lines.append(t("receipt_check_items", lang))
    return "\n".join(lines)

def _receipt_draft_view(parsed: dict, draft_id: str, lang: str = "ru") -> tuple[str, InlineKeyboardMarkup]:
    text = _format_receipt_preview(parsed, lang)
    if len(text) > 3500:
        text = text[:3500].rstrip() + t("receipt_truncated", lang)

    buttons = []
    for index, item in enumerate(parsed.get("items") or []):
        label = str(item.get("name") or t("receipt_item_fallback", lang, n=index + 1))
        buttons.append([
            InlineKeyboardButton(
                text=f"✏️/🗑 {label}"[:58],
                callback_data=f"recv_item:{draft_id}:{index}",
            )
        ])

    item_total = sum(int(item["price"]) for item in parsed.get("items") or [])
    receipt_total = parsed.get("total")
    mismatch = (
        receipt_total is not None
        and abs(item_total - int(receipt_total)) > db.RECEIPT_TOTAL_MISMATCH_TIYN
    )
    pay_row = [InlineKeyboardButton(
        text=(
            f"💳 {parsed['payment_name']}"[:58]
            if parsed.get("payment_name")
            else t("btn_payment", lang)
        ),
        callback_data=f"recv_paypick:{draft_id}",
    )]
    if parsed.get("source") == "bank_notification":
        buttons.append([
            InlineKeyboardButton(
                text=t("type_expense", lang),
                callback_data=f"recv_type:{draft_id}:expense",
            ),
            InlineKeyboardButton(
                text=t("type_income", lang),
                callback_data=f"recv_type:{draft_id}:income",
            ),
        ])
    if mismatch:
        buttons.append([
            InlineKeyboardButton(
                text=t("btn_trust_items", lang),
                callback_data=f"recv_total_items:{draft_id}",
            ),
            InlineKeyboardButton(
                text=t("btn_trust_total", lang),
                callback_data=f"recv_total_receipt:{draft_id}",
            ),
        ])
        buttons.append(pay_row)
        buttons.append([
            InlineKeyboardButton(text=t("btn_no", lang), callback_data=f"recv_cancel:{draft_id}")
        ])
    else:
        buttons.append(pay_row)
        buttons.append([
            InlineKeyboardButton(text=t("btn_save_all", lang), callback_data=f"recv_save:{draft_id}"),
            InlineKeyboardButton(text=t("btn_no", lang), callback_data=f"recv_cancel:{draft_id}"),
        ])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)

async def _refresh_receipt_draft(callback: CallbackQuery, draft_id: str, parsed: dict) -> None:
    lang = db.get_user_language(callback.from_user.id)
    text, keyboard = _receipt_draft_view(parsed, draft_id, lang)
    await callback.message.edit_text(text, reply_markup=keyboard)

@router.callback_query(F.data.startswith("recv_item:"))
async def receipt_draft_item_actions(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    _, draft_id, index_raw = callback.data.split(":")
    index = int(index_raw)
    parsed = db.get_receipt_draft(callback.from_user.id, draft_id)
    if parsed is None or index < 0 or index >= len(parsed.get("items") or []):
        await callback.answer(t("draft_stale_item", lang), show_alert=True)
        return
    item = parsed["items"][index]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=t("btn_fix", lang), callback_data=f"recv_edit:{draft_id}:{index}"
            ),
            InlineKeyboardButton(
                text=t("btn_delete", lang), callback_data=f"recv_drop:{draft_id}:{index}"
            ),
        ],
        [InlineKeyboardButton(text=t("btn_to_receipt", lang), callback_data=f"recv_back:{draft_id}")],
    ])
    await callback.message.edit_text(
        f"<b>{hx(item.get('name'))}</b> — {format_money(item.get('price'), lang)}",
        reply_markup=keyboard,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("recv_back:"))
async def receipt_draft_back(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    parsed = db.get_receipt_draft(callback.from_user.id, draft_id)
    if parsed is None:
        await callback.answer(t("draft_stale", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    await _refresh_receipt_draft(callback, draft_id, parsed)
    await callback.answer()

@router.callback_query(F.data.startswith("recv_edit:"))
async def receipt_draft_edit_start(callback: CallbackQuery, state: FSMContext):
    lang = db.get_user_language(callback.from_user.id)
    _, draft_id, index_raw = callback.data.split(":")
    index = int(index_raw)
    parsed = db.get_receipt_draft(callback.from_user.id, draft_id)
    if parsed is None or index < 0 or index >= len(parsed.get("items") or []):
        await callback.answer(t("draft_stale_item", lang), show_alert=True)
        return
    item = parsed["items"][index]
    await state.update_data(receipt_draft_id=draft_id, receipt_item_index=index)
    await state.set_state(ReceiptDraftEdit.entering_item)
    await callback.message.edit_text(
        t(
            "receipt_edit_prompt",
            lang,
            sample=f"{hx(item.get('name'))} | {tiyn_to_tenge(int(item.get('price') or 0))}",
        )
    )
    await callback.answer()

@router.message(ReceiptDraftEdit.entering_item)
async def receipt_draft_edit_apply(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not message.text or "|" not in message.text:
        await message.answer(t("receipt_edit_format", lang))
        return
    name, price_text = (part.strip() for part in message.text.rsplit("|", 1))
    price = parse_positive_amount(price_text)
    if not name or price is None:
        await message.answer(t("receipt_edit_need", lang))
        return
    data = await state.get_data()
    try:
        parsed = db.update_receipt_draft_item(
            message.from_user.id,
            data["receipt_draft_id"],
            data["receipt_item_index"],
            name,
            price,
        )
    except (IndexError, ValueError):
        parsed = None
    await state.clear()
    if parsed is None:
        await message.answer(t("draft_stale_resend", lang))
        return
    text, keyboard = _receipt_draft_view(parsed, data["receipt_draft_id"], lang)
    await message.answer(t("item_updated", lang, text=text), reply_markup=keyboard)

@router.callback_query(F.data.startswith("recv_drop:"))
async def receipt_draft_delete_item(callback: CallbackQuery):
    _, draft_id, index_raw = callback.data.split(":")
    try:
        parsed = db.delete_receipt_draft_item(
            callback.from_user.id, draft_id, int(index_raw)
        )
    except IndexError:
        parsed = None
    except ValueError:
        await callback.answer(t("last_item_block", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    if parsed is None:
        await callback.answer(t("draft_stale_item", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    await _refresh_receipt_draft(callback, draft_id, parsed)
    await callback.answer(t("item_deleted", db.get_user_language(callback.from_user.id)))

@router.callback_query(F.data.startswith("recv_total_items:"))
async def receipt_draft_use_items_total(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    try:
        parsed = db.resolve_receipt_draft_total(
            callback.from_user.id, draft_id, use_receipt_total=False
        )
    except ValueError:
        parsed = None
    if parsed is None:
        await callback.answer(t("draft_update_fail", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    await _refresh_receipt_draft(callback, draft_id, parsed)
    await callback.answer(t("used_items_total", db.get_user_language(callback.from_user.id)))

@router.callback_query(F.data.startswith("recv_total_receipt:"))
async def receipt_draft_use_receipt_total(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    try:
        parsed = db.resolve_receipt_draft_total(
            callback.from_user.id, draft_id, use_receipt_total=True
        )
    except ValueError:
        parsed = None
    if parsed is None:
        await callback.answer(t("receipt_total_bad", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    await _refresh_receipt_draft(callback, draft_id, parsed)
    await callback.answer(t("used_receipt_total", db.get_user_language(callback.from_user.id)))

@router.callback_query(F.data.startswith("recv_save:"))
async def confirm_receipt_save(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    lang = db.get_user_language(user_id)
    today = db.user_today(user_id).isoformat()

    parsed = db.get_receipt_draft(user_id, draft_id)
    if parsed is not None:
        if parsed.get("ambiguous_type"):
            await _refresh_receipt_draft(callback, draft_id, parsed)
            await callback.answer(
                t("choose_type_first", lang),
                show_alert=True,
            )
            return
        item_total = sum(int(item["price"]) for item in parsed.get("items") or [])
        receipt_total = parsed.get("total")
        if (
            receipt_total is not None
            and abs(item_total - int(receipt_total)) > db.RECEIPT_TOTAL_MISMATCH_TIYN
        ):
            await _refresh_receipt_draft(callback, draft_id, parsed)
            await callback.answer(t("choose_total_first", lang), show_alert=True)
            return

    try:
        result = db.save_receipt_draft(user_id, draft_id, fallback_date=today)
    except Exception:
        # Транзакция уже откатилась; черновик остался, поэтому пользователь
        # может нажать «Сохранить» ещё раз после временного сбоя.
        logger.exception("Не удалось атомарно сохранить чек %s", draft_id)
        await callback.answer(
            t("save_retry", lang),
            show_alert=True,
        )
        return

    if result is None:
        await callback.answer(t("draft_stale_photo", lang), show_alert=True)
        return

    receipt_id, touched_categories = result
    edit_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=t("btn_receipt_items", lang), callback_data=f"recv_items:{receipt_id}"
    )]])
    text = callback.message.text + t("receipt_saved", lang)
    warning = _budget_warnings_text(user_id, touched_categories)
    if warning:
        text = f"{text}\n\n{warning}"
    await callback.message.edit_text(text, reply_markup=edit_kb)
    await callback.answer(t("saved", lang))

@router.callback_query(F.data.startswith("recv_items:"))
async def show_receipt_items(callback: CallbackQuery):
    receipt_id = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id

    lang = db.get_user_language(user_id)
    buttons = []
    lines = [t("receipt_items_title", lang)]
    # Связь позиций с чеком берём из самой базы (transactions.receipt_id):
    # переживает рестарт и не врёт, если часть позиций уже удалили.
    for tx in db.get_transactions_by_receipt(user_id, receipt_id):
        lines.append(f"• {hx(tx['description'])} — {format_money(tx['amount'], lang)} [{hx(tx['category_name']) or t('dash', lang)}]")
        buttons.append([InlineKeyboardButton(
            text=f"✏️ {tx['description']}"[:60], callback_data=f"tx_open:{tx['id']}"
        )])
    if not buttons:
        await callback.answer(t("items_not_found", lang), show_alert=True)
        return
    await callback.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@router.callback_query(F.data.startswith("recv_cancel:"))
async def cancel_receipt_save(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    db.delete_state(RECEIPT_SCOPE, draft_id, user_id=callback.from_user.id)
    await callback.message.edit_text(t("receipt_cancelled", db.get_user_language(callback.from_user.id)), reply_markup=None)
    await callback.answer()

@router.callback_query(F.data.startswith("recv_paypick:"))
async def receipt_pick_payment(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    if db.get_receipt_draft(callback.from_user.id, draft_id) is None:
        await callback.answer(t("draft_stale", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    lang = db.get_user_language(callback.from_user.id)
    keyboard = _with_back_button(
        payments_keyboard(callback.from_user.id, f"recv_pay:{draft_id}"),
        back_callback=f"recv_back:{draft_id}",
        lang=lang,
    )
    await callback.message.edit_text(t("choose_how_paid", lang), reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data.startswith("recv_pay:"))
async def receipt_set_payment(callback: CallbackQuery):
    _, draft_id, pm_id_raw = callback.data.split(":")
    parsed = db.update_receipt_draft_payment(
        callback.from_user.id, draft_id, int(pm_id_raw)
    )
    if parsed is None:
        await callback.answer(t("draft_or_pay_bad", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    await _refresh_receipt_draft(callback, draft_id, parsed)
    await callback.answer(t("payment_updated", db.get_user_language(callback.from_user.id), text="").split("\n", 1)[0])

@router.callback_query(F.data.startswith("recv_type:"))
async def receipt_set_type(callback: CallbackQuery):
    _, draft_id, tx_type = callback.data.split(":")
    parsed = db.update_receipt_draft_type(
        callback.from_user.id,
        draft_id,
        tx_type,
    )
    if parsed is None:
        await callback.answer(t("draft_stale", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    await _refresh_receipt_draft(callback, draft_id, parsed)
    await callback.answer(t("tx_type_updated", db.get_user_language(callback.from_user.id), text="").split("\n", 1)[0])

@router.callback_query(F.data.startswith("dup_yes:"))
async def duplicate_photo_yes(callback: CallbackQuery, bot: Bot):
    dup_id = callback.data.split(":", 1)[1]
    payload = db.pop_state(DUP_SCOPE, dup_id)
    if not isinstance(payload, dict) or payload.get("user_id") != callback.from_user.id:
        await callback.answer(t("request_stale", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(t("recognize_again", db.get_user_language(callback.from_user.id)))
    await _process_receipt(
        callback.message,
        bot,
        file_id=payload["file_id"],
        unique_id=payload["unique_id"],
        declared_size=payload.get("declared_size"),
        skip_duplicate_prompt=True,
        user_id=callback.from_user.id,
    )

@router.callback_query(F.data.startswith("dup_no:"))
async def duplicate_photo_no(callback: CallbackQuery):
    dup_id = callback.data.split(":", 1)[1]
    payload = db.pop_state(DUP_SCOPE, dup_id)
    if isinstance(payload, dict) and payload.get("user_id") != callback.from_user.id:
        await callback.answer(t("request_stale", db.get_user_language(callback.from_user.id)), show_alert=True)
        return
    await callback.message.edit_text(t("dup_skipped", db.get_user_language(callback.from_user.id)))
    await callback.answer()
