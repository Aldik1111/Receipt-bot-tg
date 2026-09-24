"""Bounded local concurrency probe; all data lives in a disposable SQLite database.

Run: python scripts/load_probe.py
Latencies exclude semaphore queue time. This is a local smoke load, not a
production capacity estimate. The concurrent backup may capture earlier writes.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlencode
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def bounded(maximum):
    def parse(value):
        number = int(value)
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between 1 and {maximum}")
        return number
    return parse


def inspect_database(path):
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        count, amount = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transactions"
        ).fetchone()
        return {
            "count": count, "sum_tiyn": amount,
            "integrity_check": [row[0] for row in conn.execute("PRAGMA integrity_check")],
            "foreign_key_check": [list(row) for row in conn.execute("PRAGMA foreign_key_check")],
        }
    finally:
        conn.close()


async def probe(args, db, api, source, backup):
    from aiohttp import ClientTimeout
    from aiohttp.test_utils import TestClient, TestServer
    from scripts.backup_sqlite import copy_database

    db.init_db()
    db.ensure_user(1, "load-probe", language="en")
    category = db.get_categories(1)[0]
    payment = db.get_default_payment_method_id(1)
    book = db.active_book_id(1)
    today = db.user_today(1).isoformat()
    with db.get_conn() as conn:
        conn.executemany(
            """INSERT INTO transactions
               (user_id,type,amount,category_id,payment_method_id,description,
                op_date,op_time,created_at,created_by,book_id)
               VALUES (1,'expense',100,?,?,?,?,'12:00',?,1,?)""",
            [(category["id"], payment, f"seed-{i}", today, today, book)
             for i in range(args.seed)],
        )
    db.save_state("import_draft", "load-probe", [
        {"amount": 300, "type": "expense", "date": today,
         "description": f"import-{i}", "category": category["name"]}
        for i in range(args.import_rows)
    ], user_id=1)

    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 1})}
    secret = hmac.new(b"WebAppData", api.BOT_TOKEN.encode(), hashlib.sha256).digest()
    message = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    headers = {"X-Telegram-Init-Data": urlencode(fields)}
    timings = defaultdict(list)
    errors = []
    semaphore = asyncio.Semaphore(args.concurrency)

    async def measured(name, operation):
        async with semaphore:
            start = time.perf_counter()
            try:
                await operation()
            except Exception as exc:
                errors.append({"operation": name, "error": f"{type(exc).__name__}: {exc}"})
            finally:
                timings[name].append((time.perf_counter() - start) * 1000)

    async with TestClient(TestServer(api.create_app()),
                          timeout=ClientTimeout(total=60)) as client:
        async def http(index):
            if index % 4 == 3:
                method, path, status = "POST", "/api/transactions", 201
                body = {"type": "expense", "amount": "2.00", "category_id": category["id"],
                        "description": f"http-{index}"}
            else:
                method, status, body = "GET", 200, None
                path = ("/api/summary", "/api/categories", "/api/transactions")[index % 4]
            async with client.request(method, path, json=body, headers=headers) as response:
                data = await response.json()
                if response.status != status:
                    raise RuntimeError(f"HTTP {response.status}: {data}")

        async def bot(index):
            await asyncio.to_thread(db.add_transaction, 1, "expense", 400,
                                    category["id"], payment, None, f"bot-{index}", today)

        async def import_rows():
            count = await asyncio.to_thread(db.commit_import_draft, 1, "load-probe", payment, today)
            if count != args.import_rows:
                raise RuntimeError(f"imported {count}, expected {args.import_rows}")

        async def backup_database():
            await asyncio.to_thread(copy_database, source, backup)

        start = time.perf_counter()
        # Place each kind of work early enough to contend with HTTP operations.
        jobs = [measured("http", lambda i=i: http(i)) for i in range(min(4, args.requests))]
        jobs += [measured("bot", lambda i=i: bot(i)) for i in range(min(2, args.bot_writes))]
        jobs += [measured("import", import_rows), measured("backup", backup_database)]
        jobs += [measured("http", lambda i=i: http(i)) for i in range(4, args.requests)]
        jobs += [measured("bot", lambda i=i: bot(i)) for i in range(2, args.bot_writes)]
        await asyncio.gather(*jobs)
        elapsed = time.perf_counter() - start

    expected = {"count": args.seed + args.requests // 4 + args.bot_writes + args.import_rows,
                "sum_tiyn": args.seed * 100 + (args.requests // 4) * 200
                + args.bot_writes * 400 + args.import_rows * 300}
    databases = {}
    for name, path in (("live", source), ("backup", backup)):
        try:
            databases[name] = inspect_database(path)
            state = databases[name]
            if state["integrity_check"] != ["ok"] or state["foreign_key_check"]:
                errors.append({"operation": name, "error": "database integrity failure"})
            if name == "live" and any(state[key] != value for key, value in expected.items()):
                errors.append({"operation": name, "error": "count or sum mismatch"})
            if name == "backup" and not (args.seed <= state["count"] <= expected["count"]
                                         and args.seed * 100 <= state["sum_tiyn"] <= expected["sum_tiyn"]):
                errors.append({"operation": name, "error": "snapshot outside expected bounds"})
        except Exception as exc:
            errors.append({"operation": name, "error": str(exc)})
    latency = {}
    for name, values in timings.items():
        values.sort()
        latency[name] = {"samples": len(values), "p50_ms": round(values[math.ceil(len(values)*.5)-1], 2),
                         "p95_ms": round(values[math.ceil(len(values)*.95)-1], 2),
                         "max_ms": round(max(values), 2)}
    return {"config": vars(args), "elapsed_seconds": round(elapsed, 3), "latencies": latency,
            "expected_live": expected, "databases": databases,
            "error_count": len(errors), "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=bounded(100000), default=10000)
    parser.add_argument("--concurrency", type=bounded(32), default=8)
    parser.add_argument("--requests", type=bounded(1000), default=80)
    parser.add_argument("--bot-writes", type=bounded(1000), default=20)
    parser.add_argument("--import-rows", type=bounded(10000), default=100)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="receipt-load-probe-") as directory:
        source = Path(directory) / "live.db"
        backup = Path(directory) / "backup.db"
        # Do not read .env or use real credentials, even while importing config.
        with patch("dotenv.load_dotenv", return_value=False), patch.dict(os.environ, {
            "BOT_TOKEN": "123456:local-load-probe", "DB_PATH": str(source), "GEMINI_API_KEY": "",
        }):
            import db
            import webapp_api
            with patch.object(db, "DB_PATH", str(source)), patch.object(
                webapp_api, "BOT_TOKEN", "123456:local-load-probe"
            ):
                with patch.object(logging.getLogger("webapp_api"), "disabled", True), patch.object(
                    logging.getLogger("aiohttp.access"), "disabled", True
                ):
                    result = asyncio.run(probe(args, db, webapp_api, source, backup))
    print(json.dumps(result, indent=2))
    return bool(result["error_count"])


if __name__ == "__main__":
    raise SystemExit(main())
