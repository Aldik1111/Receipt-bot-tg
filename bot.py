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

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import backup
import bank_import
import db
import charts
import export
from categorizer import categorize, categorize_many, categorize_smart
from config import ADMIN_USER_ID, BOT_TOKEN
from formatting import hx, money, parse_positive_amount
from fsm_storage import SQLiteStorage
from gemini_engine import generate_insight_text
from i18n import LANGUAGES, t
from receipt_pipeline import extract_receipt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
BACKGROUND_TASKS: list[asyncio.Task] = []
TEXT_HINT = "Пришли текстом. /cancel — сбросить сценарий."
MAX_IMPORT_BYTES = 2 * 1024 * 1024
# 20 МБ - предел, который Telegram Bot API вообще позволяет боту скачать у себя.
MAX_RESTORE_BYTES = 20 * 1024 * 1024

# ---------------------------------------------------------------------------
# Черновики распознанных чеков, ожидающих подтверждения, и незавершённый
# импорт файла. Лежат в SQLite (таблица app_state), а не в словарях в памяти:
# рестарт бота между "вот твой чек" и нажатием "Сохранить" больше не теряет
# результат распознавания, за который уже заплачен запрос к Gemini.
# ---------------------------------------------------------------------------
RECEIPT_SCOPE = "receipt_draft"
IMPORT_SCOPE = "import_draft"
LIST_SCOPE = "list_context"
RESTORE_SCOPE = "restore_draft"


class ManualEntry(StatesGroup):
    choosing_type = State()
    entering_amount = State()
    choosing_category = State()
    choosing_payment = State()
    entering_description = State()


class CategoryEntry(StatesGroup):
    entering_name = State()
    editing_name = State()
    editing_emoji = State()


class PaymentEntry(StatesGroup):
    entering_name = State()
    editing_name = State()


class RestoreEntry(StatesGroup):
    awaiting_file = State()


# ---------------------------------------------------------------------------
# Вспомогательные функции клавиатур
# ---------------------------------------------------------------------------

def categories_keyboard(user_id: int, prefix: str) -> InlineKeyboardMarkup:
    cats = db.get_categories(user_id)
    buttons = [
        [InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data=f"{prefix}:{c['id']}")]
        for c in cats
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payments_keyboard(user_id: int, prefix: str) -> InlineKeyboardMarkup:
    pms = db.get_payment_methods(user_id)
    buttons = [
        [InlineKeyboardButton(text=p["name"], callback_data=f"{prefix}:{p['id']}")]
        for p in pms
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _with_back_button(keyboard: InlineKeyboardMarkup, back_callback: str) -> InlineKeyboardMarkup:
    rows = list(keyboard.inline_keyboard) + [
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def period_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Сегодня", callback_data="stats:day"),
         InlineKeyboardButton(text="Неделя", callback_data="stats:week")],
        [InlineKeyboardButton(text="Месяц", callback_data="stats:month"),
         InlineKeyboardButton(text="3 месяца", callback_data="stats:3months")],
        [InlineKeyboardButton(text="Год", callback_data="stats:year")],
        [InlineKeyboardButton(text="📅 Свой период", callback_data="stats_custom")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    lang = db.get_user_language(message.from_user.id)
    await message.answer(t("start_greeting", lang) + t("help_text", lang))


@router.message(Command("help"))
async def cmd_help(message: Message):
    lang = db.get_user_language(message.from_user.id)
    await message.answer(t("help_text", lang))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("Ок, сценарий сброшен. Можешь начать заново.")
    else:
        await message.answer("Сейчас нет активного сценария.")


# ---------------------------------------------------------------------------
# Обработка фото чека
# ---------------------------------------------------------------------------

@router.message(StateFilter(None), F.photo)
async def handle_receipt_photo(message: Message, bot: Bot):
    photo = message.photo[-1]  # берём максимальное качество из доступных превью
    await _process_receipt(message, bot, file_id=photo.file_id, unique_id=photo.file_unique_id)


@router.message(StateFilter(None), F.document, F.document.mime_type.startswith("image/"))
async def handle_receipt_document(message: Message, bot: Bot):
    # Telegram сильно сжимает фото, отправленные как "фото" - это одна из
    # главных причин плохого распознавания. Если прислать снимок как файл
    # (документ), сжатия нет и OCR работает заметно точнее.
    doc = message.document
    await _process_receipt(message, bot, file_id=doc.file_id, unique_id=doc.file_unique_id)


async def _process_receipt(message: Message, bot: Bot, file_id: str, unique_id: str):
    db.ensure_user(message.from_user.id, message.from_user.username)

    status = await message.answer("🔍 Распознаю чек, подожди немного...")

    file = await bot.get_file(file_id)
    suffix = os.path.splitext(file.file_path or "")[1].lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    local_path = os.path.join(
        tempfile.gettempdir(),
        f"receipt_{message.from_user.id}_{uuid.uuid4().hex}{suffix}",
    )
    await bot.download_file(file.file_path, destination=local_path)

    try:
        # Gemini ходит по сети синхронным requests (до 30 сек) и внутри ещё
        # категоризирует товары через SQLite. В event loop это заморозило бы
        # бота целиком для всех пользователей - выносим в отдельный поток.
        parsed = await asyncio.to_thread(extract_receipt, local_path, message.from_user.id)
    except Exception:
        logger.exception("Receipt extraction error")
        await status.edit_text(
            "⚠️ Не получилось распознать чек. Попробуй сфотографировать ровнее, "
            "при хорошем освещении, или добавь трату вручную - просто напиши "
            "сообщением, например: 500 такси"
        )
        return
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)

    if not parsed["items"]:
        await status.edit_text(
            "⚠️ Не нашёл товаров на чеке (Gemini не справился).\n"
            "Совет: пришли фото чека как ФАЙЛ (📎 → Файл, не как фото) - "
            "так Telegram не сжимает изображение и распознавание работает точнее.\n"
            "Либо просто напиши сообщением: 500 такси"
        )
        return

    draft_id = uuid.uuid4().hex[:16]
    db.save_state(RECEIPT_SCOPE, draft_id, parsed, user_id=message.from_user.id)

    text = _format_receipt_preview(parsed)
    if len(text) > 3500:
        text = text[:3500].rstrip() + "\n… позиции обрезаны в превью, при сохранении запишутся все."
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сохранить всё", callback_data=f"recv_save:{draft_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"recv_cancel:{draft_id}"),
            ]
        ]
    )
    await status.edit_text(text, reply_markup=keyboard)


SOURCE_LABELS = {
    "gemini": "🤖 Gemini AI",
}


def _format_receipt_preview(parsed: dict) -> str:
    lines = ["🧾 <b>Распознанный чек</b>"]
    source_label = SOURCE_LABELS.get(parsed.get("source"))
    if source_label:
        lines.append(f"<i>Источник: {source_label}</i>")
    lines.append("")
    lines.append(f"Магазин: {hx(parsed['store']) or '—'}")
    lines.append(f"Дата: {hx(parsed['date']) or '—'}   Время: {hx(parsed['time']) or '—'}\n")
    total = 0.0
    for item in parsed["items"]:
        category = item.get("category", "Прочее")
        lines.append(f"• {hx(item['name'])} — {money(item['price'])}  [{hx(category)}]")
        total += item["price"]
    lines.append(f"\nИтого по товарам: {money(total)}")
    if parsed["total"] is not None:
        lines.append(f"Итого по чеку: {money(parsed['total'])}")
    lines.append(
        "\nЕсли что-то распознано неверно - удобнее скорректировать это "
        "потом прямо в базе, либо переснять чек. Проверь и сохрани."
    )
    return "\n".join(lines)


@router.callback_query(F.data.startswith("recv_save:"))
async def confirm_receipt_save(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    today = date.today().isoformat()

    try:
        result = db.save_receipt_draft(user_id, draft_id, fallback_date=today)
    except Exception:
        # Транзакция уже откатилась; черновик остался, поэтому пользователь
        # может нажать «Сохранить» ещё раз после временного сбоя.
        logger.exception("Не удалось атомарно сохранить чек %s", draft_id)
        await callback.answer(
            "Не удалось сохранить. Черновик не потерян — попробуй ещё раз.",
            show_alert=True,
        )
        return

    if result is None:
        await callback.answer("Черновик устарел, пришли фото заново", show_alert=True)
        return

    receipt_id, touched_categories = result
    edit_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📝 Позиции чека (поправить)", callback_data=f"recv_items:{receipt_id}"
    )]])
    await callback.message.edit_text(
        callback.message.text + "\n\n✅ Сохранено в базу!", reply_markup=edit_kb
    )
    for cat_id in touched_categories:
        warning = await _budget_warning_text(user_id, cat_id)
        if warning:
            await callback.message.answer(warning)
    await callback.answer("Сохранено")


@router.callback_query(F.data.startswith("recv_items:"))
async def show_receipt_items(callback: CallbackQuery):
    receipt_id = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id

    buttons = []
    lines = ["🧾 <b>Позиции чека</b>\n"]
    # Связь позиций с чеком берём из самой базы (transactions.receipt_id):
    # переживает рестарт и не врёт, если часть позиций уже удалили.
    for tx in db.get_transactions_by_receipt(user_id, receipt_id):
        lines.append(f"• {hx(tx['description'])} — {money(tx['amount'])} [{hx(tx['category_name']) or '—'}]")
        buttons.append([InlineKeyboardButton(
            text=f"✏️ {tx['description']}"[:60], callback_data=f"tx_open:{tx['id']}"
        )])
    if not buttons:
        await callback.answer("Позиции не найдены", show_alert=True)
        return
    await callback.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("recv_cancel:"))
async def cancel_receipt_save(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    db.delete_state(RECEIPT_SCOPE, draft_id, user_id=callback.from_user.id)
    await callback.message.edit_text("❌ Отменено, ничего не сохранено.", reply_markup=None)
    await callback.answer()


# ---------------------------------------------------------------------------
# Ручное добавление операции: /add
# ---------------------------------------------------------------------------

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    db.ensure_user(message.from_user.id, message.from_user.username)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💸 Расход", callback_data="type:expense"),
                InlineKeyboardButton(text="💰 Доход", callback_data="type:income"),
            ]
        ]
    )
    await message.answer("Что добавляем?", reply_markup=keyboard)
    await state.set_state(ManualEntry.choosing_type)


