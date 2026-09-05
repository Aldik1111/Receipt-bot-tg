import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import charts
import db
from formatting import hx, money
from i18n import format_date, t, weekday_short, month_short
from handlers.common import StatsCustomPeriod, text_hint
from keyboards.common import categories_keyboard, period_keyboard

router = Router(name="stats")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    lang = db.get_user_language(message.from_user.id)
    await message.answer(t("ask_period", lang), reply_markup=period_keyboard(lang))

def _previous_period_bounds(date_from: str, date_to: str) -> tuple[str, str]:
    """Предыдущий период той же длины, сразу перед текущим - для сравнения."""
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    length = (d_to - d_from).days + 1
    prev_to = d_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=length - 1)
    return prev_from.isoformat(), prev_to.isoformat()

def _pct_change(current: float, previous: float, lang: str = "ru") -> str:
    if previous <= 0:
        return t("pct_na", lang) if current <= 0 else t("pct_new", lang)
    change = (current - previous) / previous * 100
    arrow = "🔺" if change > 0 else ("🔻" if change < 0 else "▪️")
    return f"{arrow} {abs(change):.0f}%"

async def _budget_warning_text(user_id: int, category_id: int | None) -> str | None:
    ids = [category_id] if category_id is not None else []
    return _budget_warnings_text(user_id, ids)

def _category_budget_warning(user_id: int, category_id: int, date_from: str, date_to: str) -> str | None:
    budget = db.get_budget_for_category(user_id, category_id)
    if not budget:
        return None
    spent = db.get_category_spent(user_id, category_id, date_from, date_to)
    return _format_budget_warning(
        hx(db.get_category_name(user_id, category_id)) or "?",
        spent,
        budget["monthly_limit"],
        overall=False,
        lang=db.get_user_language(user_id),
    )

def _overall_budget_warning(user_id: int, date_from: str, date_to: str) -> str | None:
    budget = db.get_overall_budget(user_id)
    if not budget:
        return None
    spent = db.get_total_expense(user_id, date_from, date_to)
    lang = db.get_user_language(user_id)
    return _format_budget_warning(
        t("budget_overall_name", lang),
        spent,
        budget["monthly_limit"],
        overall=True,
        lang=lang,
    )

def _format_budget_warning(
    name: str, spent: int, limit: int, *, overall: bool, lang: str = "ru"
) -> str | None:
    pct = (spent / limit * 100) if limit > 0 else 0
    if pct < 80:
        return None
    label = t("budget_overall_label", lang) if overall else t("budget_category_label", lang, name=name)
    icon = "⚠️" if pct >= 100 else "🟡"
    suffix = t("budget_exceeded", lang) if pct >= 100 else ""
    return t(
        "budget_warning",
        lang,
        icon=icon,
        label=label,
        suffix=suffix,
        spent=money(spent),
        limit=money(limit),
        pct=pct,
    )

def _budget_warnings_text(user_id: int, category_ids) -> str | None:
    """Одно сообщение: категориальные лимиты + общий бюджет, без двух отдельных спамов."""
    today = db.user_today(user_id)
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    lines: list[str] = []
    seen: set[int] = set()
    for category_id in category_ids or []:
        if category_id is None or category_id in seen:
            continue
        seen.add(category_id)
        warning = _category_budget_warning(user_id, category_id, start, end)
        if warning:
            lines.append(warning)
    overall = _overall_budget_warning(user_id, start, end)
    if overall:
        lines.append(overall)
    return "\n".join(lines) if lines else None

def _month_start(today: date, months_back: int = 0) -> date:
    month = today.month - months_back
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)

def _period_bounds(period: str, today: date, lang: str = "ru") -> tuple[str, str, str]:
    labels = {
        "day": t("period_label_day", lang),
        "week": t("period_label_week", lang),
        "month": t("period_label_month", lang),
        "3months": t("period_label_3months", lang),
        "year": t("period_label_year", lang),
    }
    if period == "day":
        return today.isoformat(), today.isoformat(), labels["day"]
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat(), labels["week"]
    if period == "month":
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat(), labels["month"]
    if period == "3months":
        start = _month_start(today, months_back=2)
        return start.isoformat(), today.isoformat(), labels["3months"]
    if period == "year":
        start = today.replace(month=1, day=1)
        return start.isoformat(), today.isoformat(), labels["year"]
    raise ValueError(period)

