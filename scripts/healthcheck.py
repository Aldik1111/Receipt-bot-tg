"""Process health: SQLite ping and optional HTTP /health."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


def check_db() -> dict:
    db.init_db()
    db.count_all_transactions(0)
    return {"db": "ok", **db.scheduler_health()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-only", action="store_true")
    parser.add_argument("--url")
    args = parser.parse_args()
    if args.url:
        with urllib.request.urlopen(args.url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("db") != "ok" and payload.get("status") not in {"ok", "degraded"}:
            print(payload)
            return 1
        print("ok")
        return 0
    payload = check_db()
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("db") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