@router.callback_query(ManualEntry.choosing_type, F.data.startswith("type:"))
async def add_choose_type(callback: CallbackQuery, state: FSMContext):
    tx_type = callback.data.split(":", 1)[1]
    await state.update_data(tx_type=tx_type)
    await callback.message.edit_text("Введи сумму:")
    await state.set_state(ManualEntry.entering_amount)
    await callback.answer()


@router.message(ManualEntry.entering_amount)
async def add_enter_amount(message: Message, state: FSMContext):
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer("Пришли число, например: 350 или 350.50")
        return

    await state.update_data(amount=amount)
    await message.answer(
        "Выбери категорию:",
        reply_markup=categories_keyboard(message.from_user.id, "cat"),
    )
    await state.set_state(ManualEntry.choosing_category)


@router.callback_query(ManualEntry.choosing_category, F.data.startswith("cat:"))
async def add_choose_category(callback: CallbackQuery, state: FSMContext):
    category_id = int(callback.data.split(":", 1)[1])
    await state.update_data(category_id=category_id)
    await callback.message.edit_text(
        "Способ оплаты:",
        reply_markup=payments_keyboard(callback.from_user.id, "pay"),
    )
    await state.set_state(ManualEntry.choosing_payment)
    await callback.answer()


@router.callback_query(ManualEntry.choosing_payment, F.data.startswith("pay:"))
async def add_choose_payment(callback: CallbackQuery, state: FSMContext):
    payment_id = int(callback.data.split(":", 1)[1])
    await state.update_data(payment_method_id=payment_id)
    await callback.message.edit_text("Короткое описание (или отправь '-' чтобы пропустить):")
    await state.set_state(ManualEntry.entering_description)
    await callback.answer()


@router.message(ManualEntry.entering_description)
async def add_enter_description(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    data = await state.get_data()
    description = None if message.text.strip() == "-" else message.text.strip()

    tx_id = db.add_transaction(
        user_id=message.from_user.id,
        tx_type=data["tx_type"],
        amount=data["amount"],
        category_id=data["category_id"],
        payment_method_id=data["payment_method_id"],
        store=None,
        description=description,
        op_date=date.today().strftime("%Y-%m-%d"),
        op_time=datetime.now().strftime("%H:%M"),
    )
    db.touch_activity(message.from_user.id, date.today().isoformat())
    await state.clear()
    emoji = "💰" if data["tx_type"] == "income" else "💸"
    text = f"{emoji} Записано: {money(data['amount'])} — {hx(description) or 'без описания'}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить", callback_data=f"tx_open:{tx_id}")]]
    )
    await message.answer(text, reply_markup=keyboard)
    if data["tx_type"] == "expense":
        warning = await _budget_warning_text(message.from_user.id, data["category_id"])
        if warning:
            await message.answer(warning)


# ---------------------------------------------------------------------------
# Статистика: /stats
# ---------------------------------------------------------------------------

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer("За какой период?", reply_markup=period_keyboard())


def _previous_period_bounds(date_from: str, date_to: str) -> tuple[str, str]:
    """Предыдущий период той же длины, сразу перед текущим - для сравнения."""
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    length = (d_to - d_from).days + 1
    prev_to = d_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=length - 1)
    return prev_from.isoformat(), prev_to.isoformat()


def _pct_change(current: float, previous: float) -> str:
    if previous <= 0:
        return "н/д" if current <= 0 else "новое"
    change = (current - previous) / previous * 100
    arrow = "🔺" if change > 0 else ("🔻" if change < 0 else "▪️")
    return f"{arrow} {abs(change):.0f}%"


async def _budget_warning_text(user_id: int, category_id: int | None) -> str | None:
    if category_id is None:
        return None
    budget = db.get_budget_for_category(user_id, category_id)
    if not budget:
        return None
    today = date.today()
    start = today.replace(day=1)
    spent = db.get_category_spent(user_id, category_id, start.isoformat(), today.isoformat())
    limit = budget["monthly_limit"]
    pct = (spent / limit * 100) if limit > 0 else 0
    name = hx(db.get_category_name(user_id, category_id)) or "?"
    if pct >= 100:
        return f"⚠️ Бюджет по «{name}» превышен: {money(spent)} из {money(limit)} ({pct:.0f}%)"
    if pct >= 80:
        return f"🟡 Бюджет по «{name}»: {money(spent)} из {money(limit)} ({pct:.0f}%)"
    return None


def _month_start(today: date, months_back: int = 0) -> date:
    month = today.month - months_back
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _period_bounds(period: str) -> tuple[str, str, str]:
    today = date.today()
    if period == "day":
        return today.isoformat(), today.isoformat(), "за сегодня"
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat(), "за неделю"
    if period == "month":
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat(), "за месяц"
    if period == "3months":
        start = _month_start(today, months_back=2)
        return start.isoformat(), today.isoformat(), "за 3 месяца"
    if period == "year":
        start = today.replace(month=1, day=1)
        return start.isoformat(), today.isoformat(), "за год"
    raise ValueError(period)


RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
RU_MONTHS = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