async def _send_stats(message: Message, user_id: int, date_from: str, date_to: str, label: str):
    lang = db.get_user_language(user_id)
    rows = db.get_transactions(user_id, date_from, date_to)
    expenses = [dict(r) for r in rows if r["type"] == "expense"]
    incomes = [dict(r) for r in rows if r["type"] == "income"]

    total_expense = sum(r["amount"] for r in expenses)
    total_income = sum(r["amount"] for r in incomes)
    reserved_period = db.get_goal_transfers_total(user_id, date_from, date_to)
    reserved_now = db.get_goals_reserved(user_id)

    prev_from, prev_to = _previous_period_bounds(date_from, date_to)
    prev_expense = db.get_total_expense(user_id, prev_from, prev_to)

    forecast_line = ""
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    today = db.user_today(user_id)
    if d_from <= today <= d_to and total_expense > 0:
        days_elapsed = (today - d_from).days + 1
        days_total = (d_to - d_from).days + 1
        if days_elapsed < days_total:
            forecast = total_expense * days_total // days_elapsed
            forecast_line = t("stats_forecast", lang, amount=money(forecast))

    transfer_line = ""
    if reserved_period or reserved_now:
        transfer_line = (
            t("stats_goals_period", lang, amount=money(reserved_period))
            + t("stats_goals_now", lang, amount=money(reserved_now))
        )
    summary = (
        f"{t('stats_title', lang, label=label)}\n"
        f"{t('stats_period', lang, start=format_date(date_from, lang), end=format_date(date_to, lang))}\n\n"
        f"{t('stats_expense_line', lang, amount=money(total_expense), change=_pct_change(total_expense, prev_expense, lang), vs=t('vs_prev', lang))}\n"
        f"{t('stats_income_line', lang, amount=money(total_income))}\n"
        f"{t('stats_balance_line', lang, amount=money(total_income - total_expense))}"
        f"{transfer_line}"
        f"{forecast_line}"
    )
    show_list_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=t("show_operations", lang), callback_data=f"recent_from:{date_from}:{date_to}"
    )]])
    await message.answer(summary, reply_markup=show_list_kb)

    if not expenses:
        await message.answer(t("no_expenses", lang))
        return

    buf = await asyncio.to_thread(
        charts.pie_chart_by_category,
        expenses,
        t("chart_expenses", lang, label=label),
        other_label=t("chart_other", lang),
        empty_label=t("uncategorized", lang),
        empty_text=t("chart_no_data", lang),
    )
    await message.answer_photo(BufferedInputFile(buf.read(), filename="by_category.png"))

    # Куда - топ магазинов/мест
    store_totals: dict[str, float] = defaultdict(float)
    for e in expenses:
        if e["store"]:
            store_totals[e["store"]] += e["amount"]
    if store_totals:
        top_stores = sorted(store_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
        lines = [t("top_stores", lang)]
        for name, amount in top_stores:
            lines.append(f"• {hx(name)} — {money(amount)}")
        await message.answer("\n".join(lines))

    item_totals: dict[str, tuple[int, int]] = {}
    for expense in expenses:
        name = (expense["description"] or "").strip()
        if not expense["receipt_id"] or not name:
            continue
        count, amount = item_totals.get(name, (0, 0))
        item_totals[name] = (count + 1, amount + int(expense["amount"]))
    if item_totals:
        top_items = sorted(
            item_totals.items(),
            key=lambda item: (item[1][0], item[1][1]),
            reverse=True,
        )[:5]
        lines = [t("top_items", lang)]
        for name, (count, amount) in top_items:
            lines.append(t("top_item_line", lang, name=hx(name), count=count, amount=money(amount)))
        await message.answer("\n".join(lines))

    # Способы оплаты
    pay_totals: dict[str, float] = defaultdict(float)
    for e in expenses:
        pay_totals[e["payment_name"] or t("unspecified", lang)] += e["amount"]
    if len(pay_totals) > 1 or t("unspecified", lang) not in pay_totals:
        lines = [t("by_payments", lang)]
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
                key = f"{d.isoformat()} {weekday_short(d, lang)}"
            elif span_days <= 120:
                iso = d.isocalendar()
                key = f"{iso[0]}-W{iso[1]:02d}"
            else:
                key = f"{d.year}-{d.month:02d} {month_short(d, lang)}"
            buckets[key] = buckets.get(key, 0) + e["amount"]
        if len(buckets) > 1:
            buf2 = await asyncio.to_thread(
                charts.bar_chart_by_period,
                buckets,
                t("chart_trend", lang, label=label),
                t("chart_axis", lang),
            )
            await message.answer_photo(BufferedInputFile(buf2.read(), filename="trend.png"))

@router.callback_query(F.data.startswith("stats:"))
async def show_stats(callback: CallbackQuery):
    period = callback.data.split(":", 1)[1]
    lang = db.get_user_language(callback.from_user.id)
    date_from, date_to, label = _period_bounds(period, db.user_today(callback.from_user.id), lang)
    await _send_stats(callback.message, callback.from_user.id, date_from, date_to, label)
    await callback.answer()

@router.callback_query(F.data == "stats_custom")
async def stats_custom_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(StatsCustomPeriod.entering_dates)
    await callback.message.edit_text(t("custom_period_prompt", db.get_user_language(callback.from_user.id)))
    await callback.answer()

@router.message(StatsCustomPeriod.entering_dates)
async def stats_custom_apply(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(text_hint(db.get_user_language(message.from_user.id)))
        return
    lang = db.get_user_language(message.from_user.id)
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer(t("custom_period_need_two", lang))
        return
    try:
        d_from = datetime.strptime(parts[0], "%d.%m.%Y").date()
        d_to = datetime.strptime(parts[1], "%d.%m.%Y").date()
    except ValueError:
        await message.answer(t("custom_period_bad", lang))
        return
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    await state.clear()
    await _send_stats(
        message,
        message.from_user.id,
        d_from.isoformat(),
        d_to.isoformat(),
        t("period_label_custom", lang),
    )

def _catstat_period_keyboard(cat_id: int, lang: str = "ru") -> InlineKeyboardMarkup:
    labels = [
        ("period_day", "day"),
        ("period_week", "week"),
        ("period_month", "month"),
        ("period_3months", "3months"),
        ("period_year", "year"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(key, lang), callback_data=f"catstat_period:{cat_id}:{period}")]
        for key, period in labels
    ])

