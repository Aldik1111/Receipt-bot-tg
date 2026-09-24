"""
Бэкенд для Telegram Mini App. Отдельный процесс от bot.py (тот же db.py,
та же SQLite), слушает локально на 127.0.0.1:8000 - наружу его пускает
Caddy как reverse proxy по пути /api/* (см. Caddyfile в README).

Каждый запрос обязан нести initData в заголовке X-Telegram-Init-Data -
без валидной подписи данные не отдаются никому.
"""

import asyncio
import json
import logging
from datetime import date, timedelta

from aiohttp import web

import db
from categorizer import categorize  # noqa: F401 - не используется напрямую, но держим импорт рядом с db
from config import BOT_TOKEN, WEBAPP_HOST, WEBAPP_PORT
from formatting import money, parse_positive_amount
from i18n import PRIVACY_VERSION, format_date, format_money, miniapp_bundle, normalize_lang, t
from logutil import configure_logging
from webapp_auth import validate_init_data

configure_logging()
logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


async def _authenticate(request: web.Request) -> int | None:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        return None
    await asyncio.to_thread(db.ensure_user, user["id"], user.get("username"))
    return user["id"]


async def _write_or_403(user_id: int) -> web.Response | None:
    if not (await asyncio.to_thread(db.can_write_book, user_id)):
        return web.json_response({"error": "read-only"}, status=403)
    return None


def _parse_custom_dates(request: web.Request) -> tuple[str, str] | web.Response | None:
    date_from = request.query.get("date_from")
    date_to = request.query.get("date_to")
    if not date_from and not date_to:
        return None
    if bool(date_from) != bool(date_to):
        return web.json_response(
            {"error": "date_from and date_to must be provided together"}, status=400
        )
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except ValueError:
        return web.json_response({"error": "invalid date format"}, status=400)
    if start > end:
        return web.json_response(
            {"error": "date_from must not be after date_to"}, status=400
        )
    return date_from, date_to


def _period_bounds(period: str, today: date) -> tuple[str, str]:
    if period == "week":
        start = today - timedelta(days=today.weekday())
    elif period == "3months":
        month = today.month - 2
        year = today.year
        while month <= 0:
            month += 12
            year -= 1
        start = date(year, month, 1)
    elif period == "year":
        start = today.replace(month=1, day=1)
    else:  # month по умолчанию
        start = today.replace(day=1)
    return start.isoformat(), today.isoformat()


@routes.get("/health")
async def health_check(request: web.Request) -> web.Response:
    """Публичный сторож: только status + db. Имена планировщиков не отдаём
    наружу — они остаются в scripts/healthcheck.py --db-only."""
    try:
        await asyncio.to_thread(db.count_all_transactions, 0)  # лёгкий запрос, user_id=0 никогда не существует
        health = await asyncio.to_thread(db.scheduler_health)
        status = "ok" if health.get("schedulers_ok", True) else "degraded"
        return web.json_response({"status": status, "db": "ok"})
    except Exception:
        logger.exception("Health check: БД недоступна")
        return web.json_response({"status": "error", "db": "error"}, status=500)