async def _send_stats(message: Message, user_id: int, date_from: str, date_to: str, label: str):
    rows = db.get_transactions(user_id, date_from, date_to)
    expenses = [dict(r) for r in rows if r["type"] == "expense"]
    incomes = [dict(r) for r in rows if r["type"] == "income"]

    total_expense = sum(r["amount"] for r in expenses)
    total_income = sum(r["amount"] for r in incomes)

    prev_from, prev_to = _previous_period_bounds(date_from, date_to)
    prev_expense = db.get_total_expense(user_id, prev_from, prev_to)

    forecast_line = ""
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    today = date.today()
    if d_from <= today <= d_to and total_expense > 0:
        days_elapsed = (today - d_from).days + 1
        days_total = (d_to - d_from).days + 1
        if days_elapsed < days_total:
            forecast = total_expense / days_elapsed * days_total
            forecast_line = f"\n📈 Прогноз к концу периода: ~{money(forecast)}"

    summary = (
        f"📊 <b>Статистика {label}</b>\n"
        f"Период: {date_from} — {date_to}\n\n"
        f"💸 Расходы: {money(total_expense)}  ({_pct_change(total_expense, prev_expense)} к прошлому периоду)\n"
        f"💰 Доходы: {money(total_income)}\n"
        f"Баланс: {money(total_income - total_expense)}"
        f"{forecast_line}"
    )
    show_list_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📋 Показать операции", callback_data=f"recent_from:{date_from}:{date_to}"
    )]])
    await message.answer(summary, reply_markup=show_list_kb)

    if not expenses:
        await message.answer("Расходов за этот период нет.")
        return

    # Отрисовка PNG в matplotlib - заметная CPU-работа, в поток её
    buf = await asyncio.to_thread(charts.pie_chart_by_category, expenses, f"Расходы {label}")
    await message.answer_photo(BufferedInputFile(buf.read(), filename="by_category.png"))

    # Куда - топ магазинов/мест
    store_totals: dict[str, float] = defaultdict(float)
    for e in expenses:
        if e["store"]:
            store_totals[e["store"]] += e["amount"]
    if store_totals:
        top_stores = sorted(store_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
        lines = ["📍 <b>Топ мест трат</b>"]
        for name, amount in top_stores:
            lines.append(f"• {hx(name)} — {money(amount)}")
        await message.answer("\n".join(lines))

    # Способы оплаты
    pay_totals: dict[str, float] = defaultdict(float)
    for e in expenses:
        pay_totals[e["payment_name"] or "Без указания"] += e["amount"]
    if len(pay_totals) > 1 or "Без указания" not in pay_totals:
        lines = ["💳 <b>По способам оплаты</b>"]
        for name, amount in sorted(pay_totals.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"• {hx(name)} — {money(amount)}")
        await message.answer("\n".join(lines))

    # Когда - тренд трат по времени (не строим, если период короче 2 дней - смысла мало)
    span_days = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    if span_days > 1:
        buckets: dict[str, float] = {}
        for e in expenses:
            d = date.fromisoformat(e["op_date"])
            if span_days <= 31:
                key = f"{d.isoformat()} {RU_WEEKDAYS[d.weekday()]}"
            elif span_days <= 120:
                iso = d.isocalendar()
                key = f"{iso[0]}-W{iso[1]:02d}"
            else:
                key = f"{d.year}-{d.month:02d} {RU_MONTHS[d.month - 1]}"
            buckets[key] = buckets.get(key, 0) + e["amount"]
        if len(buckets) > 1:
            buf2 = await asyncio.to_thread(
                charts.bar_chart_by_period, buckets, f"Динамика трат {label}"
            )
            await message.answer_photo(BufferedInputFile(buf2.read(), filename="trend.png"))


@router.callback_query(F.data.startswith("stats:"))
async def show_stats(callback: CallbackQuery):
    period = callback.data.split(":", 1)[1]
    date_from, date_to, label = _period_bounds(period)
    await _send_stats(callback.message, callback.from_user.id, date_from, date_to, label)
    await callback.answer()


class StatsCustomPeriod(StatesGroup):
    entering_dates = State()


@router.callback_query(F.data == "stats_custom")
async def stats_custom_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(StatsCustomPeriod.entering_dates)
    await callback.message.edit_text(
        "Введи период в формате <code>ДД.ММ.ГГГГ ДД.ММ.ГГГГ</code>\n"
        "Например: <code>01.03.2026 15.03.2026</code>"
    )
    await callback.answer()


@router.message(StatsCustomPeriod.entering_dates)
async def stats_custom_apply(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Нужно два числа через пробел, например: 01.03.2026 15.03.2026")
        return
    try:
        d_from = datetime.strptime(parts[0], "%d.%m.%Y").date()
        d_to = datetime.strptime(parts[1], "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Не разобрал даты. Формат: ДД.ММ.ГГГГ ДД.ММ.ГГГГ")
        return
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    await state.clear()
    await _send_stats(message, message.from_user.id, d_from.isoformat(), d_to.isoformat(), "за выбранный период")


@router.callback_query(F.data.startswith("recent_from:"))
async def recent_from_stats(callback: CallbackQuery):
    _, date_from, date_to = callback.data.split(":")
    text, keyboard = _recent_view(callback.from_user.id, date_from, date_to, 0)
    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# Категории: /categories
# ---------------------------------------------------------------------------

PROTECTED_CATEGORY = "Прочее"  # на неё переносятся траты при удалении других категорий


def _categories_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cats = db.get_categories(user_id)
    text = "🏷 <b>Твои категории</b>\n\nНажми 🎨 чтобы сменить эмодзи, ✏️ переименовать, 🗑 удалить."

    buttons = []
    for c in cats:
        if c["name"] == PROTECTED_CATEGORY:
            buttons.append([InlineKeyboardButton(text=f"{c['emoji']} {c['name']} 🔒", callback_data="noop")])
        else:
            buttons.append([
                InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data="noop"),
                InlineKeyboardButton(text="🎨", callback_data=f"cat_emoji:{c['id']}"),
                InlineKeyboardButton(text="✏️", callback_data=f"cat_edit:{c['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"cat_del:{c['id']}"),
            ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data="cat_add")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("categories"))
async def cmd_categories(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _categories_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer("Это базовая категория - её нельзя переименовать или удалить")


@router.callback_query(F.data == "cat_add")
async def cat_add_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введи название новой категории:")
    await state.set_state(CategoryEntry.entering_name)
    await callback.answer()


@router.message(CategoryEntry.entering_name)
async def add_category_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    name = message.text.strip()
    db.add_category(message.from_user.id, name)
    await state.clear()
    text, keyboard = _categories_view(message.from_user.id)
    await message.answer(f"✅ Категория «{hx(name)}» добавлена.\n\n{text}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("cat_edit:"))
async def cat_edit_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split(":", 1)[1])
    if db.get_category_name(callback.from_user.id, cat_id) == PROTECTED_CATEGORY:
        await callback.answer("Это базовая категория - её нельзя переименовать", show_alert=True)
        return
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(CategoryEntry.editing_name)
    await callback.message.edit_text("Введи новое название для этой категории:")
    await callback.answer()


@router.callback_query(F.data.startswith("cat_emoji:"))
async def cat_emoji_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(CategoryEntry.editing_emoji)
    await callback.message.edit_text("Пришли один эмодзи для этой категории:")
    await callback.answer()


@router.message(CategoryEntry.editing_emoji)
async def cat_emoji_apply(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    data = await state.get_data()
    emoji = message.text.strip()
    if len(emoji) > 4:  # грубая защита от случайного текста вместо эмодзи
        await message.answer("Это не похоже на один эмодзи, попробуй ещё раз.")
        return
    db.set_category_emoji(message.from_user.id, data["edit_cat_id"], emoji)
    await state.clear()
    text, keyboard = _categories_view(message.from_user.id)
    await message.answer(f"✅ Эмодзи обновлён.\n\n{text}", reply_markup=keyboard)


@router.message(CategoryEntry.editing_name)
async def edit_category_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    data = await state.get_data()
    cat_id = data["edit_cat_id"]
    new_name = message.text.strip()
    ok = db.rename_category(message.from_user.id, cat_id, new_name)
    await state.clear()

    text, keyboard = _categories_view(message.from_user.id)
    if ok:
        await message.answer(f"✅ Переименовано в «{hx(new_name)}».\n\n{text}", reply_markup=keyboard)
    else:
        await message.answer(
            f"⚠️ Категория «{hx(new_name)}» уже есть - выбери другое название.\n\n{text}",
            reply_markup=keyboard,
        )


@router.callback_query(F.data.startswith("cat_del:"))
async def cat_delete_confirm(callback: CallbackQuery):
    cat_id = int(callback.data.split(":", 1)[1])
    count = db.count_transactions_for_category(callback.from_user.id, cat_id)
    note = (
        f"У неё {count} операций - они будут перенесены в «{PROTECTED_CATEGORY}»."
        if count
        else "Операций с ней пока нет."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"cat_del_yes:{cat_id}"),
                InlineKeyboardButton(text="◀️ Отмена", callback_data="cat_del_no"),
            ]
        ]
    )
    await callback.message.edit_text(f"Удалить эту категорию?\n{note}", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("cat_del_yes:"))
async def cat_delete_apply(callback: CallbackQuery):
    cat_id = int(callback.data.split(":", 1)[1])
    ok = db.delete_category(callback.from_user.id, cat_id, fallback_name=PROTECTED_CATEGORY)
    text, keyboard = _categories_view(callback.from_user.id)
    prefix = "✅ Удалено.\n\n" if ok else "⚠️ Не удалось удалить.\n\n"
    await callback.message.edit_text(prefix + text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "cat_del_no")
async def cat_delete_cancel(callback: CallbackQuery):
    text, keyboard = _categories_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# Способы оплаты: /payments
# ---------------------------------------------------------------------------

def _payments_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    pms = db.get_payment_methods(user_id)
    text = "💳 <b>Твои способы оплаты</b>\n\nНажми ✏️ чтобы переименовать, 🗑 чтобы удалить."

    buttons = []
    for p in pms:
        buttons.append([
            InlineKeyboardButton(text=p["name"], callback_data="noop_pay"),
            InlineKeyboardButton(text="✏️", callback_data=f"pay_edit:{p['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"pay_del:{p['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить способ оплаты", callback_data="pay_add")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("payments"))
async def cmd_payments(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _payments_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "noop_pay")
async def noop_pay_callback(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "pay_add")
async def pay_add_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введи название способа оплаты (например: Kaspi Gold, Тинькофф):")
    await state.set_state(PaymentEntry.entering_name)
    await callback.answer()


@router.message(PaymentEntry.entering_name)
async def add_payment_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    name = message.text.strip()
    db.add_payment_method(message.from_user.id, name)
    await state.clear()
    text, keyboard = _payments_view(message.from_user.id)
    await message.answer(f"✅ Способ оплаты «{hx(name)}» добавлен.\n\n{text}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("pay_edit:"))
async def pay_edit_start(callback: CallbackQuery, state: FSMContext):
    pm_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_pm_id=pm_id)
    await state.set_state(PaymentEntry.editing_name)
    await callback.message.edit_text("Введи новое название для этого способа оплаты:")
    await callback.answer()


@router.message(PaymentEntry.editing_name)
async def edit_payment_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    data = await state.get_data()
    pm_id = data["edit_pm_id"]
    new_name = message.text.strip()
    ok = db.rename_payment_method(message.from_user.id, pm_id, new_name)
    await state.clear()

    text, keyboard = _payments_view(message.from_user.id)
    if ok:
        await message.answer(f"✅ Переименовано в «{hx(new_name)}».\n\n{text}", reply_markup=keyboard)
    else:
        await message.answer(
            f"⚠️ Способ оплаты «{hx(new_name)}» уже есть - выбери другое название.\n\n{text}",
            reply_markup=keyboard,
        )


@router.callback_query(F.data.startswith("pay_del:"))
async def pay_delete_confirm(callback: CallbackQuery):
    pm_id = int(callback.data.split(":", 1)[1])
    count = db.count_transactions_for_payment_method(callback.from_user.id, pm_id)
    note = (
        f"У него {count} операций - способ оплаты у них станет пустым."
        if count
        else "Операций с ним пока нет."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"pay_del_yes:{pm_id}"),
                InlineKeyboardButton(text="◀️ Отмена", callback_data="pay_del_no"),
            ]
        ]
    )
    await callback.message.edit_text(f"Удалить этот способ оплаты?\n{note}", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("pay_del_yes:"))
async def pay_delete_apply(callback: CallbackQuery):
    pm_id = int(callback.data.split(":", 1)[1])
    ok = db.delete_payment_method(callback.from_user.id, pm_id)
    text, keyboard = _payments_view(callback.from_user.id)
    if ok:
        prefix = "✅ Удалено.\n\n"
    else:
        prefix = "⚠️ Нельзя удалить последний оставшийся способ оплаты.\n\n"
    await callback.message.edit_text(prefix + text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "pay_del_no")
async def pay_delete_cancel(callback: CallbackQuery):
    text, keyboard = _payments_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# Быстрый ввод одной строкой текста, без единого клика: "500 такси" / "+50000 зарплата"
# Срабатывает только когда пользователь не находится в другом сценарии (FSM state=None)
# ---------------------------------------------------------------------------

QUICK_ADD_RE = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*(.*)$", re.DOTALL)


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def quick_add(message: Message, state: FSMContext):
    db.ensure_user(message.from_user.id, message.from_user.username)

    if message.forward_date:
        user_id = message.from_user.id
        s = db.get_settings(user_id)
        if s and s["bank_import_enabled"] and bank_import.looks_like_bank_notification(message.text):
            parsed = bank_import.parse_bank_notification(message.text)
            if not parsed:
                await message.answer("📨 Похоже на банковское уведомление, но не смог распознать сумму.")
                return
            # message_id уникален только внутри чата; user_id не даёт
            # черновикам двух пользователей перезаписать друг друга.
            draft_id = f"bank_{user_id}_{message.message_id}"
            draft = {
                "store": parsed["store"], "date": date.today().isoformat(), "time": None,
                "items": [{"name": parsed["store"] or "Банковское уведомление", "price": parsed["amount"],
                            "category": categorize(parsed["store"] or "")}],
                "total": parsed["amount"], "raw_text": "", "source": "bank_notification",
                "tx_type": parsed["type"],
            }
            db.save_state(RECEIPT_SCOPE, draft_id, draft, user_id=user_id)
            preview = _format_receipt_preview(draft)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Сохранить", callback_data=f"recv_save:{draft_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"recv_cancel:{draft_id}"),
            ]])
            await message.answer("📨 Распознал банковское уведомление:\n\n" + preview, reply_markup=keyboard)
            return
        # импорт выключен или не похоже на банк - падаем ниже, обрабатываем как обычный текст

    match = QUICK_ADD_RE.match(message.text)
    if not match:
        await message.answer(
            "Не понял 🤔 Формат: <b>сумма</b> и что купил, например:\n"
            "<code>500 такси</code>  или  <code>+50000 зарплата</code>\n"
            "Либо используй /add для пошагового ввода."
        )
        return

    amount_raw, description_raw = match.groups()

    tx_type = "income" if amount_raw.strip().startswith("+") else "expense"
    amount = parse_positive_amount(amount_raw.lstrip("+"))
    if amount is None:
        await message.answer("Не смог разобрать сумму, попробуй ещё раз.")
        return

    description = description_raw.strip() or None
    category_name = await asyncio.to_thread(categorize_smart, message.from_user.id, description) if description else "Прочее"
    category_id = db.get_category_id_by_name(message.from_user.id, category_name)
    payment_method_id = db.get_default_payment_method_id(message.from_user.id)

    tx_id = db.add_transaction(
        user_id=message.from_user.id,
        tx_type=tx_type,
        amount=amount,
        category_id=category_id,
        payment_method_id=payment_method_id,
        store=None,
        description=description,
        op_date=date.today().strftime("%Y-%m-%d"),
        op_time=datetime.now().strftime("%H:%M"),
    )
    db.touch_activity(message.from_user.id, date.today().isoformat())

    emoji = "💰" if tx_type == "income" else "💸"
    text = f"{emoji} Записано: {money(amount)} — {hx(description) or 'без описания'}\nКатегория: {hx(category_name)}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить", callback_data=f"tx_open:{tx_id}")]]
    )
    await message.answer(text, reply_markup=keyboard)
    if tx_type == "expense":
        warning = await _budget_warning_text(message.from_user.id, category_id)
        if warning:
            await message.answer(warning)


