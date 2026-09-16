"""Соединение SQLite, константы схемы и мелкие PRAGMA-хелперы."""
import json
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from config import (
    BOOK_INVITE_HOURS,
    DB_PATH,
    DEFAULT_CATEGORIES,
    DEFAULT_CATEGORY_EMOJIS,
    DEFAULT_PAYMENT_METHODS,
    DEFAULT_PAYMENT_METHODS_I18N,
    GEMINI_DAILY_LIMIT,
    GEMINI_PRO_LIMIT,
    PLAN_FREE,
    PLAN_PRO,
    PRO_DURATION_DAYS,
    all_protected_category_names,
    default_categories_for,
    default_emojis_for,
    protected_category_name,
)
from fingerprints import import_row_fingerprint
from money import MoneyError, as_stored_tiyn, backup_amount_to_tiyn, backup_signed_amount_to_tiyn
from timeutil import DEFAULT_TIMEZONE, TIMEZONE_CHOICES, normalize_timezone, now_in_tz, today_in_tz

PROTECTED_CATEGORY_NAME = "Прочее"
PROTECTED_CATEGORY_NAMES = all_protected_category_names()
SAVINGS_CATEGORY_NAME = "Накопления"
SAVINGS_CATEGORY_EMOJI = "🎯"
TX_REGULAR_TYPES = ("expense", "income")
TX_ALL_TYPES = ("expense", "income", "transfer")
MONEY_TIYN_KEY = "money_stored_as_tiyn_v1"
GOALS_SCHEMA_KEY = "goals_transfers_overall_budget_v1"
BACKUP_VERSION = 5
RECEIPT_TOTAL_MISMATCH_TIYN = 100
BOOKS_SCHEMA_KEY = "family_books_v1"
BOOK_ROLES = ("owner", "write", "read")


def _active_db_path() -> str:
    facade = sys.modules.get("db")
    if facade is not None and getattr(facade, "DB_PATH", None):
        return facade.DB_PATH
    return DB_PATH


@contextmanager
def get_conn():
    # timeout=5: если bot.py и webapp_api.py одновременно пишут в один файл,
    # ждать освобождения блокировки, а не сразу падать с "database is locked".
    conn = sqlite3.connect(_active_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: читатели (дашборд) не блокируют писателя (бот) и наоборот.
    # journal_mode пишется в файл БД, повторный PRAGMA просто подтверждает "wal".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        # Явный rollback важен для составных операций: при любой ошибке
        # не оставляем ни частично сохранённый чек, ни занятую транзакцию.
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn, table: str, column: str, add_sql: str) -> None:
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {add_sql}")


def _column_type(conn, table: str, column: str) -> str:
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row["name"] == column:
            return (row["type"] or "").upper()
    return ""


def _column_names(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _column_notnull(conn, table: str, column: str) -> bool:
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row["name"] == column:
            return bool(row["notnull"])
    return False


def _table_sql(conn, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] if row and row[0] else ""


def _ensure_tx_indexes(conn) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions(user_id, op_date)"
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_tx_user_category_date
           ON transactions(user_id, category_id, op_date)"""
    )


def _ensure_budget_indexes(conn) -> None:
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_category
           ON category_budgets(user_id, category_id)
           WHERE category_id IS NOT NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_overall
           ON category_budgets(user_id)
           WHERE category_id IS NULL"""
    )