@routes.get("/api/summary")
async def get_summary(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    period = request.query.get("period", "month")
    parsed = _parse_custom_dates(request)
    if isinstance(parsed, web.Response):
        return parsed
    if parsed:
        date_from, date_to = parsed
    else:
        date_from, date_to = _period_bounds(period, (await asyncio.to_thread(db.user_today, user_id)))
    totals = await asyncio.to_thread(db.get_summary_totals, user_id, date_from, date_to)
    expense, income = totals["expense"], totals["income"]
    lang = await asyncio.to_thread(db.get_user_language, user_id)

    return web.json_response({
        "date_from": date_from,
        "date_to": date_to,
        "timezone": (await asyncio.to_thread(db.get_user_timezone, user_id)),
        "expense": expense,
        "income": income,
        "balance": income - expense,
        "expense_formatted": format_money(expense, lang),
        "income_formatted": format_money(income, lang),
        "balance_formatted": format_money(income - expense, lang),
    })


@routes.get("/api/categories")
async def get_categories_breakdown(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    period = request.query.get("period", "month")
    parsed = _parse_custom_dates(request)
    if isinstance(parsed, web.Response):
        return parsed
    if parsed:
        date_from, date_to = parsed
    else:
        date_from, date_to = _period_bounds(period, (await asyncio.to_thread(db.user_today, user_id)))
    expenses = await asyncio.to_thread(db.get_category_totals, user_id, date_from, date_to)
    total = sum(int(r["amount"]) for r in expenses)
    lang = await asyncio.to_thread(db.get_user_language, user_id)

    totals: dict[str, int] = {}
    emojis: dict[str, str] = {}
    for r in expenses:
        name = r["category_name"] or t("uncategorized", lang)
        totals[name] = totals.get(name, 0) + int(r["amount"])
        emojis[name] = r["category_emoji"] or "🏷"

    items = [
        {"category": name, "emoji": emojis.get(name, "🏷"), "amount": amount,
         "pct": (amount / total * 100) if total else 0}
        for name, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return web.json_response({"items": items, "total": total})


@routes.get("/api/category_list")
async def get_category_list(request: web.Request) -> web.Response:
    """Лёгкий список категорий (для выпадающего фильтра на дашборде)."""
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    items = [
        {"id": c["id"], "name": c["name"], "emoji": c["emoji"]}
        for c in (await asyncio.to_thread(db.get_categories, user_id))
    ]
    return web.json_response({"items": items})


def _serialize_tx(row, lang: str) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "amount": int(row["amount"]),
        "amount_formatted": format_money(row["amount"], lang),
        "category": row["category_name"],
        "category_id": row["category_id"],
        "category_emoji": row["category_emoji"] or "🏷",
        "payment": row["payment_name"],
        "payment_method_id": row["payment_method_id"],
        "store": row["store"],
        "description": row["description"],
        "date": row["op_date"],
        "date_formatted": format_date(row["op_date"], lang),
        "time": row["op_time"],
    }


async def _parse_tx_payload(payload: dict, user_id: int) -> tuple[dict | None, web.Response | None]:
    if not isinstance(payload, dict):
        return None, web.json_response({"error": "invalid json"}, status=400)

    tx_type = payload.get("type")
    if tx_type not in ("expense", "income"):
        return None, web.json_response({"error": "invalid type"}, status=400)

    amount = parse_positive_amount(str(payload.get("amount", "")))
    if amount is None:
        return None, web.json_response({"error": "invalid amount"}, status=400)

    try:
        category_id = int(payload.get("category_id"))
    except (TypeError, ValueError):
        return None, web.json_response({"error": "invalid category"}, status=400)
    if (await asyncio.to_thread(db.get_category_name, user_id, category_id)) is None:
        return None, web.json_response({"error": "category not found"}, status=403)

    op_date = payload.get("date") or (await asyncio.to_thread(db.user_today, user_id)).isoformat()
    try:
        date.fromisoformat(str(op_date))
    except ValueError:
        return None, web.json_response({"error": "invalid date"}, status=400)

    payment_method_id = payload.get("payment_method_id")
    if payment_method_id in ("", None):
        payment_method_id = None
    else:
        try:
            payment_method_id = int(payment_method_id)
        except (TypeError, ValueError):
            return None, web.json_response({"error": "invalid payment"}, status=400)
        owned = {row["id"] for row in (await asyncio.to_thread(db.get_payment_methods, user_id))}
        if payment_method_id not in owned:
            return None, web.json_response({"error": "payment not found"}, status=403)

    description = payload.get("description")
    if description is not None:
        description = str(description).strip()[:500] or None
    store = payload.get("store")
    if store is not None:
        store = str(store).strip()[:128] or None

    return {
        "type": tx_type,
        "amount": amount,
        "category_id": category_id,
        "date": str(op_date),
        "description": description,
        "store": store,
        "payment_method_id": payment_method_id,
    }, None


@routes.get("/api/me")
async def get_me(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    lang = normalize_lang((await asyncio.to_thread(db.get_user_language, user_id)))
    return web.json_response({
        "language": lang,
        "timezone": (await asyncio.to_thread(db.get_user_timezone, user_id)),
        "today": (await asyncio.to_thread(db.user_today, user_id)).isoformat(),
        "onboarded": (await asyncio.to_thread(db.is_onboarded, user_id)),
        "privacy_accepted_version": (await asyncio.to_thread(db.get_privacy_accepted_version, user_id)),
        "privacy_current_version": PRIVACY_VERSION,
        "plan": (await asyncio.to_thread(db.user_plan_info, user_id))["plan"],
        "can_write": (await asyncio.to_thread(db.can_write_book, user_id)),
        "translations": miniapp_bundle(lang),
        "categories": [
            {"id": c["id"], "name": c["name"], "emoji": c["emoji"]}
            for c in (await asyncio.to_thread(db.get_categories, user_id))
        ],
        "payments": [
            {"id": p["id"], "name": p["name"]}
            for p in (await asyncio.to_thread(db.get_payment_methods, user_id))
        ],
    })


@routes.post("/api/transactions")
async def create_transaction(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "invalid json"}, status=400)
    parsed, error = await _parse_tx_payload(payload, user_id)
    if error is not None:
        return error
    if not (await asyncio.to_thread(db.can_write_book, user_id)):
        return web.json_response({"error": "read-only"}, status=403)
    now = await asyncio.to_thread(db.user_now, user_id)
    try:
        tx_id = await asyncio.to_thread(db.add_transaction,
            user_id=user_id,
            tx_type=parsed["type"],
            amount=parsed["amount"],
            category_id=parsed["category_id"],
            payment_method_id=parsed["payment_method_id"],
            store=parsed["store"],
            description=parsed["description"],
            op_date=parsed["date"],
            op_time=now.strftime("%H:%M"),
        )
    except PermissionError:
        return web.json_response({"error": "read-only"}, status=403)
    await asyncio.to_thread(db.touch_activity, user_id, now.date().isoformat())
    row = await asyncio.to_thread(db.get_transaction_by_id, user_id, tx_id)
    lang = await asyncio.to_thread(db.get_user_language, user_id)
    return web.json_response(_serialize_tx(row, lang), status=201)


@routes.patch("/api/transactions/{tx_id}")
async def update_transaction(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        tx_id = int(request.match_info["tx_id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid id"}, status=400)
    row = await asyncio.to_thread(db.get_transaction_by_id, user_id, tx_id)
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    if not (await asyncio.to_thread(db.can_write_book, user_id)):
        return web.json_response({"error": "read-only"}, status=403)
    if row["type"] == "transfer":
        return web.json_response({"error": "transfer is read-only"}, status=409)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid json"}, status=400)

    merged = {
        "type": payload.get("type", row["type"]),
        "amount": payload.get("amount", money_input_from_tiyn(row["amount"])),
        "category_id": payload.get("category_id", row["category_id"]),
        "date": payload.get("date", row["op_date"]),
        "description": payload.get("description", row["description"]),
        "store": payload.get("store", row["store"]),
        "payment_method_id": payload.get(
            "payment_method_id", row["payment_method_id"]
        ),
    }
    parsed, error = await _parse_tx_payload(merged, user_id)
    if error is not None:
        return error
    try:
        ok = await asyncio.to_thread(db.update_transaction_fields,
            user_id,
            tx_id,
            tx_type=parsed["type"],
            amount=parsed["amount"],
            category_id=parsed["category_id"],
            op_date=parsed["date"],
            description=parsed["description"],
            store=parsed["store"],
            payment_method_id=parsed["payment_method_id"],
        )
    except PermissionError:
        return web.json_response({"error": "read-only"}, status=403)
    if not ok:
        return web.json_response({"error": "update failed"}, status=400)
    lang = await asyncio.to_thread(db.get_user_language, user_id)
    return web.json_response(_serialize_tx((await asyncio.to_thread(db.get_transaction_by_id, user_id, tx_id)), lang))


@routes.delete("/api/transactions/{tx_id}")
async def delete_transaction(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        tx_id = int(request.match_info["tx_id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid id"}, status=400)
    row = await asyncio.to_thread(db.get_transaction_by_id, user_id, tx_id)
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    denied = await _write_or_403(user_id)
    if denied is not None:
        return denied
    try:
        ok = await asyncio.to_thread(db.delete_transaction, user_id, tx_id)
    except PermissionError:
        return web.json_response({"error": "read-only"}, status=403)
    if not ok:
        return web.json_response({"error": "delete failed"}, status=400)
    return web.json_response({"ok": True})


def money_input_from_tiyn(amount_tiyn: int) -> str:
    from money import tiyn_to_tenge

    return str(tiyn_to_tenge(int(amount_tiyn)))


@routes.get("/api/transactions")
async def get_transactions_list(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        limit = int(request.query.get("limit", 50))
        offset = int(request.query.get("offset", 0))
    except ValueError:
        return web.json_response(
            {"error": "limit and offset must be integers"}, status=400
        )
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    date_from = request.query.get("date_from")
    date_to = request.query.get("date_to")
    category = request.query.get("category")
    parsed = _parse_custom_dates(request)
    if isinstance(parsed, web.Response):
        return parsed
    if parsed:
        date_from, date_to = parsed
    elif request.query.get("period"):
        # Mini App использует period, а не явные даты. Раньше endpoint его
        # игнорировал: карточки были за месяц, список операций — за всё время.
        date_from, date_to = _period_bounds(request.query["period"], (await asyncio.to_thread(db.user_today, user_id)))

    category_name = category if category and category != "all" else None
    rows = await asyncio.to_thread(db.get_recent_transactions,
        user_id,
        limit=limit,
        offset=offset,
        date_from=date_from,
        date_to=date_to,
        category_name=category_name,
    )
    total_count = await asyncio.to_thread(db.count_all_transactions,
        user_id,
        date_from=date_from,
        date_to=date_to,
        category_name=category_name,
    )

    lang = await asyncio.to_thread(db.get_user_language, user_id)
    items = [_serialize_tx(r, lang) for r in rows]
    return web.json_response({"items": items, "total": total_count})


def _serialize_goal(g) -> dict:
    return {
        "id": g["id"],
        "name": g["name"],
        "current": int(g["current_amount"]),
        "target": int(g["target_amount"]),
        "deadline": g["deadline"],
        "pct": (g["current_amount"] / g["target_amount"] * 100) if g["target_amount"] else 0,
        "current_formatted": money(g["current_amount"]),
        "target_formatted": money(g["target_amount"]),
    }


async def _serialize_budget(user_id: int, b) -> dict:
    today = await asyncio.to_thread(db.user_today, user_id)
    start = today.replace(day=1)
    if b["category_id"] is None:
        spent = await asyncio.to_thread(db.get_total_expense, user_id, start.isoformat(), today.isoformat())
        category = t("overall_expenses", (await asyncio.to_thread(db.get_user_language, user_id)))
    else:
        spent = await asyncio.to_thread(db.get_category_spent, user_id, b["category_id"], start.isoformat(), today.isoformat())
        category = b["category_name"]
    return {
        "id": int(b["id"]),
        "category_id": b["category_id"],
        "category": category,
        "overall": b["category_id"] is None,
        "spent": int(spent),
        "limit": int(b["monthly_limit"]),
        "pct": (spent / b["monthly_limit"] * 100) if b["monthly_limit"] else 0,
        "spent_formatted": money(spent),
        "limit_formatted": money(b["monthly_limit"]),
    }


@routes.get("/api/budgets")
async def get_budgets_status(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    items = [await _serialize_budget(user_id, b) for b in (await asyncio.to_thread(db.get_budgets, user_id))]
    return web.json_response({"items": items})


@routes.post("/api/budgets")
async def create_or_set_budget(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    denied = await _write_or_403(user_id)
    if denied is not None:
        return denied
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    category_id = payload.get("category_id")
    if category_id in ("", None):
        category_id = None
    else:
        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid category"}, status=400)
        if (await asyncio.to_thread(db.get_category_name, user_id, category_id)) is None:
            return web.json_response({"error": "category not found"}, status=403)
    limit = parse_positive_amount(str(payload.get("monthly_limit", "")))
    if limit is None:
        return web.json_response({"error": "invalid amount"}, status=400)
    try:
        await asyncio.to_thread(db.set_budget, user_id, category_id, limit)
    except PermissionError:
        return web.json_response({"error": "read-only"}, status=403)
    row = next(
        (b for b in (await asyncio.to_thread(db.get_budgets, user_id)) if b["category_id"] == category_id),
        None,
    )
    if row is None:
        return web.json_response({"error": "update failed"}, status=400)
    return web.json_response(await _serialize_budget(user_id, row), status=201)


@routes.patch("/api/budgets/{budget_id}")
async def patch_budget(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    denied = await _write_or_403(user_id)
    if denied is not None:
        return denied
    try:
        budget_id = int(request.match_info["budget_id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid id"}, status=400)
    row = await asyncio.to_thread(db.get_budget_by_id, user_id, budget_id)
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    limit = parse_positive_amount(str(payload.get("monthly_limit", "")))
    if limit is None:
        return web.json_response({"error": "invalid amount"}, status=400)
    try:
        await asyncio.to_thread(db.set_budget, user_id, row["category_id"], limit)
    except PermissionError:
        return web.json_response({"error": "read-only"}, status=403)
    updated = await asyncio.to_thread(db.get_budget_by_id, user_id, budget_id)
    return web.json_response(await _serialize_budget(user_id, updated))


@routes.get("/api/goals")
async def get_goals_status(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"items": [_serialize_goal(g) for g in (await asyncio.to_thread(db.get_goals, user_id))]})


@routes.post("/api/goals")
async def create_goal(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    denied = await _write_or_403(user_id)
    if denied is not None:
        return denied
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    name = str(payload.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "invalid name"}, status=400)
    target = parse_positive_amount(str(payload.get("target_amount", "")))
    if target is None:
        return web.json_response({"error": "invalid amount"}, status=400)
    deadline = payload.get("deadline") or None
    if deadline == "":
        deadline = None
    try:
        goal_id = await asyncio.to_thread(db.create_goal, user_id, name, target, deadline)
    except PermissionError:
        return web.json_response({"error": "read-only"}, status=403)
    except ValueError:
        return web.json_response({"error": "invalid deadline"}, status=400)
    row = await asyncio.to_thread(db.get_goal_by_id, user_id, goal_id)
    return web.json_response(_serialize_goal(row), status=201)


@routes.patch("/api/goals/{goal_id}")
async def patch_goal(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    denied = await _write_or_403(user_id)
    if denied is not None:
        return denied
    try:
        goal_id = int(request.match_info["goal_id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid id"}, status=400)
    if (await asyncio.to_thread(db.get_goal_by_id, user_id, goal_id)) is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    name = payload.get("name")
    if name is not None:
        name = str(name).strip()
        if not name:
            return web.json_response({"error": "invalid name"}, status=400)
    target = payload.get("target_amount")
    if target is not None:
        parsed_target = parse_positive_amount(str(target))
        if parsed_target is None:
            return web.json_response({"error": "invalid amount"}, status=400)
        target = parsed_target
    deadline = payload.get("deadline")
    clear_deadline = deadline == "" or deadline is None and "deadline" in payload
    if deadline == "":
        deadline = None
        clear_deadline = True
    try:
        ok = await asyncio.to_thread(db.update_goal,
            user_id,
            goal_id,
            name=name,
            target_amount=target,
            deadline=deadline if not clear_deadline else None,
            clear_deadline=clear_deadline,
        )
    except PermissionError:
        return web.json_response({"error": "read-only"}, status=403)
    if not ok:
        return web.json_response({"error": "update failed"}, status=400)
    return web.json_response(_serialize_goal((await asyncio.to_thread(db.get_goal_by_id, user_id, goal_id))))


@routes.post("/api/goals/{goal_id}/contribute")
async def contribute_goal(request: web.Request) -> web.Response:
    user_id = await _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    denied = await _write_or_403(user_id)
    if denied is not None:
        return denied
    try:
        goal_id = int(request.match_info["goal_id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid id"}, status=400)
    if (await asyncio.to_thread(db.get_goal_by_id, user_id, goal_id)) is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    amount = parse_positive_amount(str(payload.get("amount", "")))
    if amount is None:
        return web.json_response({"error": "invalid amount"}, status=400)
    try:
        tx_id = await asyncio.to_thread(db.contribute_to_goal, user_id, goal_id, amount)
    except PermissionError:
        return web.json_response({"error": "read-only"}, status=403)
    if tx_id is None:
        return web.json_response({"error": "not found"}, status=404)
    goal = await asyncio.to_thread(db.get_goal_by_id, user_id, goal_id)
    payload_out = _serialize_goal(goal)
    payload_out["transfer_id"] = tx_id
    return web.json_response(payload_out)


async def _on_startup(app: web.Application) -> None:
    await asyncio.to_thread(db.init_db)


async def _on_shutdown(app: web.Application) -> None:
    logger.info("webapp shutting down")


def create_app() -> web.Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан - без него нельзя проверять подпись initData")
    app = web.Application()
    app.add_routes(routes)
    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(),
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
        handle_signals=True,
    )