# ---------------------------------------------------------------------------
# Просмотр и редактирование операций: /recent
# ---------------------------------------------------------------------------

PAGE_SIZE = 8
TYPE_EMOJI = {"expense": "💸", "income": "💰"}

# Последний открытый пользователем список операций (фильтр по датам + номер
# страницы) - нужно, чтобы кнопка "◀️ Назад" из карточки операции возвращала
# туда же, откуда пришли, а не всегда на первую страницу без фильтра.

def _get_list_context(user_id: int) -> tuple[str | None, str | None, int]:
    saved = db.load_state(LIST_SCOPE, str(user_id))
    if not saved:
        return None, None, 0
    date_from, date_to, page = saved
    return date_from, date_to, page


def _recent_view(
    user_id: int, date_from: str | None, date_to: str | None, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    db.save_state(LIST_SCOPE, str(user_id), [date_from, date_to, page], user_id=user_id)

    total = db.count_all_transactions(user_id, date_from, date_to)
    rows = db.get_recent_transactions(
        user_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE, date_from=date_from, date_to=date_to
    )

    if date_from and date_to:
        header = f"📋 <b>Операции за период</b>\n{date_from} — {date_to}"
    else:
        header = "📋 <b>Последние операции</b>"

    if not rows:
        return header + "\n\nЗдесь пока пусто.", InlineKeyboardMarkup(inline_keyboard=[])

    lines = [header, ""]
    buttons = []
    for r in rows:
        emoji = TYPE_EMOJI.get(r["type"], "•")
        label = hx(r["description"] or r["store"] or "без описания")
        cat = f" [{r['category_emoji']} {hx(r['category_name'])}]" if r["category_name"] else ""
        lines.append(f"{emoji} {r['op_date']} · {money(r['amount'])} · {label}{cat}")
        buttons.append([InlineKeyboardButton(
            text=f"✏️ {r['op_date']} · {money(r['amount'])} · {label}"[:60],
            callback_data=f"tx_open:{r['id']}",
        )])

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines.append(f"\nСтраница {page + 1} из {total_pages}")

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Раньше", callback_data="recent_page:prev"))
    if (page + 1) * PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton(text="Позже ▶️", callback_data="recent_page:next"))
    if nav_row:
        buttons.append(nav_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("recent"))
async def cmd_recent(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _recent_view(message.from_user.id, None, None, 0)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("recent_page:"))
async def recent_page_nav(callback: CallbackQuery):
    direction = callback.data.split(":", 1)[1]
    date_from, date_to, page = _get_list_context(callback.from_user.id)
    page = page + 1 if direction == "next" else max(0, page - 1)
    text, keyboard = _recent_view(callback.from_user.id, date_from, date_to, page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "recent_back")
async def recent_back(callback: CallbackQuery):
    date_from, date_to, page = _get_list_context(callback.from_user.id)
    text, keyboard = _recent_view(callback.from_user.id, date_from, date_to, page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


def _tx_detail_view(user_id: int, tx_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    r = db.get_transaction_by_id(user_id, tx_id)
    if not r:
        return None

    emoji = TYPE_EMOJI.get(r["type"], "•")
    type_label = "Доход" if r["type"] == "income" else "Расход"
    try:
        display_date = datetime.strptime(r["op_date"], "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        display_date = hx(r["op_date"]) or "—"
    cat_display = f"{r['category_emoji']} {hx(r['category_name'])}" if r["category_name"] else "—"
    lines = [
        f"{emoji} <b>{money(r['amount'])}</b>",
        f"Тип: {type_label}",
        f"Дата: {display_date}" + (f"  Время: {r['op_time']}" if r["op_time"] else ""),
        f"Категория: {cat_display}",
        f"Способ оплаты: {hx(r['payment_name']) or '—'}",
        f"Магазин: {hx(r['store']) or '—'}",
        f"Описание: {hx(r['description']) or '—'}",
    ]
    text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Сумма", callback_data=f"tx_amount:{tx_id}"),
                InlineKeyboardButton(text="🏷 Категория", callback_data=f"tx_cat:{tx_id}"),
            ],
            [
                InlineKeyboardButton(text="💳 Оплата", callback_data=f"tx_pay:{tx_id}"),
                InlineKeyboardButton(text="📝 Описание", callback_data=f"tx_desc:{tx_id}"),
            ],
            [
                InlineKeyboardButton(text="📅 Дата", callback_data=f"tx_date:{tx_id}"),
                InlineKeyboardButton(text="🏪 Магазин", callback_data=f"tx_store:{tx_id}"),
            ],
            [InlineKeyboardButton(text="🔄 Тип операции", callback_data=f"tx_type:{tx_id}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"tx_del:{tx_id}")],
            [InlineKeyboardButton(text="◀️ К списку", callback_data="recent_back")],
        ]
    )
    return text, keyboard


@router.callback_query(F.data.startswith("tx_open:"))
async def tx_open(callback: CallbackQuery):
    tx_id = int(callback.data.split(":", 1)[1])
    view = _tx_detail_view(callback.from_user.id, tx_id)
    if not view:
        await callback.answer("Операция не найдена (возможно, уже удалена)", show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


class TransactionEdit(StatesGroup):
    entering_amount = State()
    entering_description = State()
    entering_date = State()
    entering_store = State()


@router.callback_query(F.data.startswith("tx_amount:"))
async def tx_edit_amount_start(callback: CallbackQuery, state: FSMContext):
    tx_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_tx_id=tx_id)
    await state.set_state(TransactionEdit.entering_amount)
    await callback.message.edit_text("Введи новую сумму:")
    await callback.answer()


@router.message(TransactionEdit.entering_amount)
async def tx_edit_amount_apply(message: Message, state: FSMContext):
    data = await state.get_data()
    tx_id = data["edit_tx_id"]
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer("Пришли число больше нуля, например: 350")
        return
    db.update_transaction_amount(message.from_user.id, tx_id, amount)
    await state.clear()
    view = _tx_detail_view(message.from_user.id, tx_id)
    if not view:
        await message.answer("Операция не найдена (возможно, уже удалена)")
        await state.clear()
        return
    text, keyboard = view
    await message.answer(f"✅ Сумма обновлена.\n\n{text}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("tx_date:"))
async def tx_edit_date_start(callback: CallbackQuery, state: FSMContext):
    tx_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_tx_id=tx_id)
    await state.set_state(TransactionEdit.entering_date)
    await callback.message.edit_text(
        "Введи новую дату в формате <code>ДД.ММ.ГГГГ</code>, например "
        "<code>04.09.2026</code>:"
    )
    await callback.answer()


@router.message(TransactionEdit.entering_date)
async def tx_edit_date_apply(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    try:
        new_date = datetime.strptime(message.text.strip(), "%d.%m.%Y").date().isoformat()
    except ValueError:
        await message.answer("Не разобрал дату. Используй формат ДД.ММ.ГГГГ, например 04.09.2026.")
        return
    data = await state.get_data()
    tx_id = data["edit_tx_id"]
    if not db.update_transaction_date(message.from_user.id, tx_id, new_date):
        await state.clear()
        await message.answer("Операция не найдена (возможно, уже удалена)")
        return
    await state.clear()
    text, keyboard = _tx_detail_view(message.from_user.id, tx_id)
    await message.answer(f"✅ Дата обновлена.\n\n{text}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("tx_store:"))
async def tx_edit_store_start(callback: CallbackQuery, state: FSMContext):
    tx_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_tx_id=tx_id)
    await state.set_state(TransactionEdit.entering_store)
    await callback.message.edit_text("Введи новый магазин (или '-' чтобы убрать):")
    await callback.answer()


@router.message(TransactionEdit.entering_store)
async def tx_edit_store_apply(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    value = message.text.strip()
    if len(value) > 128:
        await message.answer("Название магазина слишком длинное. Максимум 128 символов.")
        return
    new_store = None if value == "-" else value
    data = await state.get_data()
    tx_id = data["edit_tx_id"]
    if not db.update_transaction_store(message.from_user.id, tx_id, new_store):
        await state.clear()
        await message.answer("Операция не найдена (возможно, уже удалена)")
        return
    await state.clear()
    text, keyboard = _tx_detail_view(message.from_user.id, tx_id)
    await message.answer(f"✅ Магазин обновлён.\n\n{text}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("tx_type:"))
async def tx_edit_type_start(callback: CallbackQuery):
    tx_id = int(callback.data.split(":", 1)[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💸 Расход", callback_data=f"tx_settype:{tx_id}:expense"),
            InlineKeyboardButton(text="💰 Доход", callback_data=f"tx_settype:{tx_id}:income"),
        ],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"tx_open:{tx_id}")],
    ])
    await callback.message.edit_text("Выбери новый тип операции:", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("tx_settype:"))
async def tx_edit_type_apply(callback: CallbackQuery):
    _, tx_id_raw, tx_type = callback.data.split(":")
    tx_id = int(tx_id_raw)
    if not db.update_transaction_type(callback.from_user.id, tx_id, tx_type):
        await callback.answer("Операция не найдена или тип некорректен", show_alert=True)
        return
    view = _tx_detail_view(callback.from_user.id, tx_id)
    if not view:
        await callback.answer("Операция не найдена (возможно, уже удалена)", show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(f"✅ Тип операции обновлён.\n\n{text}", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("tx_desc:"))
async def tx_edit_desc_start(callback: CallbackQuery, state: FSMContext):
    tx_id = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_tx_id=tx_id)
    await state.set_state(TransactionEdit.entering_description)
    await callback.message.edit_text("Новое описание (или '-' чтобы убрать):")
    await callback.answer()


@router.message(TransactionEdit.entering_description)
async def tx_edit_desc_apply(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    data = await state.get_data()
    tx_id = data["edit_tx_id"]
    new_desc = None if message.text.strip() == "-" else message.text.strip()
    db.update_transaction_description(message.from_user.id, tx_id, new_desc)
    await state.clear()
    view = _tx_detail_view(message.from_user.id, tx_id)
    if not view:
        await message.answer("Операция не найдена (возможно, уже удалена)")
        return
    text, keyboard = view
    await message.answer(f"✅ Описание обновлено.\n\n{text}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("tx_cat:"))
async def tx_edit_category_start(callback: CallbackQuery):
    tx_id = int(callback.data.split(":", 1)[1])
    keyboard = _with_back_button(
        categories_keyboard(callback.from_user.id, f"tx_setcat:{tx_id}"),
        back_callback=f"tx_open:{tx_id}",
    )
    await callback.message.edit_text("Выбери новую категорию:", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("tx_setcat:"))
async def tx_edit_category_apply(callback: CallbackQuery):
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
        await callback.answer("Операция не найдена (возможно, уже удалена)", show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(f"✅ Категория обновлена (запомнил на будущее).\n\n{text}", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("tx_pay:"))
async def tx_edit_payment_start(callback: CallbackQuery):
    tx_id = int(callback.data.split(":", 1)[1])
    keyboard = _with_back_button(
        payments_keyboard(callback.from_user.id, f"tx_setpay:{tx_id}"),
        back_callback=f"tx_open:{tx_id}",
    )
    await callback.message.edit_text("Выбери новый способ оплаты:", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("tx_setpay:"))
async def tx_edit_payment_apply(callback: CallbackQuery):
    _, tx_id, pm_id = callback.data.split(":")
    db.update_transaction_payment_method(callback.from_user.id, int(tx_id), int(pm_id))
    view = _tx_detail_view(callback.from_user.id, int(tx_id))
    if not view:
        await callback.answer("Операция не найдена (возможно, уже удалена)", show_alert=True)
        return
    text, keyboard = view
    await callback.message.edit_text(f"✅ Способ оплаты обновлён.\n\n{text}", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("tx_del:"))
async def tx_delete_confirm(callback: CallbackQuery):
    tx_id = int(callback.data.split(":", 1)[1])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"tx_del_yes:{tx_id}"),
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"tx_open:{tx_id}"),
            ]
        ]
    )
    await callback.message.edit_text("Удалить эту операцию без возможности восстановить?", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("tx_del_yes:"))
