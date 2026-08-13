"""
Бэкенд для Telegram Mini App. Отдельный процесс от bot.py (тот же db.py,
та же SQLite), слушает локально на 127.0.0.1:8000 - наружу его пускает
Caddy как reverse proxy по пути /api/* (см. Caddyfile в README).

Каждый запрос обязан нести initData в заголовке X-Telegram-Init-Data -
без валидной подписи данные не отдаются никому.
"""

import json
import logging
from datetime import date, timedelta

from aiohttp import web

import db
from categorizer import categorize  # noqa: F401 - не используется напрямую, но держим импорт рядом с db
from config import BOT_TOKEN
from formatting import money
from webapp_auth import validate_init_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _authenticate(request: web.Request) -> int | None:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_init_data(init_data, BOT_TOKEN)
    return user["id"] if user else None


def _period_bounds(period: str) -> tuple[str, str]:
    today = date.today()
    if period == "week":
        start = today - timedelta(days=today.weekday())
    elif period == "3months":
        start = (today.replace(day=1) - timedelta(days=62)).replace(day=1)
    elif period == "year":
        start = today.replace(month=1, day=1)
    else:  # month по умолчанию
        start = today.replace(day=1)
    return start.isoformat(), today.isoformat()


@routes.get("/api/summary")
async def get_summary(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    period = request.query.get("period", "month")
    if request.query.get("date_from") and request.query.get("date_to"):
        date_from, date_to = request.query["date_from"], request.query["date_to"]
    else:
        date_from, date_to = _period_bounds(period)
    rows = db.get_transactions(user_id, date_from, date_to)
    expense = sum(r["amount"] for r in rows if r["type"] == "expense")
    income = sum(r["amount"] for r in rows if r["type"] == "income")

    return web.json_response({
        "date_from": date_from,
        "date_to": date_to,
        "expense": expense,
        "income": income,
        "balance": income - expense,
        "expense_formatted": money(expense),
        "income_formatted": money(income),
        "balance_formatted": money(income - expense),
    })


@routes.get("/api/categories")
async def get_categories_breakdown(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    period = request.query.get("period", "month")
    if request.query.get("date_from") and request.query.get("date_to"):
        date_from, date_to = request.query["date_from"], request.query["date_to"]
    else:
        date_from, date_to = _period_bounds(period)
    rows = db.get_transactions(user_id, date_from, date_to)
    expenses = [r for r in rows if r["type"] == "expense"]
    total = sum(r["amount"] for r in expenses)

    totals: dict[str, float] = {}
    emojis: dict[str, str] = {}
    for r in expenses:
        name = r["category_name"] or "Без категории"
        totals[name] = totals.get(name, 0) + r["amount"]
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
    user_id = _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    items = [{"name": c["name"], "emoji": c["emoji"]} for c in db.get_categories(user_id)]
    return web.json_response({"items": items})


@routes.get("/api/transactions")
async def get_transactions_list(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    limit = min(int(request.query.get("limit", 50)), 200)
    offset = int(request.query.get("offset", 0))
    date_from = request.query.get("date_from")
    date_to = request.query.get("date_to")
    category = request.query.get("category")

    rows = db.get_recent_transactions(user_id, limit=500, offset=0, date_from=date_from, date_to=date_to)
    if category and category != "all":
        rows = [r for r in rows if r["category_name"] == category]
    total_count = len(rows)
    rows = rows[offset:offset + limit]

    items = [{
        "id": r["id"],
        "type": r["type"],
        "amount": r["amount"],
        "amount_formatted": money(r["amount"]),
        "category": r["category_name"],
        "category_emoji": r["category_emoji"] or "🏷",
        "payment": r["payment_name"],
        "store": r["store"],
        "description": r["description"],
        "date": r["op_date"],
        "time": r["op_time"],
    } for r in rows]
    return web.json_response({"items": items, "total": total_count})


@routes.get("/api/budgets")
async def get_budgets_status(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    today = date.today()
    start = today.replace(day=1)
    items = []
    for b in db.get_budgets(user_id):
        spent = db.get_category_spent(user_id, b["category_id"], start.isoformat(), today.isoformat())
        items.append({
            "category": b["category_name"],
            "spent": spent,
            "limit": b["monthly_limit"],
            "pct": (spent / b["monthly_limit"] * 100) if b["monthly_limit"] else 0,
            "spent_formatted": money(spent),
            "limit_formatted": money(b["monthly_limit"]),
        })
    return web.json_response({"items": items})


@routes.get("/api/goals")
async def get_goals_status(request: web.Request) -> web.Response:
    user_id = _authenticate(request)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    items = [{
        "id": g["id"],
        "name": g["name"],
        "current": g["current_amount"],
        "target": g["target_amount"],
        "pct": (g["current_amount"] / g["target_amount"] * 100) if g["target_amount"] else 0,
        "current_formatted": money(g["current_amount"]),
        "target_formatted": money(g["target_amount"]),
    } for g in db.get_goals(user_id)]
    return web.json_response({"items": items})


def create_app() -> web.Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан - без него нельзя проверять подпись initData")
    db.init_db()
    app = web.Application()
    app.add_routes(routes)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="127.0.0.1", port=8000)