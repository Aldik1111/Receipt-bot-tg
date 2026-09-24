"""HTTP regressions for database thread placement and loop responsiveness."""

import asyncio
import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

import db
import webapp_api


def auth_headers(token, user_id):
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    message = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    pairs["hash"] = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(pairs)}


class ApiWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, name, value in (
            (db, "DB_PATH", str(Path(self.tmp.name) / "api.db")),
            (webapp_api, "BOT_TOKEN", "123456:worker-test"),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        db.ensure_user(1, "owner")
        self.category = db.get_categories(1)[0]["id"]
        self.headers = auth_headers(webapp_api.BOT_TOKEN, 1)
        self.client = TestClient(TestServer(webapp_api.create_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_slow_database_does_not_block_event_loop(self):
        order = []
        original = db.count_all_transactions

        def slow_count(*args, **kwargs):
            order.append("start")
            time.sleep(0.3)
            result = original(*args, **kwargs)
            order.append("end")
            return result

        async def tick():
            while "start" not in order:
                await asyncio.sleep(0.005)
            order.append("tick")

        with patch.object(db, "count_all_transactions", slow_count):
            response, _ = await asyncio.gather(self.client.get("/health"), tick())
        self.assertEqual(response.status, 200)
        self.assertEqual(order, ["start", "tick", "end"])

    async def test_reports_use_aggregates_and_preserve_response_totals(self):
        today = db.user_today(1).isoformat()
        db.add_transaction(1, "expense", 12345, self.category, None, None, None, today)
        db.add_transaction(1, "income", 20000, self.category, None, None, None, today)
        goal = db.create_goal(1, "Trip", 100000, None)
        db.contribute_to_goal(1, goal, 1000)
        with patch.object(db, "get_transactions", side_effect=AssertionError("full-row report")):
            response = await self.client.get("/api/summary", headers=self.headers)
            self.assertEqual(response.status, 200, await response.text())
            summary = await response.json()
            self.assertEqual((summary["expense"], summary["income"], summary["balance"]),
                             (12345, 20000, 7655))
            response = await self.client.get("/api/categories", headers=self.headers)
            self.assertEqual(response.status, 200, await response.text())
            categories = await response.json()
            self.assertEqual(categories["total"], 12345)
            self.assertEqual(len(categories["items"]), 1)
            self.assertEqual(categories["items"][0]["pct"], 100)

    async def test_permission_revoked_during_transaction_write_returns_403(self):
        today = db.user_today(1).isoformat()
        tx = db.add_transaction(1, "expense", 100, self.category, None, None, None, today)
        for method, path, function in (
            ("POST", "/api/transactions", "add_transaction"),
            ("PATCH", f"/api/transactions/{tx}", "update_transaction_fields"),
        ):
            with self.subTest(method=method), patch.object(
                db, function, side_effect=PermissionError("read-only book")
            ):
                response = await self.client.request(method, path, headers=self.headers, json={
                    "type": "expense", "amount": "500", "category_id": self.category,
                })
                self.assertEqual(response.status, 403, await response.text())
        self.assertEqual(db.count_all_transactions(1), 1)
        self.assertEqual(db.get_transaction_by_id(1, tx)["amount"], 100)

    async def test_http_database_connections_run_outside_event_loop(self):
        loop_thread = threading.get_ident()
        connect = sqlite3.connect

        def checked_connect(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return connect(*args, **kwargs)

        async def request(method, path, body=None, status=200):
            response = await self.client.request(method, path, json=body, headers=self.headers)
            text = await response.text()
            self.assertEqual(response.status, status, text)
            return json.loads(text)

        with patch("sqlite3.connect", checked_connect):
            for path in ("/health", "/api/me", "/api/summary", "/api/categories",
                         "/api/category_list", "/api/transactions", "/api/budgets", "/api/goals"):
                await request("GET", path)
            tx = await request("POST", "/api/transactions", {
                "type": "expense", "amount": "500", "category_id": self.category,
            }, 201)
            await request("PATCH", f"/api/transactions/{tx['id']}", {"amount": "600"})
            await request("DELETE", f"/api/transactions/{tx['id']}")
            budget = await request("POST", "/api/budgets", {"monthly_limit": "1000"}, 201)
            await request("PATCH", f"/api/budgets/{budget['id']}", {"monthly_limit": "2000"})
            goal = await request("POST", "/api/goals", {"name": "Trip", "target_amount": "1000"}, 201)
            await request("PATCH", f"/api/goals/{goal['id']}", {"name": "Holiday"})
            await request("POST", f"/api/goals/{goal['id']}/contribute", {"amount": "100"})