async def tx_delete_apply(callback: CallbackQuery):
    tx_id = int(callback.data.split(":", 1)[1])
    db.delete_transaction(callback.from_user.id, tx_id)
    date_from, date_to, page = _get_list_context(callback.from_user.id)
    text, keyboard = _recent_view(callback.from_user.id, date_from, date_to, page)
    await callback.message.edit_text("✅ Удалено.\n\n" + text, reply_markup=keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# Бюджеты по категориям: /budget
# ---------------------------------------------------------------------------

class BudgetEntry(StatesGroup):
    entering_limit = State()


def _budget_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    budgets = db.get_budgets(user_id)
    today = date.today()
    start = today.replace(day=1)
    lines = ["💰 <b>Бюджеты по категориям</b>", ""]
    buttons = []
    if not budgets:
        lines.append("Пока не задано ни одного лимита.")
    for b in budgets:
        spent = db.get_category_spent(user_id, b["category_id"], start.isoformat(), today.isoformat())
        pct = (spent / b["monthly_limit"] * 100) if b["monthly_limit"] > 0 else 0
        emoji = "🔴" if pct >= 100 else ("🟡" if pct >= 80 else "🟢")
        lines.append(f"{emoji} {hx(b['category_name'])}: {money(spent)} / {money(b['monthly_limit'])} ({pct:.0f}%)")
        buttons.append([
            InlineKeyboardButton(text=f"✏️ {b['category_name']}", callback_data=f"bud_pick:{b['category_id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"bud_del:{b['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text="➕ Установить лимит", callback_data="bud_add")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("budget"))
async def cmd_budget(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _budget_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "bud_add")
async def bud_add_start(callback: CallbackQuery):
    keyboard = categories_keyboard(callback.from_user.id, "bud_pick")
    await callback.message.edit_text("Для какой категории задать лимит?", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("bud_pick:"))
async def bud_pick_category(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split(":", 1)[1])
    await state.update_data(budget_cat_id=cat_id)
    await state.set_state(BudgetEntry.entering_limit)
    await callback.message.edit_text("Введи месячный лимит для этой категории (число, без пробелов):")
    await callback.answer()


@router.message(BudgetEntry.entering_limit)
async def bud_enter_limit(message: Message, state: FSMContext):
    limit = parse_positive_amount(message.text)
    if limit is None:
        await message.answer("Пришли число больше нуля, например: 100000")
        return
    data = await state.get_data()
    db.set_budget(message.from_user.id, data["budget_cat_id"], limit)
    await state.clear()
    text, keyboard = _budget_view(message.from_user.id)
    await message.answer(f"✅ Лимит установлен.\n\n{text}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("bud_del:"))
async def bud_delete(callback: CallbackQuery):
    budget_id = int(callback.data.split(":", 1)[1])
    db.delete_budget(callback.from_user.id, budget_id)
    text, keyboard = _budget_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer("Удалено")


# ---------------------------------------------------------------------------
# Накопительные цели: /goals
# ---------------------------------------------------------------------------

class GoalEntry(StatesGroup):
    entering_name = State()
    entering_target = State()


class GoalContribute(StatesGroup):
    entering_amount = State()


def _progress_bar(pct: float) -> str:
    filled = round(min(pct, 100) / 10)
    return "🟩" * filled + "⬜" * (10 - filled)


def _goals_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    goals = db.get_goals(user_id)
    lines = ["🎯 <b>Накопительные цели</b>", ""]
    buttons = []
    if not goals:
        lines.append("Пока нет ни одной цели.")
    for g in goals:
        pct = (g["current_amount"] / g["target_amount"] * 100) if g["target_amount"] > 0 else 0
        lines.append(
            f"<b>{hx(g['name'])}</b>: {money(g['current_amount'])} / {money(g['target_amount'])}\n"
            f"{_progress_bar(pct)} {pct:.0f}%"
        )
        buttons.append([
            InlineKeyboardButton(text=f"➕ Пополнить «{g['name']}»", callback_data=f"goal_add:{g['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"goal_del:{g['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text="➕ Новая цель", callback_data="goal_new")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("goals"))
async def cmd_goals(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _goals_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "goal_new")
async def goal_new_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GoalEntry.entering_name)
    await callback.message.edit_text("Название цели (например: Отпуск):")
    await callback.answer()


@router.message(GoalEntry.entering_name)
async def goal_enter_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    await state.update_data(goal_name=message.text.strip())
    await state.set_state(GoalEntry.entering_target)
    await message.answer("Сколько нужно накопить (число)?")


@router.message(GoalEntry.entering_target)
async def goal_enter_target(message: Message, state: FSMContext):
    target = parse_positive_amount(message.text)
    if target is None:
        await message.answer("Пришли число больше нуля, например: 300000")
        return
    data = await state.get_data()
    db.create_goal(message.from_user.id, data["goal_name"], target)
    await state.clear()
    text, keyboard = _goals_view(message.from_user.id)
    await message.answer(f"✅ Цель создана.\n\n{text}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("goal_add:"))
async def goal_contribute_start(callback: CallbackQuery, state: FSMContext):
    goal_id = int(callback.data.split(":", 1)[1])
    await state.update_data(goal_id=goal_id)
    await state.set_state(GoalContribute.entering_amount)
    await callback.message.edit_text("На сколько пополнить цель?")
    await callback.answer()


@router.message(GoalContribute.entering_amount)
async def goal_contribute_apply(message: Message, state: FSMContext):
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer("Пришли число больше нуля.")
        return
    data = await state.get_data()
    db.contribute_to_goal(message.from_user.id, data["goal_id"], amount)
    await state.clear()
    goal = db.get_goal_by_id(message.from_user.id, data["goal_id"])
    text, keyboard = _goals_view(message.from_user.id)
    prefix = "🎉 Цель достигнута!\n\n" if goal and goal["current_amount"] >= goal["target_amount"] else "✅ Пополнено.\n\n"
    await message.answer(prefix + text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("goal_del:"))
async def goal_delete_confirm(callback: CallbackQuery):
    goal_id = int(callback.data.split(":", 1)[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"goal_del_yes:{goal_id}"),
        InlineKeyboardButton(text="◀️ Отмена", callback_data="goal_del_no"),
    ]])
    await callback.message.edit_text("Удалить эту цель вместе с накопленным прогрессом?", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("goal_del_yes:"))
async def goal_delete_apply(callback: CallbackQuery):
    goal_id = int(callback.data.split(":", 1)[1])
    db.delete_goal(callback.from_user.id, goal_id)
    text, keyboard = _goals_view(callback.from_user.id)
    await callback.message.edit_text("✅ Удалено.\n\n" + text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "goal_del_no")
async def goal_delete_cancel(callback: CallbackQuery):
    text, keyboard = _goals_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# Повторяющиеся платежи: /recurring
# ---------------------------------------------------------------------------

class RecurringEntry(StatesGroup):
    choosing_type = State()
    entering_amount = State()
    choosing_category = State()
    choosing_payment = State()
    entering_day = State()
    entering_description = State()


def _recurring_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    items = db.get_recurring_list(user_id)
    lines = ["🔁 <b>Повторяющиеся платежи</b>", ""]
    buttons = []
    if not items:
        lines.append("Пока не добавлено ни одного.")
    for r in items:
        status = "✅" if r["active"] else "⏸"
        emoji = "💸" if r["type"] == "expense" else "💰"
        label = hx(r["description"] or r["category_name"]) or "без названия"
        lines.append(f"{status} {emoji} {money(r['amount'])} — {label}, {r['day_of_month']} числа каждый месяц")
        buttons.append([
            InlineKeyboardButton(text="⏸/▶️ Вкл/выкл", callback_data=f"rec_toggle:{r['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"rec_del:{r['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="rec_add")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("recurring"))
async def cmd_recurring(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _recurring_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "rec_add")
async def rec_add_start(callback: CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💸 Расход", callback_data="rec_type:expense"),
        InlineKeyboardButton(text="💰 Доход", callback_data="rec_type:income"),
    ]])
    await callback.message.edit_text("Что повторяем?", reply_markup=keyboard)
    await state.set_state(RecurringEntry.choosing_type)
    await callback.answer()


@router.callback_query(RecurringEntry.choosing_type, F.data.startswith("rec_type:"))
async def rec_choose_type(callback: CallbackQuery, state: FSMContext):
    tx_type = callback.data.split(":", 1)[1]
    await state.update_data(rec_type=tx_type)
    await state.set_state(RecurringEntry.entering_amount)
    await callback.message.edit_text("Сумма:")
    await callback.answer()


@router.message(RecurringEntry.entering_amount)
async def rec_enter_amount(message: Message, state: FSMContext):
    amount = parse_positive_amount(message.text)
    if amount is None:
        await message.answer("Пришли число больше нуля.")
        return
    await state.update_data(rec_amount=amount)
    await state.set_state(RecurringEntry.choosing_category)
    await message.answer("Категория:", reply_markup=categories_keyboard(message.from_user.id, "rec_cat"))


@router.callback_query(RecurringEntry.choosing_category, F.data.startswith("rec_cat:"))
async def rec_choose_category(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split(":", 1)[1])
    await state.update_data(rec_cat_id=cat_id)
    await state.set_state(RecurringEntry.choosing_payment)
    await callback.message.edit_text(
        "Способ оплаты:", reply_markup=payments_keyboard(callback.from_user.id, "rec_pay")
    )
    await callback.answer()


@router.callback_query(RecurringEntry.choosing_payment, F.data.startswith("rec_pay:"))
async def rec_choose_payment(callback: CallbackQuery, state: FSMContext):
    pm_id = int(callback.data.split(":", 1)[1])
    await state.update_data(rec_pay_id=pm_id)
    await state.set_state(RecurringEntry.entering_day)
    await callback.message.edit_text("Какого числа каждый месяц (1-28)?")
    await callback.answer()


@router.message(RecurringEntry.entering_day)
async def rec_enter_day(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    try:
        day = int(message.text.strip())
        if not (1 <= day <= 28):
            raise ValueError
    except ValueError:
        await message.answer("Пришли число от 1 до 28 (чтобы работало для всех месяцев).")
        return
    await state.update_data(rec_day=day)
    await state.set_state(RecurringEntry.entering_description)
    await message.answer("Название платежа (например: Аренда квартиры):")


@router.message(RecurringEntry.entering_description)
async def rec_enter_description(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(TEXT_HINT)
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
    await message.answer(f"✅ Повторяющийся платёж добавлен.\n\n{text}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("rec_toggle:"))
async def rec_toggle(callback: CallbackQuery):
    rec_id = int(callback.data.split(":", 1)[1])
    db.toggle_recurring_active(callback.from_user.id, rec_id)
    text, keyboard = _recurring_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("rec_del:"))
async def rec_delete(callback: CallbackQuery):
    rec_id = int(callback.data.split(":", 1)[1])
    db.delete_recurring(callback.from_user.id, rec_id)
    text, keyboard = _recurring_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer("Удалено")


async def _recurring_scheduler(bot: Bot):
    """Раз в несколько часов проверяет все активные повторяющиеся платежи и
    создаёт транзакцию, если наступил день месяца и в этом месяце ещё не срабатывал."""
    while True:
        try:
            today = date.today()
            for r in db.get_all_active_recurring():
                try:
                    created = db.apply_due_recurring(r["id"], today.isoformat())
                    if not created:
                        continue
                    emoji = "💰" if r["type"] == "income" else "💸"
                    await bot.send_message(
                        r["user_id"],
                        f"🔁 Автоматически добавлено: {emoji} {money(r['amount'])} — {hx(r['description']) or ''}",
                    )
                except Exception:
                    logger.exception(
                        "Не удалось обработать повторяющийся платёж %s", r["id"]
                    )
        except Exception:
            logger.exception("Сбой итерации планировщика повторяющихся платежей")
        await asyncio.sleep(6 * 3600)


# ---------------------------------------------------------------------------
# Аналитика по категории: /category_stats
# ---------------------------------------------------------------------------

def _catstat_period_keyboard(cat_id: int) -> InlineKeyboardMarkup:
    labels = [("Сегодня", "day"), ("Неделя", "week"), ("Месяц", "month"), ("3 месяца", "3months"), ("Год", "year")]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=f"catstat_period:{cat_id}:{period}")]
        for text, period in labels
    ])


@router.message(Command("category_stats"))
async def cmd_category_stats(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    keyboard = categories_keyboard(message.from_user.id, "catstat_pick")
    await message.answer("По какой категории посмотреть статистику?", reply_markup=keyboard)


@router.callback_query(F.data.startswith("catstat_pick:"))
async def catstat_pick_category(callback: CallbackQuery):
    cat_id = int(callback.data.split(":", 1)[1])
    await callback.message.edit_text("За какой период?", reply_markup=_catstat_period_keyboard(cat_id))
    await callback.answer()


@router.callback_query(F.data.startswith("catstat_period:"))
async def catstat_show(callback: CallbackQuery):
    _, cat_id_str, period = callback.data.split(":")
    cat_id = int(cat_id_str)
    user_id = callback.from_user.id

    date_from, date_to, label = _period_bounds(period)
    spent = db.get_category_spent(user_id, cat_id, date_from, date_to)
    total = db.get_total_expense(user_id, date_from, date_to)
    pct_of_total = (spent / total * 100) if total > 0 else 0

    prev_from, prev_to = _previous_period_bounds(date_from, date_to)
    prev_spent = db.get_category_spent(user_id, cat_id, prev_from, prev_to)

    cat_name = hx(db.get_category_name(user_id, cat_id)) or "?"
    text = (
        f"🔍 <b>{cat_name}</b> — {label}\n"
        f"Период: {date_from} — {date_to}\n\n"
        f"Потрачено: {money(spent)}\n"
        f"Доля от всех расходов за период: {pct_of_total:.0f}%\n"
        f"К прошлому периоду: {_pct_change(spent, prev_spent)} (было {money(prev_spent)})"
    )
    await callback.message.edit_text(text)
    await callback.answer()


# ---------------------------------------------------------------------------
# Экспорт операций: /export
# ---------------------------------------------------------------------------

class ExportState(StatesGroup):
    choosing_format = State()


@router.message(Command("export"))
async def cmd_export(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Месяц", callback_data="exp_period:month"),
         InlineKeyboardButton(text="3 месяца", callback_data="exp_period:3months")],
        [InlineKeyboardButton(text="Год", callback_data="exp_period:year"),
         InlineKeyboardButton(text="Всё время", callback_data="exp_period:all")],
    ])
    await message.answer("За какой период выгрузить операции?", reply_markup=keyboard)


@router.callback_query(F.data.startswith("exp_period:"))
async def export_choose_format(callback: CallbackQuery, state: FSMContext):
    period = callback.data.split(":", 1)[1]
    await state.update_data(export_period=period)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="CSV", callback_data="exp_format:csv"),
        InlineKeyboardButton(text="Excel", callback_data="exp_format:xlsx"),
    ]])
    await callback.message.edit_text("В каком формате?", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("exp_format:"))
async def export_generate(callback: CallbackQuery, state: FSMContext):
    fmt = callback.data.split(":", 1)[1]
    data = await state.get_data()
    period = data.get("export_period", "month")
    user_id = callback.from_user.id

    if period == "all":
        date_from, date_to = "2000-01-01", date.today().isoformat()
    else:
        date_from, date_to, _ = _period_bounds(period)

    rows = [dict(r) for r in db.get_transactions(user_id, date_from, date_to)]
    await state.clear()

    if not rows:
        await callback.message.edit_text("За этот период операций нет - нечего выгружать.")
        await callback.answer()
        return

    if fmt == "csv":
        buf = await asyncio.to_thread(export.export_csv, rows)
        filename = f"operations_{date_from}_{date_to}.csv"
    else:
        # openpyxl на выгрузке за год - это секунды CPU, держать на них loop нельзя
        buf = await asyncio.to_thread(export.export_xlsx, rows)
        filename = f"operations_{date_from}_{date_to}.xlsx"

    await callback.message.edit_text(f"Готово, {len(rows)} операций 👇")
    await callback.message.answer_document(BufferedInputFile(buf.read(), filename=filename))
    await callback.answer()


# ---------------------------------------------------------------------------
# Язык интерфейса: /language
# ---------------------------------------------------------------------------

@router.message(Command("language"))
async def cmd_language(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"lang:{code}")]
        for code, name in LANGUAGES.items()
    ])
    await message.answer(t("language_prompt", db.get_user_language(message.from_user.id)), reply_markup=keyboard)


@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery):
    lang = callback.data.split(":", 1)[1]
    db.ensure_user(callback.from_user.id, callback.from_user.username)
    db.set_user_language(callback.from_user.id, lang)
    await callback.message.edit_text(t("language_set", lang))
    await callback.answer()


# ---------------------------------------------------------------------------
# Автосводка + AI-инсайт по расписанию: /digest
# ---------------------------------------------------------------------------

DIGEST_FREQ_LABELS = {"off": "digest_off", "day": "digest_day", "week": "digest_week",
                       "month": "digest_month", "year": "digest_year"}


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


def _digest_due(freq: str, last_sent: str | None, today: date) -> bool:
    if freq == "off":
        return False
    if not last_sent:
        return True
    last = date.fromisoformat(last_sent)
    if freq == "day":
        return last != today
    if freq == "week":
        return (today - last).days >= 7
    if freq == "month":
        return (last.year, last.month) != (today.year, today.month)
    if freq == "year":
        return last.year != today.year
    return False


async def _digest_scheduler(bot: Bot):
    """Раз в несколько часов проверяет, кому пора прислать автосводку со
    статистикой за месяц и коротким AI-наблюдением."""
    while True:
        try:
            today = date.today()
            for row in db.get_users_for_digest():
                try:
                    if not _digest_due(row["digest_frequency"], row["digest_last_sent"], today):
                        continue
                    user_id = row["user_id"]
                    lang = db.get_user_language(user_id)
                    freq = row["digest_frequency"]
                    period = freq if freq in ("day", "week", "month", "year") else "month"
                    date_from, date_to, label = _period_bounds(period)
                    total_expense = db.get_total_expense(user_id, date_from, date_to)
                    categories_rows = [dict(r) for r in db.get_transactions(user_id, date_from, date_to) if r["type"] == "expense"]
                    cat_totals: dict[str, float] = defaultdict(float)
                    for r in categories_rows:
                        cat_totals[r["category_name"] or "Без категории"] += r["amount"]
                    top = sorted(cat_totals.items(), key=lambda kv: kv[1], reverse=True)[:3]

                    text = f"🔔 <b>{t('stats_title', lang, label=label)}</b>\n{t('expenses', lang)}: {money(total_expense)}"
                    if top:
                        text += "\n\n" + "\n".join(f"• {hx(name)}: {money(amount)}" for name, amount in top)

                    insight = await asyncio.to_thread(
                        generate_insight_text,
                        {"total_expense": total_expense, "top_categories": top},
                        lang,
                    )
                    if insight:
                        text += f"\n\n💡 {hx(insight)}"

                    await bot.send_message(user_id, text)
                    db.mark_digest_sent(user_id, today.isoformat())
                except Exception:
                    logger.exception("Не удалось отправить дайджест пользователю %s", row["user_id"])
        except Exception:
            logger.exception("Сбой итерации планировщика дайджестов")
        await asyncio.sleep(6 * 3600)


# ---------------------------------------------------------------------------
# Единые настройки: /settings
# ---------------------------------------------------------------------------

def _settings_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    s = db.get_settings(user_id)
    lang = s["language"]
    lang_name = LANGUAGES.get(lang, lang)
    digest_label = t(DIGEST_FREQ_LABELS.get(s["digest_frequency"], "digest_off"), lang)
    idle_on = bool(s["idle_reminder_enabled"])
    backup_on = bool(s["backup_enabled"])
    bank_on = bool(s["bank_import_enabled"])

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🌐 Язык: {lang_name}\n"
        f"🔔 Автосводка: {digest_label}\n"
        f"📉 Напоминание о простое: {'включено' if idle_on else 'выключено'}\n"
        f"📦 Еженедельный автобэкап: {'включён' if backup_on else 'выключен'}\n"
        f"🏦 Импорт из банковских уведомлений: {'включён' if bank_on else 'выключен'}\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Сменить язык", callback_data="settings_lang")],
        [InlineKeyboardButton(text="🔔 Частота автосводки", callback_data="settings_digest")],
        [InlineKeyboardButton(
            text=f"📉 Напоминание: {'выключить' if idle_on else 'включить'}",
            callback_data="settings_toggle_idle",
        )],
        [InlineKeyboardButton(
            text=f"📦 Автобэкап: {'выключить' if backup_on else 'включить'}",
            callback_data="settings_toggle_backup",
        )],
        [InlineKeyboardButton(
            text=f"🏦 Импорт из банков: {'выключить' if bank_on else 'включить'}",
            callback_data="settings_toggle_bank",
        )],
        [InlineKeyboardButton(text="📦 Скачать бэкап сейчас", callback_data="settings_backup_now")],
        [InlineKeyboardButton(text="📥 Восстановить из файла", callback_data="settings_restore_start")],
        [InlineKeyboardButton(text="⚠️ Удалить все мои данные", callback_data="danger_start")],
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
    note = (
        "\n\n📦 Раз в неделю я буду присылать JSON-бэкап в этот чат."
        if new_state else ""
    )
    await callback.message.edit_text(text + note, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "settings_toggle_bank")