@router.message(Command("category_stats"))
async def cmd_category_stats(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    lang = db.get_user_language(message.from_user.id)
    keyboard = categories_keyboard(message.from_user.id, "catstat_pick")
    await message.answer(t("category_stats_pick", lang), reply_markup=keyboard)

@router.callback_query(F.data.startswith("catstat_pick:"))
async def catstat_pick_category(callback: CallbackQuery):
    cat_id = int(callback.data.split(":", 1)[1])
    lang = db.get_user_language(callback.from_user.id)
    await callback.message.edit_text(t("ask_period", lang), reply_markup=_catstat_period_keyboard(cat_id, lang))
    await callback.answer()

@router.callback_query(F.data.startswith("catstat_period:"))
async def catstat_show(callback: CallbackQuery):
    _, cat_id_str, period = callback.data.split(":")
    cat_id = int(cat_id_str)
    user_id = callback.from_user.id

    lang = db.get_user_language(user_id)
    date_from, date_to, label = _period_bounds(period, db.user_today(user_id), lang)
    spent = db.get_category_spent(user_id, cat_id, date_from, date_to)
    total = db.get_total_expense(user_id, date_from, date_to)
    pct_of_total = (spent / total * 100) if total > 0 else 0

    prev_from, prev_to = _previous_period_bounds(date_from, date_to)
    prev_spent = db.get_category_spent(user_id, cat_id, prev_from, prev_to)

    cat_name = hx(db.get_category_name(user_id, cat_id)) or "?"
    text = t(
        "category_stats_body",
        lang,
        name=cat_name,
        label=label,
        start=format_date(date_from, lang),
        end=format_date(date_to, lang),
        spent=money(spent),
        pct=pct_of_total,
        change=_pct_change(spent, prev_spent, lang),
        prev=money(prev_spent),
    )
    await callback.message.edit_text(text)
    await callback.answer()