async def settings_toggle_bank(callback: CallbackQuery):
    s = db.get_settings(callback.from_user.id)
    new_state = not s["bank_import_enabled"]
    db.set_bank_import_enabled(callback.from_user.id, new_state)
    text, keyboard = _settings_view(callback.from_user.id)
    note = (
        "\n\n📨 Теперь просто перешли мне уведомление о платеже из приложения "
        "банка - я попробую распознать сумму и магазин."
        if new_state else ""
    )
    await callback.message.edit_text(text + note, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "settings_backup_now")
async def settings_backup_now(callback: CallbackQuery):
    buf = await asyncio.to_thread(backup.build_backup, callback.from_user.id)
    payload = buf.read()
    if len(payload) > 45 * 1024 * 1024:
        await callback.answer("Бэкап слишком большой для Telegram, скачай с сервера", show_alert=True)
        return
    try:
        await callback.message.answer_document(
            BufferedInputFile(payload, filename=f"backup_{date.today().isoformat()}.json")
        )
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
        await callback.message.answer_document(
            BufferedInputFile(payload, filename=f"backup_{date.today().isoformat()}.json")
        )
    except Exception:
        logger.exception("Не удалось отправить бэкап")
        await callback.answer("Не удалось отправить файл", show_alert=True)
        return
    db.mark_backup_sent(callback.from_user.id, date.today().isoformat())
    await callback.answer("Бэкап отправлен")


# ---------------------------------------------------------------------------
# Восстановление из JSON-бэкапа прямо в чате: без этого пользователь без
# доступа к серверу не мог восстановиться после потери телефона/переустановки.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "settings_restore_start")
async def settings_restore_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RestoreEntry.awaiting_file)
    await callback.message.answer(
        "📥 Пришли файл бэкапа (.json), который я присылал раньше.\n\n"
        "⚠️ Все текущие операции, чеки, повторы, бюджеты и цели будут "
        "<b>полностью заменены</b> содержимым файла. Отменить это будет нельзя.\n\n"
        "/cancel — отмена."
    )
    await callback.answer()


@router.message(RestoreEntry.awaiting_file, F.document)
async def restore_receive_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if doc.file_size and doc.file_size > MAX_RESTORE_BYTES:
        await message.answer("Файл слишком большой (максимум 20 МБ).")
        return
    file = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    raw = buf.getvalue()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await message.answer("❌ Это не похоже на JSON-бэкап. Пришли файл без изменений или /cancel.")
        return
    if not isinstance(data, dict):
        await message.answer("❌ Некорректный формат бэкапа. Пришли файл без изменений или /cancel.")
        return

    draft_id = f"{message.from_user.id}_{uuid.uuid4().hex[:10]}"
    db.save_state(RESTORE_SCOPE, draft_id, data, user_id=message.from_user.id)
    await state.clear()

    counts = (
        f"🧾 Чеков: {len(data.get('receipts') or [])}\n"
        f"💰 Операций: {len(data.get('transactions') or [])}\n"
        f"🔁 Повторов: {len(data.get('recurring') or [])}\n"
        f"🎯 Целей: {len(data.get('goals') or [])}\n"
        f"🏷 Категорий: {len(data.get('categories') or [])}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Заменить данные", callback_data=f"restore_go:{draft_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"restore_no:{draft_id}"),
    ]])
    await message.answer(
        f"Нашёл в файле:\n{counts}\n\n⚠️ Все текущие данные будут заменены этим. Подтверждаешь?",
        reply_markup=keyboard,
    )


@router.message(RestoreEntry.awaiting_file)
async def restore_awaiting_wrong_content(message: Message):
    await message.answer("Жду файл бэкапа (.json). /cancel — отмена.")


@router.callback_query(F.data.startswith("restore_go:"))
async def restore_confirm(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    if not draft_id.startswith(f"{user_id}_"):
        await callback.answer("Это не твой черновик.", show_alert=True)
        return
    data = db.load_state(RESTORE_SCOPE, draft_id)
    if data is None:
        await callback.answer("Черновик устарел, пришли файл ещё раз.", show_alert=True)
        return
    try:
        result = await asyncio.to_thread(db.restore_user_backup, user_id, data)
    except Exception:
        logger.exception("Не удалось восстановить бэкап")
        await callback.message.edit_text(
            "❌ Не получилось восстановить бэкап - файл повреждён или несовместим. "
            "Твои текущие данные не тронуты."
        )
        await callback.answer()
        return
    db.delete_state(RESTORE_SCOPE, draft_id)
    await callback.message.edit_text(
        "✅ Данные восстановлены:\n"
        f"💰 Операций: {result.get('transactions', 0)}\n"
        f"🧾 Чеков: {result.get('receipts', 0)}\n"
        f"🔁 Повторов: {result.get('recurring', 0)}\n"
        f"🎯 Целей: {result.get('goals', 0)}"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("restore_no:"))
async def restore_cancel(callback: CallbackQuery):
    draft_id = callback.data.split(":", 1)[1]
    if not draft_id.startswith(f"{callback.from_user.id}_"):
        await callback.answer("Это не твой черновик.", show_alert=True)
        return
    db.delete_state(RESTORE_SCOPE, draft_id)
    await callback.message.edit_text("Восстановление отменено, данные не тронуты.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Опасная зона: полное удаление всех данных (двойное подтверждение)
# ---------------------------------------------------------------------------

class DangerZone(StatesGroup):
    entering_phrase = State()


@router.callback_query(F.data == "danger_start")
async def danger_start(callback: CallbackQuery):
    user_id = callback.from_user.id
    count = db.count_user_data(user_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚠️ Да, я понимаю последствия", callback_data="danger_confirm1"),
        InlineKeyboardButton(text="◀️ Отмена", callback_data="danger_cancel"),
    ]])
    await callback.message.edit_text(
        f"⚠️ <b>Это удалит НАВСЕГДА:</b>\n"
        f"— {count} операций\n"
        f"— все категории, способы оплаты, бюджеты и цели\n\n"
        f"Восстановить будет нельзя (если не делал /settings → бэкап заранее).\n\n"
        f"Точно продолжить?",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data == "danger_confirm1")
async def danger_confirm1(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DangerZone.entering_phrase)
    await callback.message.edit_text(
        "Последний шаг. Чтобы подтвердить, напиши сообщением ровно:\n\n<code>УДАЛИТЬ ВСЁ</code>"
    )
    await callback.answer()


@router.message(DangerZone.entering_phrase)
async def danger_confirm2(message: Message, state: FSMContext):
    await state.clear()
    if not message.text or message.text.strip() != "УДАЛИТЬ ВСЁ":
        await message.answer("Фраза не совпала - ничего не удалено. Если передумал, это и хорошо.")
        return
    db.delete_all_user_data(message.from_user.id)
    await message.answer("Готово, все данные удалены. /start - чтобы начать заново.")


@router.callback_query(F.data == "danger_cancel")
async def danger_cancel(callback: CallbackQuery):
    text, keyboard = _settings_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer("Отменено")


# ---------------------------------------------------------------------------
# Импорт: файлы выписок (обработчик форварда банковских уведомлений теперь
# стоит выше, перед quick_add - см. handle_forwarded_bank_notification)
# ---------------------------------------------------------------------------

@router.message(StateFilter(None), F.document, F.document.file_name.regexp(r"\.(csv|xlsx)$"))
async def handle_import_file(message: Message, bot: Bot):
    user_id = message.from_user.id
    db.ensure_user(user_id, message.from_user.username)

    file_size = message.document.file_size or 0
    if file_size > MAX_IMPORT_BYTES:
        await message.answer("⚠️ Файл слишком большой. Максимум 2 МБ.")
        return

    file = await bot.get_file(message.document.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)

    try:
        rows = await asyncio.to_thread(
            export.parse_import_file, buf.getvalue(), message.document.file_name
        )
    except ValueError:
        await message.answer("⚠️ Слишком много строк в файле (лимит 5000).")
        return
    except Exception:
        logger.exception("Ошибка разбора импортируемого файла")
        await message.answer("⚠️ Не смог прочитать файл. Поддерживаются CSV и Excel с колонками дата/сумма/описание.")
        return

    if not rows:
        await message.answer(
            "⚠️ Не нашёл в файле колонку с суммой. Убедись, что в первой строке "
            "есть заголовок вроде «Сумма» / «Amount»."
        )
        return

    import_id = f"{user_id}_{message.message_id}"
    db.save_state(IMPORT_SCOPE, import_id, rows, user_id=user_id)
    expense_total = sum(r["amount"] for r in rows if r["type"] == "expense")
    income_total = sum(r["amount"] for r in rows if r["type"] == "income")
    preview_lines = [
        f"• {hx(r['date']) or '—'} — {'+' if r['type'] == 'income' else '−'}{money(r['amount'])}"
        f" — {hx(r['description']) or 'без описания'}"
        for r in rows[:10]
    ]
    more = f"\n… и ещё {len(rows) - 10}" if len(rows) > 10 else ""
    header = f"📤 Нашёл {len(rows)} операций:\n💸 Расходы: {money(expense_total)}"
    if income_total:
        header += f"\n💰 Доходы: {money(income_total)}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Импортировать всё ({len(rows)})", callback_data=f"imp_ok:{message.message_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"imp_no:{message.message_id}"),
    ]])
    await message.answer(
        header + "\n\n" + "\n".join(preview_lines) + more + "\n\nИмпортировать?",
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
        await callback.message.edit_text("Черновик устарел")
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
        date.today().isoformat(),
    )
    if count is None:
        await callback.message.edit_text("Черновик устарел")
        return
    await callback.message.edit_text(f"✅ Импортировано {count} операций.")


@router.callback_query(F.data.startswith("imp_no:"))
async def import_cancel(callback: CallbackQuery):
    user_id = callback.from_user.id
    message_id = callback.data.split(":", 1)[1]
    db.delete_state(IMPORT_SCOPE, f"{user_id}_{message_id}", user_id=user_id)
    await callback.message.edit_text("❌ Импорт отменён.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Напоминание о простое и автобэкап - фоновые задачи
# ---------------------------------------------------------------------------

async def _idle_reminder_scheduler(bot: Bot):
    while True:
        try:
            purged = db.purge_stale_state()
            if purged:
                logger.info("Периодическая очистка app_state: %s", purged)
            today = date.today()
            for row in db.get_users_for_idle_check():
                try:
                    last_activity = date.fromisoformat(row["last_activity_date"]) if row["last_activity_date"] else None
                    if last_activity is None or (today - last_activity).days < 3:
                        continue
                    last_sent = date.fromisoformat(row["idle_reminder_last_sent"]) if row["idle_reminder_last_sent"] else None
                    if last_sent and (today - last_sent).days < 3:
                        continue
                    await bot.send_message(
                        row["user_id"],
                        "👋 Давно не было новых записей. Всё в порядке? Если что - "
                        "просто напиши сумму и что купил, займёт секунду."
                    )
                    db.mark_idle_reminder_sent(row["user_id"], today.isoformat())
                except Exception:
                    logger.exception("Не удалось отправить напоминание о простое пользователю %s", row["user_id"])
        except Exception:
            logger.exception("Сбой итерации планировщика напоминаний о простое")
        await asyncio.sleep(12 * 3600)


async def _backup_scheduler(bot: Bot):
    while True:
        try:
            today = date.today()
            for row in db.get_users_for_backup():
                try:
                    last_sent = date.fromisoformat(row["backup_last_sent"]) if row["backup_last_sent"] else None
                    if last_sent and (today - last_sent).days < 7:
                        continue
                    user_id = row["user_id"]
                    if db.count_user_data(user_id) == 0:
                        continue
                    buf = await asyncio.to_thread(backup.build_backup, user_id)
                    await bot.send_document(
                        user_id,
                        BufferedInputFile(buf.read(), filename=f"backup_{today.isoformat()}.json"),
                        caption="📦 Еженедельный автобэкап твоих данных",
                    )
                    db.mark_backup_sent(user_id, today.isoformat())
                except Exception:
                    logger.exception("Не удалось отправить автобэкап пользователю %s", row["user_id"])
        except Exception:
            logger.exception("Сбой итерации планировщика бэкапов")
        await asyncio.sleep(24 * 3600)


# ---------------------------------------------------------------------------
# Обратная связь: /feedback
# ---------------------------------------------------------------------------

class FeedbackEntry(StatesGroup):
    entering_text = State()


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    if not ADMIN_USER_ID:
        await message.answer("Обратная связь пока не настроена разработчиком.")
        return
    await state.set_state(FeedbackEntry.entering_text)
    await message.answer(
        "Напиши сообщение - баг, идею, что угодно. Я перешлю разработчику напрямую."
    )


@router.message(FeedbackEntry.entering_text)
async def feedback_send(message: Message, state: FSMContext, bot: Bot):
    if not message.text:
        await message.answer(TEXT_HINT)
        return
    await state.clear()
    user = message.from_user
    username = f"@{user.username}" if user.username else user.full_name
    try:
        await bot.send_message(
            ADMIN_USER_ID,
            f"📩 <b>Фидбэк от {hx(username)}</b> (id {user.id}):\n\n{hx(message.text)}",
        )
        await message.answer("✅ Отправлено, спасибо!")
    except Exception:
        logger.exception("Не удалось переслать фидбэк админу")
        await message.answer("⚠️ Не получилось отправить, попробуй ещё раз позже.")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Установи переменную окружения BOT_TOKEN "
            "с токеном, полученным у @BotFather."
        )

    db.init_db()
    purged = db.purge_stale_state()
    if purged:
        logger.info("Удалено брошенных черновиков и FSM-сессий: %s", purged)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=SQLiteStorage())
    dp.include_router(router)

    asyncio.create_task(_run_supervised(bot))
    await dp.start_polling(bot)


async def _supervised(name: str, factory, bot: Bot) -> None:
    while True:
        try:
            await factory(bot)
            logger.error("Фоновая задача %s завершилась, перезапускаю", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Фоновая задача %s упала, перезапускаю", name)
        await asyncio.sleep(30)


async def _run_supervised(bot: Bot) -> None:
    BACKGROUND_TASKS[:] = [
        asyncio.create_task(_supervised("recurring", _recurring_scheduler, bot), name="recurring"),
        asyncio.create_task(_supervised("digest", _digest_scheduler, bot), name="digest"),
        asyncio.create_task(_supervised("idle", _idle_reminder_scheduler, bot), name="idle"),
        asyncio.create_task(_supervised("backup", _backup_scheduler, bot), name="backup"),
    ]
    await asyncio.gather(*BACKGROUND_TASKS)


if __name__ == "__main__":
    asyncio.run(main())