"""
Слой работы с базой данных (SQLite).

Схема:
  users            - пользователи бота
  categories       - категории расходов/доходов (свои у каждого пользователя)
  payment_methods  - способы оплаты (свои у каждого пользователя)
  receipts         - "шапка" чека (магазин, дата, время, способ оплаты)
  transactions     - отдельные операции (расход/доход/перевод в цель). Если
                      операция создана из чека - привязана к receipts через
                      receipt_id, и тогда каждая строка чека = отдельная
                      transaction (это даёт точную статистику по категориям
                      и товарам). Пополнение и снятие с цели - type=transfer
                      с goal_id/goal_delta, чтобы статистика расходов не врала.
"""

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from config import (
    BOOK_INVITE_HOURS,
    DB_PATH,
    DEFAULT_CATEGORIES,
    DEFAULT_CATEGORY_EMOJIS,
    DEFAULT_PAYMENT_METHODS,
    GEMINI_DAILY_LIMIT,
    GEMINI_PRO_LIMIT,
    PLAN_FREE,
    PLAN_PRO,
    PRO_DURATION_DAYS,
)
from fingerprints import import_row_fingerprint
from money import MoneyError, as_stored_tiyn, backup_amount_to_tiyn, backup_signed_amount_to_tiyn
from timeutil import DEFAULT_TIMEZONE, TIMEZONE_CHOICES, normalize_timezone, now_in_tz, today_in_tz

PROTECTED_CATEGORY_NAME = "Прочее"
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


@contextmanager
def get_conn():
    # timeout=5: если bot.py и webapp_api.py одновременно пишут в один файл,
    # ждать освобождения блокировки, а не сразу падать с "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
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


def _money_totals(conn) -> dict:
    def scalar(sql: str):
        return conn.execute(sql).fetchone()[0] or 0

    return {
        "transactions_n": scalar("SELECT COUNT(*) FROM transactions"),
        "transactions_sum": scalar("SELECT COALESCE(SUM(amount), 0) FROM transactions"),
        "recurring_n": scalar("SELECT COUNT(*) FROM recurring_payments"),
        "recurring_sum": scalar("SELECT COALESCE(SUM(amount), 0) FROM recurring_payments"),
        "budgets_n": scalar("SELECT COUNT(*) FROM category_budgets"),
        "budgets_sum": scalar("SELECT COALESCE(SUM(monthly_limit), 0) FROM category_budgets"),
        "goals_n": scalar("SELECT COUNT(*) FROM savings_goals"),
        "goals_target_sum": scalar("SELECT COALESCE(SUM(target_amount), 0) FROM savings_goals"),
        "goals_current_sum": scalar("SELECT COALESCE(SUM(current_amount), 0) FROM savings_goals"),
    }


def _rebuild_table(conn, table: str, create_sql: str, columns: str) -> None:
    new_table = f"{table}_new"
    conn.execute(f"DROP TABLE IF EXISTS {new_table}")
    conn.execute(create_sql)
    conn.execute(f"INSERT INTO {new_table} ({columns}) SELECT {columns} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {new_table} RENAME TO {table}")


def _scale_legacy_tenge_columns(conn, table: str, *columns: str) -> None:
    quoted = ", ".join(columns)
    rows = conn.execute(f"SELECT rowid AS rid, {quoted} FROM {table}").fetchall()
    assignments = ", ".join(f"{column}=?" for column in columns)
    for row in rows:
        values = []
        for column in columns:
            raw = row[column]
            if raw is None:
                values.append(None)
            else:
                values.append(backup_amount_to_tiyn(raw, "tenge"))
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE rowid=?",
            (*values, row["rid"]),
        )


def _migrate_money_to_tiyn(conn) -> dict | None:
    """REAL-тенге → INTEGER-тиыны один раз. Новые БД уже INTEGER — только маркер."""
    if conn.execute("SELECT 1 FROM app_meta WHERE key=?", (MONEY_TIYN_KEY,)).fetchone():
        return None

    amount_type = _column_type(conn, "transactions", "amount")
    before = _money_totals(conn)
    scaled = amount_type == "REAL"
    if scaled:
        # Не SQL ROUND(REAL): 1.005 тенге во float может стать 100 тиын,
        # а tenge_to_tiyn даёт 101 (ROUND_HALF_UP).
        _scale_legacy_tenge_columns(conn, "transactions", "amount")
        _scale_legacy_tenge_columns(conn, "recurring_payments", "amount")
        _scale_legacy_tenge_columns(conn, "category_budgets", "monthly_limit")
        _scale_legacy_tenge_columns(
            conn, "savings_goals", "target_amount", "current_amount"
        )
        # В старых черновиках JSON мог держать и 100.0 (тенге), и 100 (тоже тенге).
        # После перехода int=тиын это нельзя разобрать надёжно — просим прислать снова.
        conn.execute(
            "DELETE FROM app_state WHERE scope IN ('receipt_draft', 'import_draft')"
        )

    if amount_type != "INTEGER":
        conn.execute("PRAGMA foreign_keys = OFF")
        _rebuild_table(
            conn,
            "transactions",
            """CREATE TABLE transactions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('expense', 'income')),
                amount INTEGER NOT NULL,
                category_id INTEGER,
                payment_method_id INTEGER,
                store TEXT,
                description TEXT,
                receipt_id INTEGER,
                op_date TEXT NOT NULL,
                op_time TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(category_id) REFERENCES categories(id),
                FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id),
                FOREIGN KEY(receipt_id) REFERENCES receipts(id)
            )""",
            "id, user_id, type, amount, category_id, payment_method_id, store, "
            "description, receipt_id, op_date, op_time, created_at",
        )
        _rebuild_table(
            conn,
            "recurring_payments",
            """CREATE TABLE recurring_payments_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('expense', 'income')),
                amount INTEGER NOT NULL,
                category_id INTEGER,
                payment_method_id INTEGER,
                description TEXT,
                day_of_month INTEGER NOT NULL,
                last_run_date TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(category_id) REFERENCES categories(id),
                FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id)
            )""",
            "id, user_id, type, amount, category_id, payment_method_id, description, "
            "day_of_month, last_run_date, active, created_at",
        )
        _rebuild_table(
            conn,
            "category_budgets",
            """CREATE TABLE category_budgets_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                monthly_limit INTEGER NOT NULL,
                UNIQUE(user_id, category_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(category_id) REFERENCES categories(id)
            )""",
            "id, user_id, category_id, monthly_limit",
        )
        _rebuild_table(
            conn,
            "savings_goals",
            """CREATE TABLE savings_goals_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                target_amount INTEGER NOT NULL,
                current_amount INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )""",
            "id, user_id, name, target_amount, current_amount, created_at",
        )
        _ensure_tx_indexes(conn)
        conn.execute("PRAGMA foreign_keys = ON")

    after = _money_totals(conn)
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?,?)",
        (
            MONEY_TIYN_KEY,
            json.dumps(
                {"before": before, "after": after, "scaled": scaled},
                ensure_ascii=False,
                default=str,
            ),
        ),
    )
    return {"before": before, "after": after, "scaled": scaled}


def _migrate_goals_transfers_and_overall_budget(conn) -> None:
    """type=transfer + goal_id, дедлайн цели, общий бюджет (category_id NULL)."""
    if conn.execute("SELECT 1 FROM app_meta WHERE key=?", (GOALS_SCHEMA_KEY,)).fetchone():
        _ensure_column(conn, "savings_goals", "deadline", "deadline TEXT")
        _ensure_budget_indexes(conn)
        return

    tx_sql = _table_sql(conn, "transactions")
    need_tx_rebuild = (
        "goal_id" not in _column_names(conn, "transactions")
        or "transfer" not in tx_sql.lower()
    )
    need_budget_rebuild = _column_notnull(conn, "category_budgets", "category_id")

    if need_tx_rebuild or need_budget_rebuild:
        conn.execute("PRAGMA foreign_keys = OFF")
        if need_tx_rebuild:
            _rebuild_table(
                conn,
                "transactions",
                """CREATE TABLE transactions_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('expense', 'income', 'transfer')),
                    amount INTEGER NOT NULL,
                    category_id INTEGER,
                    payment_method_id INTEGER,
                    store TEXT,
                    description TEXT,
                    receipt_id INTEGER,
                    goal_id INTEGER,
                    goal_delta INTEGER,
                    op_date TEXT NOT NULL,
                    op_time TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id),
                    FOREIGN KEY(category_id) REFERENCES categories(id),
                    FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id),
                    FOREIGN KEY(receipt_id) REFERENCES receipts(id),
                    FOREIGN KEY(goal_id) REFERENCES savings_goals(id) ON DELETE SET NULL
                )""",
                "id, user_id, type, amount, category_id, payment_method_id, store, "
                "description, receipt_id, op_date, op_time, created_at",
            )
            _ensure_tx_indexes(conn)
        if need_budget_rebuild:
            _rebuild_table(
                conn,
                "category_budgets",
                """CREATE TABLE category_budgets_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category_id INTEGER,
                    monthly_limit INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id),
                    FOREIGN KEY(category_id) REFERENCES categories(id)
                )""",
                "id, user_id, category_id, monthly_limit",
            )
        conn.execute("PRAGMA foreign_keys = ON")

    _ensure_column(conn, "savings_goals", "deadline", "deadline TEXT")
    _ensure_budget_indexes(conn)
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?,?)",
        (GOALS_SCHEMA_KEY, datetime.now(UTC).isoformat()),
    )


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                UNIQUE(user_id, name)
            );

            CREATE TABLE IF NOT EXISTS payment_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                UNIQUE(user_id, name)
            );

            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                store TEXT,
                receipt_date TEXT,
                receipt_time TEXT,
                payment_method_id INTEGER,
                raw_text TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id)
            );

            CREATE TABLE IF NOT EXISTS savings_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                target_amount INTEGER NOT NULL,
                current_amount INTEGER NOT NULL DEFAULT 0,
                deadline TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('expense', 'income', 'transfer')),
                amount INTEGER NOT NULL,
                category_id INTEGER,
                payment_method_id INTEGER,
                store TEXT,
                description TEXT,
                receipt_id INTEGER,
                goal_id INTEGER,
                goal_delta INTEGER,
                op_date TEXT NOT NULL,
                op_time TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(category_id) REFERENCES categories(id),
                FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id),
                FOREIGN KEY(receipt_id) REFERENCES receipts(id),
                FOREIGN KEY(goal_id) REFERENCES savings_goals(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions(user_id, op_date);
            CREATE INDEX IF NOT EXISTS idx_tx_user_category_date
                ON transactions(user_id, category_id, op_date);

            CREATE TABLE IF NOT EXISTS recurring_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('expense', 'income')),
                amount INTEGER NOT NULL,
                category_id INTEGER,
                payment_method_id INTEGER,
                description TEXT,
                day_of_month INTEGER NOT NULL,
                last_run_date TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(category_id) REFERENCES categories(id),
                FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id)
            );

            CREATE TABLE IF NOT EXISTS category_budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_id INTEGER,
                monthly_limit INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS learned_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                UNIQUE(user_id, keyword),
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            -- Временное состояние диалога: черновики чеков и импорта, шаг FSM,
            -- позиция в списке операций. Раньше жило в словарях в памяти и
            -- терялось при каждом рестарте бота.
            -- user_id без внешнего ключа: состояние FSM может появиться раньше,
            -- чем сам пользователь (например, до первого /start).
            CREATE TABLE IF NOT EXISTS app_state (
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                user_id INTEGER,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, key)
            );

            CREATE INDEX IF NOT EXISTS idx_app_state_user ON app_state(user_id);

            CREATE TABLE IF NOT EXISTS recurring_runs (
                recurring_id INTEGER NOT NULL,
                period TEXT NOT NULL,
                PRIMARY KEY (recurring_id, period),
                FOREIGN KEY(recurring_id) REFERENCES recurring_payments(id)
            );

            -- Маркеры одноразовых миграций данных. В отличие от ALTER TABLE,
            -- такие миграции меняют существующие значения и не должны
            -- повторяться при каждом запуске.
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gemini_usage (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );

            CREATE TABLE IF NOT EXISTS source_fingerprints (
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                PRIMARY KEY (user_id, kind, fingerprint)
            );
            """
        )
        _ensure_column(conn, "users", "language", "language TEXT NOT NULL DEFAULT 'ru'")
        _ensure_column(conn, "users", "digest_frequency", "digest_frequency TEXT NOT NULL DEFAULT 'off'")
        _ensure_column(conn, "users", "digest_last_sent", "digest_last_sent TEXT")
        _ensure_column(conn, "users", "bank_import_enabled", "bank_import_enabled INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "users", "idle_reminder_enabled", "idle_reminder_enabled INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "users", "last_activity_date", "last_activity_date TEXT")
        _ensure_column(conn, "users", "idle_reminder_last_sent", "idle_reminder_last_sent TEXT")
        _ensure_column(conn, "users", "backup_enabled", "backup_enabled INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "users", "backup_last_sent", "backup_last_sent TEXT")
        _ensure_column(
            conn,
            "users",
            "timezone",
            f"timezone TEXT NOT NULL DEFAULT '{DEFAULT_TIMEZONE}'",
        )
        _ensure_column(conn, "users", "onboarded", "onboarded INTEGER NOT NULL DEFAULT 0")
        _ensure_column(
            conn,
            "users",
            "privacy_accepted_version",
            "privacy_accepted_version TEXT",
        )
        _ensure_column(conn, "categories", "emoji", "emoji TEXT NOT NULL DEFAULT '🏷'")
        existing_onboarded = "existing_users_marked_onboarded_v1"
        if not conn.execute(
            "SELECT 1 FROM app_meta WHERE key=?", (existing_onboarded,)
        ).fetchone():
            conn.execute("UPDATE users SET onboarded=1")
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES (?,?)",
                (existing_onboarded, datetime.now(UTC).isoformat()),
            )

        _migrate_money_to_tiyn(conn)
        _migrate_goals_transfers_and_overall_budget(conn)

        # Раньше idle_reminder_enabled создавался с DEFAULT 1, а автобэкап
        # вообще рассылался всем пользователям с операциями. Явного согласия
        # на эти сообщения в БД не было, поэтому безопасная миграция один раз
        # отключает старый неявный opt-in. После маркера ручные настройки
        # пользователя больше никогда не сбрасываются при старте.
        migration_key = "automatic_messages_require_opt_in_v1"
        migrated = conn.execute(
            "SELECT 1 FROM app_meta WHERE key=?", (migration_key,)
        ).fetchone()
        if not migrated:
            conn.execute("UPDATE users SET idle_reminder_enabled=0, backup_enabled=0")
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES (?,?)",
                (migration_key, datetime.now(UTC).isoformat()),
            )

        _migrate_family_billing(conn)


def _create_personal_book(conn, user_id: int) -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO books(owner_user_id, name, created_at) VALUES (?,?,?)",
        (user_id, "personal", now),
    )
    book_id = cur.lastrowid
    conn.execute(
        """INSERT INTO book_members(book_id, user_id, role, joined_at)
           VALUES (?,?,?,?)""",
        (book_id, user_id, "owner", now),
    )
    conn.execute(
        "UPDATE users SET active_book_id=? WHERE user_id=?",
        (book_id, user_id),
    )
    return book_id


def _migrate_family_billing(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS book_members (
            book_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('owner', 'write', 'read')),
            joined_at TEXT NOT NULL,
            PRIMARY KEY (book_id, user_id),
            FOREIGN KEY(book_id) REFERENCES books(id),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS book_invites (
            code TEXT PRIMARY KEY,
            book_id INTEGER NOT NULL,
            created_by INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('write', 'read')),
            expires_at TEXT NOT NULL,
            used_by INTEGER,
            used_at TEXT,
            FOREIGN KEY(book_id) REFERENCES books(id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            actor_user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            entity TEXT,
            entity_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            stars INTEGER,
            plan TEXT NOT NULL,
            status TEXT NOT NULL,
            paid_at TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    _ensure_column(conn, "users", "active_book_id", "active_book_id INTEGER")
    _ensure_column(conn, "users", "plan", f"plan TEXT NOT NULL DEFAULT '{PLAN_FREE}'")
    _ensure_column(conn, "users", "plan_until", "plan_until TEXT")
    _ensure_column(conn, "transactions", "created_by", "created_by INTEGER")
    _ensure_column(conn, "transactions", "book_id", "book_id INTEGER")
    if conn.execute("SELECT 1 FROM app_meta WHERE key=?", (BOOKS_SCHEMA_KEY,)).fetchone():
        return
    for row in conn.execute("SELECT user_id FROM users").fetchall():
        existing = conn.execute(
            "SELECT id FROM books WHERE owner_user_id=?",
            (row["user_id"],),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET active_book_id=? WHERE user_id=? AND active_book_id IS NULL",
                (existing["id"], row["user_id"]),
            )
            continue
        _create_personal_book(conn, row["user_id"])
    conn.execute(
        """UPDATE transactions SET created_by = user_id
           WHERE created_by IS NULL"""
    )
    conn.execute(
        """UPDATE transactions SET book_id = (
               SELECT u.active_book_id FROM users u WHERE u.user_id = transactions.user_id
           )
           WHERE book_id IS NULL"""
    )
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?,?)",
        (BOOKS_SCHEMA_KEY, datetime.now(UTC).isoformat()),
    )


def ensure_user(user_id: int, username: str | None):
    """Создаёт пользователя и его дефолтные категории/способы оплаты, если его ещё нет."""
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return
        # Значения opt-in указываем явно: в старой базе SQL-default для
        # idle_reminder_enabled мог остаться равным 1 даже после миграции.
        conn.execute(
            """INSERT INTO users(
                   user_id, username, created_at,
                   idle_reminder_enabled, backup_enabled
               ) VALUES (?,?,?,?,?)""",
            (user_id, username, datetime.now(UTC).isoformat(), 0, 0),
        )
        for cat_name in DEFAULT_CATEGORIES.keys():
            emoji = DEFAULT_CATEGORY_EMOJIS.get(cat_name, "🏷")
            conn.execute(
                "INSERT OR IGNORE INTO categories(user_id, name, emoji) VALUES (?,?,?)",
                (user_id, cat_name, emoji),
            )
        for pm_name in DEFAULT_PAYMENT_METHODS:
            conn.execute(
                "INSERT OR IGNORE INTO payment_methods(user_id, name) VALUES (?,?)",
                (user_id, pm_name),
            )
        _create_personal_book(conn, user_id)


def get_categories(user_id: int) -> list[sqlite3.Row]:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM categories WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall()


def add_category(user_id: int, name: str, emoji: str = "🏷") -> int:
    user_id = _require_write(user_id)
    name = name.strip()[:64]
    emoji = (emoji or "🏷")[:16]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO categories(user_id, name, emoji) VALUES (?,?,?)",
            (user_id, name, emoji),
        )
        row = conn.execute(
            "SELECT id FROM categories WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        return row["id"]


def get_category_id_by_name(user_id: int, name: str) -> int | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM categories WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        return row["id"] if row else None


def get_payment_method_id_by_name(user_id: int, name: str) -> int | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM payment_methods WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        return row["id"] if row else None


def get_payment_methods(user_id: int) -> list[sqlite3.Row]:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM payment_methods WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall()


def add_payment_method(user_id: int, name: str) -> int:
    user_id = _require_write(user_id)
    name = name.strip()[:64]
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO payment_methods(user_id, name) VALUES (?,?)",
            (user_id, name),
        )
        row = conn.execute(
            "SELECT id FROM payment_methods WHERE user_id=? AND name=?",
            (user_id, name),
        ).fetchone()
        return row["id"]


def _owned_category_id(conn, user_id: int, category_id: int | None) -> int | None:
    if category_id is not None:
        row = conn.execute(
            "SELECT id FROM categories WHERE id=? AND user_id=?",
            (category_id, user_id),
        ).fetchone()
        if row:
            return row["id"]
    fallback = conn.execute(
        "SELECT id FROM categories WHERE user_id=? AND name=?",
        (user_id, PROTECTED_CATEGORY_NAME),
    ).fetchone()
    return fallback["id"] if fallback else None


def _owned_payment_id(conn, user_id: int, payment_method_id: int | None) -> int | None:
    if payment_method_id is None:
        return None
    row = conn.execute(
        "SELECT id FROM payment_methods WHERE id=? AND user_id=?",
        (payment_method_id, user_id),
    ).fetchone()
    return row["id"] if row else None


def add_transaction(
    user_id: int,
    tx_type: str,
    amount: int,
    category_id: int | None,
    payment_method_id: int | None,
    store: str | None,
    description: str | None,
    op_date: str,
    op_time: str | None = None,
    receipt_id: int | None = None,
) -> int:
    if tx_type not in TX_REGULAR_TYPES:
        raise ValueError("Недопустимый тип операции")
    if not can_write_book(user_id):
        raise PermissionError("read-only book")
    actor_id = user_id
    owner_id = scope_user(user_id)
    book_id = active_book_id(user_id)
    amount_tiyn = as_stored_tiyn(amount)
    with get_conn() as conn:
        category_id = _owned_category_id(conn, owner_id, category_id)
        payment_method_id = _owned_payment_id(conn, owner_id, payment_method_id)
        cur = conn.execute(
            """INSERT INTO transactions(
                   user_id, type, amount, category_id, payment_method_id, store,
                   description, receipt_id, op_date, op_time, created_at,
                   created_by, book_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                owner_id,
                tx_type,
                amount_tiyn,
                category_id,
                payment_method_id,
                store,
                (description or "")[:500] or None,
                receipt_id,
                op_date,
                op_time,
                datetime.now(UTC).isoformat(),
                actor_id,
                book_id,
            ),
        )
        write_audit(
            book_id, actor_id, "tx_create", "transaction", cur.lastrowid, conn=conn
        )
        return cur.lastrowid


def save_receipt_draft(
    user_id: int, draft_id: str, fallback_date: str
) -> tuple[int, set[int]] | None:
    """Атомарно сохраняет черновик чека и удаляет его.

    Возвращает (receipt_id, затронутые категории) или None, если черновик уже
    сохранён/отменён. BEGIN IMMEDIATE берёт право на запись до чтения: два
    одновременных нажатия кнопки не смогут оба прочитать один черновик.
    При исключении get_conn откатит шапку, все позиции и удаление черновика.
    """
    if not can_write_book(user_id):
        raise PermissionError("read-only book")
    actor_id = user_id
    owner_id = scope_user(user_id)
    book_id = active_book_id(user_id)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT payload FROM app_state
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (draft_id, user_id),
        ).fetchone()
        if not row:
            return None

        parsed = json.loads(row["payload"])
        items = parsed.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("Черновик чека не содержит позиций")
        item_total = sum(as_stored_tiyn(item["price"]) for item in items)
        receipt_total = parsed.get("total")
        if (
            receipt_total is not None
            and abs(item_total - as_stored_tiyn(receipt_total)) > RECEIPT_TOTAL_MISMATCH_TIYN
        ):
            raise ValueError("Сначала нужно выбрать итог чека")

        payment_method_id = _owned_payment_id(conn, owner_id, parsed.get("payment_method_id"))
        if payment_method_id is None:
            payment = conn.execute(
                """SELECT id FROM payment_methods WHERE user_id=?
                   ORDER BY CASE WHEN name='Наличные' THEN 0 ELSE 1 END, id
                   LIMIT 1""",
                (owner_id,),
            ).fetchone()
            payment_method_id = payment["id"] if payment else None
        now = datetime.now(UTC).isoformat()

        receipt_id = conn.execute(
            """INSERT INTO receipts(user_id, store, receipt_date, receipt_time,
               payment_method_id, raw_text, created_at) VALUES (?,?,?,?,?,?,?)""",
            (
                owner_id,
                parsed.get("store"),
                parsed.get("date"),
                parsed.get("time"),
                payment_method_id,
                "" if parsed.get("source") == "bank_notification" else parsed.get("raw_text", ""),
                now,
            ),
        ).lastrowid

        tx_type = parsed.get("tx_type", "expense")
        if tx_type not in ("expense", "income"):
            raise ValueError("Недопустимый тип операции в черновике")

        touched_categories: set[int] = set()
        op_date = parsed.get("date") or fallback_date
        for item in items:
            category = conn.execute(
                "SELECT id FROM categories WHERE user_id=? AND name=?",
                (owner_id, item.get("category") or PROTECTED_CATEGORY_NAME),
            ).fetchone()
            category_id = category["id"] if category else _owned_category_id(conn, owner_id, None)
            if category_id is not None:
                touched_categories.add(category_id)

            conn.execute(
                """INSERT INTO transactions(
                       user_id, type, amount, category_id, payment_method_id, store,
                       description, receipt_id, op_date, op_time, created_at,
                       created_by, book_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    owner_id,
                    tx_type,
                    as_stored_tiyn(item["price"]),
                    category_id,
                    payment_method_id,
                    parsed.get("store"),
                    item.get("name"),
                    receipt_id,
                    op_date,
                    parsed.get("time"),
                    now,
                    actor_id,
                    book_id,
                ),
            )

        conn.execute(
            "UPDATE users SET last_activity_date=? WHERE user_id=?",
            (fallback_date, user_id),
        )
        kind = "bank" if parsed.get("source") == "bank_notification" else "photo"
        for fingerprint in parsed.get("fingerprints") or []:
            if isinstance(fingerprint, str) and fingerprint:
                _remember_fingerprint(conn, owner_id, kind, fingerprint, op_date)
        write_audit(book_id, actor_id, "receipt_save", "receipt", receipt_id, conn=conn)
        deleted = conn.execute(
            """DELETE FROM app_state
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (draft_id, actor_id),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("Черновик чека изменился во время сохранения")

        return receipt_id, touched_categories


def get_receipt_draft(user_id: int, draft_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT payload FROM app_state
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (draft_id, user_id),
        ).fetchone()
    if not row:
        return None
    try:
        parsed = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _mutate_receipt_draft(user_id: int, draft_id: str, mutator) -> dict | None:
    """Атомарно меняет принадлежащий пользователю черновик."""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT payload FROM app_state
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (draft_id, user_id),
        ).fetchone()
        if not row:
            return None
        parsed = json.loads(row["payload"])
        if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
            raise ValueError("Некорректный черновик чека")
        mutator(parsed)
        conn.execute(
            """UPDATE app_state SET payload=?, updated_at=?
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (
                json.dumps(parsed, ensure_ascii=False),
                datetime.now(UTC).isoformat(),
                draft_id,
                user_id,
            ),
        )
        return parsed


def update_receipt_draft_item(
    user_id: int, draft_id: str, item_index: int, name: str, price
) -> dict | None:
    name = name.strip()
    try:
        price_tiyn = as_stored_tiyn(price)
    except MoneyError as exc:
        raise ValueError("Некорректная цена позиции") from exc
    if not name or len(name) > 500:
        raise ValueError("Некорректное название позиции")

    def mutate(parsed: dict) -> None:
        items = parsed["items"]
        if item_index < 0 or item_index >= len(items):
            raise IndexError("Позиция не найдена")
        items[item_index]["name"] = name
        items[item_index]["price"] = price_tiyn
        parsed.pop("total_resolution", None)

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def delete_receipt_draft_item(user_id: int, draft_id: str, item_index: int) -> dict | None:
    def mutate(parsed: dict) -> None:
        items = parsed["items"]
        if item_index < 0 or item_index >= len(items):
            raise IndexError("Позиция не найдена")
        if len(items) == 1:
            raise ValueError("Нельзя удалить последнюю позицию")
        items.pop(item_index)
        parsed.pop("total_resolution", None)

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def update_receipt_draft_payment(
    user_id: int, draft_id: str, payment_method_id: int
) -> dict | None:
    owner_id = scope_user(user_id)
    with get_conn() as conn:
        owned = _owned_payment_id(conn, owner_id, payment_method_id)
        if owned != payment_method_id:
            return None
        row = conn.execute(
            "SELECT name FROM payment_methods WHERE id=? AND user_id=?",
            (payment_method_id, owner_id),
        ).fetchone()
        payment_name = row["name"] if row else None

    def mutate(parsed: dict) -> None:
        parsed["payment_method_id"] = payment_method_id
        parsed["payment_name"] = payment_name

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def update_receipt_draft_type(
    user_id: int,
    draft_id: str,
    tx_type: str,
) -> dict | None:
    if tx_type not in {"expense", "income"}:
        return None

    def mutate(parsed: dict) -> None:
        parsed["tx_type"] = tx_type
        parsed["ambiguous_type"] = False

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def resolve_receipt_draft_total(
    user_id: int, draft_id: str, use_receipt_total: bool
) -> dict | None:
    """Выбирает сумму товаров либо масштабирует позиции до итога чека."""
    def mutate(parsed: dict) -> None:
        items = parsed["items"]
        if not items:
            raise ValueError("Черновик чека не содержит позиций")
        try:
            prices = [as_stored_tiyn(item["price"]) for item in items]
        except MoneyError as exc:
            raise ValueError("Некорректная цена позиции") from exc

        if not use_receipt_total:
            parsed["total"] = sum(prices)
            parsed["total_resolution"] = "items"
            for item, price in zip(items, prices):
                item["price"] = price
            return

        try:
            target_tiyn = as_stored_tiyn(parsed.get("total"))
        except MoneyError as exc:
            raise ValueError("В чеке нет корректного итога") from exc
        if target_tiyn < len(items):
            raise ValueError("Итог слишком мал для количества позиций")

        # Распределяем итог пропорционально исходным ценам в целых тиынах,
        # чтобы сумма записанных операций совпала с итогом чека до тиына.
        price_sum = sum(prices)
        distributable = target_tiyn - len(items)
        shares = [distributable * price // price_sum for price in prices]
        leftovers = [distributable * price % price_sum for price in prices]
        tiyn = [1 + share for share in shares]
        remainder = target_tiyn - sum(tiyn)
        order = sorted(
            range(len(items)),
            key=lambda index: leftovers[index],
            reverse=True,
        )
        for index in order[:remainder]:
            tiyn[index] += 1
        for item, amount_tiyn in zip(items, tiyn):
            item["price"] = amount_tiyn
        parsed["total"] = target_tiyn
        parsed["total_resolution"] = "receipt"

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def get_transactions(user_id: int, date_from: str, date_to: str) -> list[sqlite3.Row]:
    """date_from / date_to в формате YYYY-MM-DD, включительно."""
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            """SELECT t.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE t.user_id=? AND t.op_date BETWEEN ? AND ?
               ORDER BY t.op_date, t.op_time""",
            (user_id, date_from, date_to),
        ).fetchall()


def get_default_payment_method_id(user_id: int) -> int | None:
    """"Наличные", если есть, иначе первый попавшийся способ оплаты пользователя."""
    pms = get_payment_methods(user_id)
    if not pms:
        return None
    default = next((p for p in pms if p["name"] == "Наличные"), pms[0])
    return default["id"]


def update_transaction_category(user_id: int, tx_id: int, category_id: int) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type FROM transactions WHERE id=? AND user_id=?",
            (tx_id, user_id),
        ).fetchone()
        if not row or row["type"] == "transfer":
            return False
        owned = _owned_category_id(conn, user_id, category_id)
        if owned != category_id:
            return False
        cur = conn.execute(
            "UPDATE transactions SET category_id=? WHERE id=? AND user_id=?",
            (owned, tx_id, user_id),
        )
        return cur.rowcount > 0


def rename_category(user_id: int, cat_id: int, new_name: str) -> bool:
    """False, если имя занято, это «Прочее», или пытаются так назвать другую."""
    user_id = _require_write(user_id)
    new_name = new_name.strip()
    if not new_name or new_name == PROTECTED_CATEGORY_NAME:
        return False
    with get_conn() as conn:
        current = conn.execute(
            "SELECT name FROM categories WHERE id=? AND user_id=?",
            (cat_id, user_id),
        ).fetchone()
        if not current or current["name"] == PROTECTED_CATEGORY_NAME:
            return False
        try:
            cur = conn.execute(
                "UPDATE categories SET name=? WHERE id=? AND user_id=?",
                (new_name, cat_id, user_id),
            )
        except sqlite3.IntegrityError:
            return False
        return cur.rowcount > 0


def count_transactions_for_category(user_id: int, cat_id: int) -> int:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE user_id=? AND category_id=?",
            (user_id, cat_id),
        ).fetchone()
        return row["n"]


def delete_category(user_id: int, cat_id: int, fallback_name: str = "Прочее") -> bool:
    """Удаляет категорию, предварительно перенеся все её транзакции в fallback
    (по умолчанию "Прочее"). Отказывает, если удаляют саму fallback-категорию."""
    user_id = _require_write(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM categories WHERE id=? AND user_id=?", (cat_id, user_id)
        ).fetchone()
        if not row:
            return False
        if row["name"] == fallback_name:
            return False  # защищаем базовую категорию от удаления

        fallback = conn.execute(
            "SELECT id FROM categories WHERE user_id=? AND name=?",
            (user_id, fallback_name),
        ).fetchone()
        if not fallback:
            fallback_id = conn.execute(
                "INSERT INTO categories(user_id, name) VALUES (?,?)",
                (user_id, fallback_name),
            ).lastrowid
        else:
            fallback_id = fallback["id"]

        conn.execute(
            "UPDATE transactions SET category_id=? WHERE user_id=? AND category_id=?",
            (fallback_id, user_id, cat_id),
        )
        conn.execute(
            "UPDATE recurring_payments SET category_id=? WHERE user_id=? AND category_id=?",
            (fallback_id, user_id, cat_id),
        )
        conn.execute(
            "UPDATE learned_categories SET category_id=? WHERE user_id=? AND category_id=?",
            (fallback_id, user_id, cat_id),
        )
        conn.execute(
            "DELETE FROM category_budgets WHERE user_id=? AND category_id=?",
            (user_id, cat_id),
        )
        cur = conn.execute(
            "DELETE FROM categories WHERE id=? AND user_id=?", (cat_id, user_id)
        )
        return cur.rowcount > 0


def rename_payment_method(user_id: int, pm_id: int, new_name: str) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "UPDATE payment_methods SET name=? WHERE id=? AND user_id=?",
                (new_name, pm_id, user_id),
            )
        except sqlite3.IntegrityError:
            return False
        return cur.rowcount > 0


def count_transactions_for_payment_method(user_id: int, pm_id: int) -> int:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE user_id=? AND payment_method_id=?",
            (user_id, pm_id),
        ).fetchone()
        return row["n"]


def delete_payment_method(user_id: int, pm_id: int) -> bool:
    """Удаляет способ оплаты. У транзакций, где он использовался, payment_method_id
    станет NULL (способ оплаты был не критичен для истории - в отличие от категории,
    отдельного "запасного" способа оплаты не создаём)."""
    user_id = _require_write(user_id)
    with get_conn() as conn:
        pms = conn.execute(
            "SELECT id FROM payment_methods WHERE user_id=?", (user_id,)
        ).fetchall()
        if len(pms) <= 1:
            return False  # не даём удалить последний способ оплаты
        conn.execute(
            "UPDATE transactions SET payment_method_id=NULL WHERE user_id=? AND payment_method_id=?",
            (user_id, pm_id),
        )
        conn.execute(
            "UPDATE receipts SET payment_method_id=NULL WHERE user_id=? AND payment_method_id=?",
            (user_id, pm_id),
        )
        conn.execute(
            "UPDATE recurring_payments SET payment_method_id=NULL WHERE user_id=? AND payment_method_id=?",
            (user_id, pm_id),
        )
        cur = conn.execute(
            "DELETE FROM payment_methods WHERE id=? AND user_id=?", (pm_id, user_id)
        )
        return cur.rowcount > 0


def get_recent_transactions(
    user_id: int,
    limit: int = 8,
    offset: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
    category_name: str | None = None,
    *,
    query: str | None = None,
    category_id: int | None = None,
    tx_type: str | None = None,
) -> list[sqlite3.Row]:
    """Список операций, новые сверху, с пагинацией и фильтрами в SQL."""
    user_id = scope_user(user_id)
    conditions = ["t.user_id=?"]
    params: list = [user_id]
    if date_from and date_to:
        conditions.append("t.op_date BETWEEN ? AND ?")
        params.extend((date_from, date_to))
    if category_name:
        conditions.append(
            "t.category_id=(SELECT id FROM categories WHERE user_id=? AND name=?)"
        )
        params.extend((user_id, category_name))
    if category_id is not None:
        conditions.append(
            "t.category_id IN (SELECT id FROM categories WHERE user_id=? AND id=?)"
        )
        params.extend((user_id, category_id))
    if tx_type in {"expense", "income", "transfer"}:
        conditions.append("t.type=?")
        params.append(tx_type)
    if query and query.strip():
        escaped = (
            query.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        conditions.append(
            "(COALESCE(t.description, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(t.store, '') LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        )
        params.extend((f"%{escaped}%", f"%{escaped}%"))

    params.extend((limit, offset))
    where = " AND ".join(conditions)
    with get_conn() as conn:
        return conn.execute(
            f"""SELECT t.*, c.name AS category_name, c.emoji AS category_emoji,
                       p.name AS payment_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE {where}
               ORDER BY t.op_date DESC, t.op_time DESC, t.id DESC
               LIMIT ? OFFSET ?""",
            params,
        ).fetchall()


def count_all_transactions(
    user_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    category_name: str | None = None,
    *,
    query: str | None = None,
    category_id: int | None = None,
    tx_type: str | None = None,
) -> int:
    user_id = scope_user(user_id)
    conditions = ["t.user_id=?"]
    params: list = [user_id]
    if date_from and date_to:
        conditions.append("t.op_date BETWEEN ? AND ?")
        params.extend((date_from, date_to))
    if category_name:
        conditions.append(
            "t.category_id=(SELECT id FROM categories WHERE user_id=? AND name=?)"
        )
        params.extend((user_id, category_name))
    if category_id is not None:
        conditions.append(
            "t.category_id IN (SELECT id FROM categories WHERE user_id=? AND id=?)"
        )
        params.extend((user_id, category_id))
    if tx_type in {"expense", "income", "transfer"}:
        conditions.append("t.type=?")
        params.append(tx_type)
    if query and query.strip():
        escaped = (
            query.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        conditions.append(
            "(COALESCE(t.description, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(t.store, '') LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        )
        params.extend((f"%{escaped}%", f"%{escaped}%"))

    where = " AND ".join(conditions)
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS n FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                WHERE {where}""",
            params,
        ).fetchone()
        return row["n"]


def get_transaction_by_id(user_id: int, tx_id: int) -> sqlite3.Row | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            """SELECT t.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name,
                      g.name AS goal_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               LEFT JOIN savings_goals g ON g.id = t.goal_id
               WHERE t.id=? AND t.user_id=?""",
            (tx_id, user_id),
        ).fetchone()


def update_transaction_amount(user_id: int, tx_id: int, amount) -> bool:
    user_id = _require_write(user_id)
    amount_tiyn = as_stored_tiyn(amount)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type FROM transactions WHERE id=? AND user_id=?",
            (tx_id, user_id),
        ).fetchone()
        if not row or row["type"] == "transfer":
            return False
        cur = conn.execute(
            "UPDATE transactions SET amount=? WHERE id=? AND user_id=?",
            (amount_tiyn, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_date(user_id: int, tx_id: int, op_date: str) -> bool:
    """Обновляет дату только своей операции; принимает ISO YYYY-MM-DD."""
    user_id = _require_write(user_id)
    try:
        datetime.strptime(op_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET op_date=? WHERE id=? AND user_id=?",
            (op_date, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_type(user_id: int, tx_id: int, tx_type: str) -> bool:
    user_id = _require_write(user_id)
    if tx_type not in TX_REGULAR_TYPES:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type FROM transactions WHERE id=? AND user_id=?",
            (tx_id, user_id),
        ).fetchone()
        if not row or row["type"] == "transfer":
            return False
        cur = conn.execute(
            "UPDATE transactions SET type=? WHERE id=? AND user_id=?",
            (tx_type, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_store(user_id: int, tx_id: int, store: str | None) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET store=? WHERE id=? AND user_id=?",
            (store, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_description(user_id: int, tx_id: int, description: str | None) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET description=? WHERE id=? AND user_id=?",
            (description, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_payment_method(user_id: int, tx_id: int, payment_method_id: int | None) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        owned = _owned_payment_id(conn, user_id, payment_method_id)
        if payment_method_id is not None and owned != payment_method_id:
            return False
        cur = conn.execute(
            "UPDATE transactions SET payment_method_id=? WHERE id=? AND user_id=?",
            (owned, tx_id, user_id),
        )
        return cur.rowcount > 0


def delete_transaction(user_id: int, tx_id: int) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT receipt_id, type, goal_id, goal_delta
               FROM transactions WHERE id=? AND user_id=?""",
            (tx_id, user_id),
        ).fetchone()
        if not row:
            return False
        if row["type"] == "transfer" and row["goal_id"] and row["goal_delta"]:
            goal = conn.execute(
                "SELECT current_amount FROM savings_goals WHERE id=? AND user_id=?",
                (row["goal_id"], user_id),
            ).fetchone()
            if goal is not None:
                new_current = int(goal["current_amount"]) - int(row["goal_delta"])
                if new_current < 0:
                    return False
                conn.execute(
                    "UPDATE savings_goals SET current_amount=? WHERE id=? AND user_id=?",
                    (new_current, row["goal_id"], user_id),
                )
        receipt_id = row["receipt_id"]
        cur = conn.execute(
            "DELETE FROM transactions WHERE id=? AND user_id=?", (tx_id, user_id)
        )
        if receipt_id:
            leftover = conn.execute(
                "SELECT 1 FROM transactions WHERE receipt_id=? AND user_id=?",
                (receipt_id, user_id),
            ).fetchone()
            if not leftover:
                conn.execute(
                    "DELETE FROM receipts WHERE id=? AND user_id=?",
                    (receipt_id, user_id),
                )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Повторяющиеся платежи
# ---------------------------------------------------------------------------

def add_recurring(
    user_id: int, tx_type: str, amount, category_id: int | None,
    payment_method_id: int | None, description: str, day_of_month: int,
) -> int:
    user_id = _require_write(user_id)
    if tx_type not in TX_REGULAR_TYPES:
        raise ValueError("Недопустимый тип повтора")
    if not (1 <= int(day_of_month) <= 28):
        raise ValueError("День повтора должен быть 1–28")
    amount_tiyn = as_stored_tiyn(amount)
    with get_conn() as conn:
        category_id = _owned_category_id(conn, user_id, category_id)
        payment_method_id = _owned_payment_id(conn, user_id, payment_method_id)
        cur = conn.execute(
            """INSERT INTO recurring_payments
               (user_id, type, amount, category_id, payment_method_id, description,
                day_of_month, last_run_date, active, created_at)
               VALUES (?,?,?,?,?,?,?,NULL,1,?)""",
            (user_id, tx_type, amount_tiyn, category_id, payment_method_id, description,
             day_of_month, datetime.now(UTC).isoformat()),
        )
        return cur.lastrowid


def get_recurring_list(user_id: int) -> list[sqlite3.Row]:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            """SELECT r.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
               FROM recurring_payments r
               LEFT JOIN categories c ON c.id = r.category_id
               LEFT JOIN payment_methods p ON p.id = r.payment_method_id
               WHERE r.user_id=? ORDER BY r.day_of_month""",
            (user_id,),
        ).fetchall()


def get_recurring_by_id(user_id: int, rec_id: int) -> sqlite3.Row | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            """SELECT r.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
               FROM recurring_payments r
               LEFT JOIN categories c ON c.id = r.category_id
               LEFT JOIN payment_methods p ON p.id = r.payment_method_id
               WHERE r.id=? AND r.user_id=?""",
            (rec_id, user_id),
        ).fetchone()


def update_recurring(
    user_id: int,
    rec_id: int,
    *,
    amount=None,
    day_of_month: int | None = None,
    category_id: int | None = None,
    payment_method_id: int | None = None,
    description: str | None = None,
    tx_type: str | None = None,
) -> bool:
    """Меняет поля правила. recurring_runs не трогает — уже прошедшие периоды остаются."""
    user_id = _require_write(user_id)
    if tx_type is not None and tx_type not in TX_REGULAR_TYPES:
        return False
    if day_of_month is not None and not (1 <= int(day_of_month) <= 28):
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM recurring_payments WHERE id=? AND user_id=?",
            (rec_id, user_id),
        ).fetchone()
        if not row:
            return False
        updates: list[str] = []
        params: list = []
        if amount is not None:
            updates.append("amount=?")
            params.append(as_stored_tiyn(amount))
        if day_of_month is not None:
            updates.append("day_of_month=?")
            params.append(int(day_of_month))
        if category_id is not None:
            owned = _owned_category_id(conn, user_id, category_id)
            if owned != category_id:
                return False
            updates.append("category_id=?")
            params.append(owned)
        if payment_method_id is not None:
            owned_pay = _owned_payment_id(conn, user_id, payment_method_id)
            if owned_pay != payment_method_id:
                return False
            updates.append("payment_method_id=?")
            params.append(owned_pay)
        if description is not None:
            text = description.strip()[:200]
            if not text:
                return False
            updates.append("description=?")
            params.append(text)
        if tx_type is not None:
            updates.append("type=?")
            params.append(tx_type)
        if not updates:
            return True
        params.extend((rec_id, user_id))
        cur = conn.execute(
            f"UPDATE recurring_payments SET {', '.join(updates)} WHERE id=? AND user_id=?",
            params,
        )
        return cur.rowcount > 0


def delete_recurring(user_id: int, rec_id: int) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM recurring_runs WHERE recurring_id=?", (rec_id,))
        cur = conn.execute(
            "DELETE FROM recurring_payments WHERE id=? AND user_id=?", (rec_id, user_id)
        )
        return cur.rowcount > 0


def toggle_recurring_active(user_id: int, rec_id: int) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE recurring_payments SET active = 1 - active WHERE id=? AND user_id=?",
            (rec_id, user_id),
        )
        return cur.rowcount > 0


def get_all_active_recurring() -> list[sqlite3.Row]:
    """Для планировщика - все активные повторяющиеся платежи всех пользователей."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM recurring_payments WHERE active=1").fetchall()


def apply_due_recurring(recurring_id: int, today: str) -> bool:
    """Атомарно создаёт платёж за YYYY-MM. False, если уже создан или ещё рано."""
    period = today[:7]
    year, month, day = (int(part) for part in today.split("-"))
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM recurring_payments WHERE id=? AND active=1",
            (recurring_id,),
        ).fetchone()
        if not row or row["type"] not in TX_REGULAR_TYPES or day < row["day_of_month"]:
            return False
        try:
            conn.execute(
                "INSERT INTO recurring_runs(recurring_id, period) VALUES (?,?)",
                (recurring_id, period),
            )
        except sqlite3.IntegrityError:
            return False
        book_row = conn.execute(
            "SELECT active_book_id FROM users WHERE user_id=?",
            (row["user_id"],),
        ).fetchone()
        book_id = book_row["active_book_id"] if book_row else None
        conn.execute(
            """INSERT INTO transactions(
                   user_id, type, amount, category_id, payment_method_id, store,
                   description, receipt_id, op_date, op_time, created_at,
                   created_by, book_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["user_id"],
                row["type"],
                row["amount"],
                row["category_id"],
                row["payment_method_id"],
                None,
                row["description"],
                None,
                today,
                None,
                datetime.now(UTC).isoformat(),
                row["user_id"],
                book_id,
            ),
        )
        conn.execute(
            "UPDATE recurring_payments SET last_run_date=? WHERE id=?",
            (today, recurring_id),
        )
        return True


# ---------------------------------------------------------------------------
# Бюджеты по категориям
# ---------------------------------------------------------------------------

def set_budget(user_id: int, category_id: int | None, monthly_limit) -> None:
    user_id = _require_write(user_id)
    limit_tiyn = as_stored_tiyn(monthly_limit)
    with get_conn() as conn:
        if category_id is None:
            row = conn.execute(
                "SELECT id FROM category_budgets WHERE user_id=? AND category_id IS NULL",
                (user_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE category_budgets SET monthly_limit=? WHERE id=? AND user_id=?",
                    (limit_tiyn, row["id"], user_id),
                )
            else:
                conn.execute(
                    """INSERT INTO category_budgets(user_id, category_id, monthly_limit)
                       VALUES (?,?,?)""",
                    (user_id, None, limit_tiyn),
                )
            return
        if _owned_category_id(conn, user_id, category_id) != category_id:
            return
        row = conn.execute(
            "SELECT id FROM category_budgets WHERE user_id=? AND category_id=?",
            (user_id, category_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE category_budgets SET monthly_limit=? WHERE id=? AND user_id=?",
                (limit_tiyn, row["id"], user_id),
            )
        else:
            conn.execute(
                """INSERT INTO category_budgets(user_id, category_id, monthly_limit)
                   VALUES (?,?,?)""",
                (user_id, category_id, limit_tiyn),
            )


def get_budgets(user_id: int) -> list[sqlite3.Row]:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            """SELECT b.*, c.name AS category_name, c.emoji AS category_emoji FROM category_budgets b
               LEFT JOIN categories c ON c.id = b.category_id
               WHERE b.user_id=?
               ORDER BY CASE WHEN b.category_id IS NULL THEN 0 ELSE 1 END, c.name""",
            (user_id,),
        ).fetchall()


def get_budget_for_category(user_id: int, category_id: int) -> sqlite3.Row | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM category_budgets WHERE user_id=? AND category_id=?",
            (user_id, category_id),
        ).fetchone()


def get_overall_budget(user_id: int) -> sqlite3.Row | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM category_budgets WHERE user_id=? AND category_id IS NULL",
            (user_id,),
        ).fetchone()


def delete_budget(user_id: int, budget_id: int) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM category_budgets WHERE id=? AND user_id=?", (budget_id, user_id)
        )
        return cur.rowcount > 0


def get_category_spent(user_id: int, category_id: int, date_from: str, date_to: str) -> int:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
               WHERE user_id=? AND category_id=? AND type='expense' AND op_date BETWEEN ? AND ?""",
            (user_id, category_id, date_from, date_to),
        ).fetchone()
        return int(row["total"] or 0)


def get_total_expense(user_id: int, date_from: str, date_to: str) -> int:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
               WHERE user_id=? AND type='expense' AND op_date BETWEEN ? AND ?""",
            (user_id, date_from, date_to),
        ).fetchone()
        return int(row["total"] or 0)


def get_goal_transfers_total(user_id: int, date_from: str, date_to: str) -> int:
    """Чистый приток в цели за период: пополнения минус снятия."""
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(goal_delta), 0) AS total FROM transactions
               WHERE user_id=? AND type='transfer' AND op_date BETWEEN ? AND ?""",
            (user_id, date_from, date_to),
        ).fetchone()
        return int(row["total"] or 0)


def get_goals_reserved(user_id: int) -> int:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(current_amount), 0) AS total FROM savings_goals WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return int(row["total"] or 0)


# ---------------------------------------------------------------------------
# Накопительные цели
# ---------------------------------------------------------------------------

def _parse_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return value


def _default_payment_id(conn, user_id: int) -> int | None:
    row = conn.execute(
        """SELECT id FROM payment_methods WHERE user_id=?
           ORDER BY CASE WHEN name='Наличные' THEN 0 ELSE 1 END, id
           LIMIT 1""",
        (user_id,),
    ).fetchone()
    return row["id"] if row else None


def _ensure_savings_category(conn, user_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM categories WHERE user_id=? AND name=?",
        (user_id, SAVINGS_CATEGORY_NAME),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO categories(user_id, name, emoji) VALUES (?,?,?)",
        (user_id, SAVINGS_CATEGORY_NAME, SAVINGS_CATEGORY_EMOJI),
    )
    return cur.lastrowid


def _insert_goal_transfer(
    conn,
    user_id: int,
    goal: sqlite3.Row,
    amount_tiyn: int,
    goal_delta: int,
    op_date: str,
    payment_method_id: int | None,
) -> int:
    category_id = _ensure_savings_category(conn, user_id)
    pay_id = _owned_payment_id(conn, user_id, payment_method_id) or _default_payment_id(conn, user_id)
    verb = "Пополнение цели" if goal_delta > 0 else "Снятие с цели"
    cur = conn.execute(
        """INSERT INTO transactions(
               user_id, type, amount, category_id, payment_method_id, store,
               description, receipt_id, goal_id, goal_delta, op_date, op_time, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            user_id,
            "transfer",
            amount_tiyn,
            category_id,
            pay_id,
            None,
            f"{verb}: {goal['name']}",
            None,
            goal["id"],
            goal_delta,
            op_date,
            None,
            datetime.now(UTC).isoformat(),
        ),
    )
    return cur.lastrowid


def create_goal(user_id: int, name: str, target_amount, deadline: str | None = None) -> int:
    user_id = _require_write(user_id)
    name = name.strip()[:64]
    target_tiyn = as_stored_tiyn(target_amount)
    deadline = _parse_iso_date(deadline)
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO savings_goals(
                   user_id, name, target_amount, current_amount, deadline, created_at
               ) VALUES (?,?,?,0,?,?)""",
            (user_id, name, target_tiyn, deadline, datetime.now(UTC).isoformat()),
        )
        return cur.lastrowid


def get_goals(user_id: int) -> list[sqlite3.Row]:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM savings_goals WHERE user_id=? ORDER BY created_at", (user_id,)
        ).fetchall()


def get_goal_by_id(user_id: int, goal_id: int) -> sqlite3.Row | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM savings_goals WHERE id=? AND user_id=?", (goal_id, user_id)
        ).fetchone()


def update_goal(
    user_id: int,
    goal_id: int,
    *,
    name: str | None = None,
    target_amount=None,
    deadline: str | None = None,
    clear_deadline: bool = False,
) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM savings_goals WHERE id=? AND user_id=?",
            (goal_id, user_id),
        ).fetchone()
        if not row:
            return False
        updates: list[str] = []
        params: list = []
        if name is not None:
            cleaned = name.strip()[:64]
            if not cleaned:
                return False
            updates.append("name=?")
            params.append(cleaned)
        if target_amount is not None:
            updates.append("target_amount=?")
            params.append(as_stored_tiyn(target_amount))
        if clear_deadline:
            updates.append("deadline=?")
            params.append(None)
        elif deadline is not None:
            parsed = _parse_iso_date(deadline)
            if parsed is None:
                return False
            updates.append("deadline=?")
            params.append(parsed)
        if not updates:
            return True
        params.extend((goal_id, user_id))
        cur = conn.execute(
            f"UPDATE savings_goals SET {', '.join(updates)} WHERE id=? AND user_id=?",
            params,
        )
        return cur.rowcount > 0


def contribute_to_goal(
    user_id: int,
    goal_id: int,
    amount,
    op_date: str | None = None,
    payment_method_id: int | None = None,
) -> int | None:
    """Пополнение: перевод + прогресс в одной транзакции БД. Возвращает id операции."""
    user_id = _require_write(user_id)
    amount_tiyn = as_stored_tiyn(amount)
    op_date = op_date or user_today(user_id).isoformat()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        goal = conn.execute(
            "SELECT * FROM savings_goals WHERE id=? AND user_id=?",
            (goal_id, user_id),
        ).fetchone()
        if not goal:
            return None
        tx_id = _insert_goal_transfer(
            conn, user_id, goal, amount_tiyn, amount_tiyn, op_date, payment_method_id
        )
        conn.execute(
            "UPDATE savings_goals SET current_amount = current_amount + ? WHERE id=? AND user_id=?",
            (amount_tiyn, goal_id, user_id),
        )
        return tx_id


def withdraw_from_goal(
    user_id: int,
    goal_id: int,
    amount,
    op_date: str | None = None,
    payment_method_id: int | None = None,
) -> tuple[int | None, str]:
    """Снятие с цели. Не даёт уйти в минус. (tx_id, 'ok') или (None, причина)."""
    user_id = _require_write(user_id)
    try:
        amount_tiyn = as_stored_tiyn(amount)
    except MoneyError:
        return None, "bad_amount"
    op_date = op_date or user_today(user_id).isoformat()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        goal = conn.execute(
            "SELECT * FROM savings_goals WHERE id=? AND user_id=?",
            (goal_id, user_id),
        ).fetchone()
        if not goal:
            return None, "not_found"
        if int(goal["current_amount"]) < amount_tiyn:
            return None, "insufficient"
        tx_id = _insert_goal_transfer(
            conn, user_id, goal, amount_tiyn, -amount_tiyn, op_date, payment_method_id
        )
        cur = conn.execute(
            """UPDATE savings_goals SET current_amount = current_amount - ?
               WHERE id=? AND user_id=? AND current_amount >= ?""",
            (amount_tiyn, goal_id, user_id, amount_tiyn),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Не удалось атомарно снять с цели")
        return tx_id, "ok"


def delete_goal(user_id: int, goal_id: int) -> bool:
    """Удаляет цель. Остаток возвращается переводом, история операций сохраняется."""
    user_id = _require_write(user_id)
    op_date = user_today(user_id).isoformat()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        goal = conn.execute(
            "SELECT * FROM savings_goals WHERE id=? AND user_id=?",
            (goal_id, user_id),
        ).fetchone()
        if not goal:
            return False
        remaining = int(goal["current_amount"] or 0)
        if remaining > 0:
            _insert_goal_transfer(
                conn, user_id, goal, remaining, -remaining, op_date, None
            )
        cur = conn.execute(
            "DELETE FROM savings_goals WHERE id=? AND user_id=?", (goal_id, user_id)
        )
        return cur.rowcount > 0


def get_category_name(user_id: int, category_id: int) -> str | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM categories WHERE id=? AND user_id=?", (category_id, user_id)
        ).fetchone()
        return row["name"] if row else None


# ---------------------------------------------------------------------------
# Язык и настройка автосводки
# ---------------------------------------------------------------------------

def get_user_language(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT language FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["language"] if row and row["language"] else "ru"


def get_user_timezone(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT timezone FROM users WHERE user_id=?", (user_id,)).fetchone()
    name = row["timezone"] if row else None
    return normalize_timezone(name)


def set_user_timezone(user_id: int, tz_name: str) -> bool:
    if tz_name not in TIMEZONE_CHOICES:
        return False
    with get_conn() as conn:
        cur = conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz_name, user_id))
        return cur.rowcount > 0


def user_now(user_id: int, now: datetime | None = None) -> datetime:
    return now_in_tz(get_user_timezone(user_id), now)


def user_today(user_id: int, now: datetime | None = None) -> date:
    return today_in_tz(get_user_timezone(user_id), now)


def set_user_language(user_id: int, lang: str) -> None:
    from i18n import normalize_lang

    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET language=? WHERE user_id=?",
            (normalize_lang(lang), user_id),
        )


def is_onboarded(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT onboarded FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return bool(row and row["onboarded"])


def mark_onboarded(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET onboarded=1 WHERE user_id=?", (user_id,))


def get_privacy_accepted_version(user_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT privacy_accepted_version FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row["privacy_accepted_version"] if row else None


def accept_privacy_policy(user_id: int, version: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE users SET privacy_accepted_version=?, onboarded=1
               WHERE user_id=?""",
            (version, user_id),
        )


def get_digest_frequency(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT digest_frequency FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["digest_frequency"] if row and row["digest_frequency"] else "off"


def set_digest_frequency(user_id: int, freq: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET digest_frequency=? WHERE user_id=?", (freq, user_id))


def get_users_for_digest() -> list[sqlite3.Row]:
    """Все пользователи, у кого автосводка включена (не 'off')."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_id, digest_frequency, digest_last_sent FROM users WHERE digest_frequency != 'off'"
        ).fetchall()


def mark_digest_sent(user_id: int, sent_date: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET digest_last_sent=? WHERE user_id=?", (sent_date, user_id))


# ---------------------------------------------------------------------------
# Обучение категоризации (запоминание слово -> категория)
# ---------------------------------------------------------------------------

def learn_category(user_id: int, keyword: str, category_id: int) -> None:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        if _owned_category_id(conn, user_id, category_id) != category_id:
            return
        conn.execute(
            """INSERT INTO learned_categories(user_id, keyword, category_id) VALUES (?,?,?)
               ON CONFLICT(user_id, keyword) DO UPDATE SET category_id=excluded.category_id""",
            (user_id, keyword.strip().lower(), category_id),
        )


def get_learned_category_name(user_id: int, keyword: str) -> str | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT c.name FROM learned_categories lc
               JOIN categories c ON c.id = lc.category_id
               WHERE lc.user_id=? AND lc.keyword=?""",
            (user_id, keyword.strip().lower()),
        ).fetchone()
        return row["name"] if row else None


# ---------------------------------------------------------------------------
# Эмодзи категорий
# ---------------------------------------------------------------------------

def set_category_emoji(user_id: int, category_id: int, emoji: str) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE categories SET emoji=? WHERE id=? AND user_id=?", (emoji, category_id, user_id)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Настройки: импорт из банков, напоминание о простое, активность, бэкап
# ---------------------------------------------------------------------------

def get_settings(user_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        return conn.execute(
            """SELECT language, digest_frequency, bank_import_enabled, idle_reminder_enabled,
                      last_activity_date, backup_enabled, backup_last_sent, timezone,
                      onboarded, privacy_accepted_version
               FROM users WHERE user_id=?""",
            (user_id,),
        ).fetchone()


def set_bank_import_enabled(user_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET bank_import_enabled=? WHERE user_id=?", (int(enabled), user_id))


def set_idle_reminder_enabled(user_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET idle_reminder_enabled=? WHERE user_id=?", (int(enabled), user_id))


def set_backup_enabled(user_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET backup_enabled=? WHERE user_id=?", (int(enabled), user_id))


def touch_activity(user_id: int, today: str) -> None:
    """Отмечает, что пользователь сегодня что-то делал - сбрасывает счётчик простоя."""
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_activity_date=? WHERE user_id=?", (today, user_id))


def get_users_for_idle_check() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT user_id, last_activity_date, idle_reminder_last_sent FROM users
               WHERE idle_reminder_enabled=1"""
        ).fetchall()


def mark_idle_reminder_sent(user_id: int, sent_date: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET idle_reminder_last_sent=? WHERE user_id=?", (sent_date, user_id))


def get_users_for_backup() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT user_id, backup_last_sent FROM users
               WHERE backup_enabled=1"""
        ).fetchall()


def mark_backup_sent(user_id: int, sent_date: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET backup_last_sent=? WHERE user_id=?", (sent_date, user_id))


def get_users_bank_import_enabled() -> set[int]:
    with get_conn() as conn:
        return {r["user_id"] for r in conn.execute("SELECT user_id FROM users WHERE bank_import_enabled=1")}


def try_consume_gemini_quota(
    user_id: int, day: str, limit: int | None = None
) -> tuple[bool, int]:
    """Атомарно занимает слот распознавания. BEGIN IMMEDIATE закрывает гонку."""
    cap = gemini_daily_limit(user_id) if limit is None else limit
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT count FROM gemini_usage WHERE user_id=? AND day=?",
            (user_id, day),
        ).fetchone()
        used = int(row["count"]) if row else 0
        if used >= cap:
            return False, used
        conn.execute(
            """INSERT INTO gemini_usage(user_id, day, count) VALUES (?,?,1)
               ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1""",
            (user_id, day),
        )
        return True, used + 1


def refund_gemini_quota(user_id: int, day: str) -> int:
    """Возвращает слот, если Gemini упал сетью/JSON, а не распознал пустой чек."""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT count FROM gemini_usage WHERE user_id=? AND day=?",
            (user_id, day),
        ).fetchone()
        used = int(row["count"]) if row else 0
        if used <= 0:
            return 0
        conn.execute(
            "UPDATE gemini_usage SET count = count - 1 WHERE user_id=? AND day=? AND count > 0",
            (user_id, day),
        )
        return used - 1


def get_gemini_quota_used(user_id: int, day: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count FROM gemini_usage WHERE user_id=? AND day=?",
            (user_id, day),
        ).fetchone()
        return int(row["count"]) if row else 0


def get_gemini_usage_total(day: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS n FROM gemini_usage WHERE day=?",
            (day,),
        ).fetchone()
        return int(row["n"] or 0)


def _remember_fingerprint(
    conn, user_id: int, kind: str, fingerprint: str, seen_date: str
) -> None:
    conn.execute(
        """INSERT INTO source_fingerprints(user_id, kind, fingerprint, first_seen, last_seen)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, kind, fingerprint) DO UPDATE SET last_seen=excluded.last_seen""",
        (user_id, kind, fingerprint, seen_date, seen_date),
    )


def find_fingerprint(user_id: int, kind: str, fingerprints: list[str]) -> str | None:
    """Возвращает last_seen ближайшего известного отпечатка."""
    user_id = scope_user(user_id)
    marks = [item for item in fingerprints if item]
    if not marks:
        return None
    placeholders = ",".join("?" for _ in marks)
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT last_seen FROM source_fingerprints
                WHERE user_id=? AND kind=? AND fingerprint IN ({placeholders})
                ORDER BY last_seen DESC LIMIT 1""",
            (user_id, kind, *marks),
        ).fetchone()
        return row["last_seen"] if row else None


def count_import_duplicates(user_id: int, rows: list[dict], fallback_date: str) -> int:
    user_id = scope_user(user_id)
    if not rows:
        return 0
    marks = [
        import_row_fingerprint(
            row.get("date") or fallback_date,
            as_stored_tiyn(row["amount"]),
            row.get("description"),
        )
        for row in rows
        if row.get("amount")
    ]
    if not marks:
        return 0
    placeholders = ",".join("?" for _ in marks)
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS n FROM source_fingerprints
                WHERE user_id=? AND kind='import' AND fingerprint IN ({placeholders})""",
            (user_id, *marks),
        ).fetchone()
        return int(row["n"] or 0)


# ---------------------------------------------------------------------------
# Временное состояние диалога (черновики, шаг FSM, позиция в списке)
# ---------------------------------------------------------------------------

def save_state(scope: str, key: str, payload, user_id: int | None = None) -> None:
    """Сохраняет любое JSON-сериализуемое значение под парой (scope, key)."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO app_state(scope, key, user_id, payload, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(scope, key) DO UPDATE SET
                   user_id=excluded.user_id,
                   payload=excluded.payload,
                   updated_at=excluded.updated_at""",
            (scope, key, user_id, json.dumps(payload, ensure_ascii=False),
             datetime.now(UTC).isoformat()),
        )


def load_state(scope: str, key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM app_state WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return default


def pop_state(scope: str, key: str, default=None):
    """Читает и сразу удаляет - для одноразовых черновиков (чек, импорт)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM app_state WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
        conn.execute("DELETE FROM app_state WHERE scope=? AND key=?", (scope, key))
    if not row:
        return default
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return default


def commit_import_draft(
    user_id: int,
    import_id: str,
    payment_method_id: int | None,
    fallback_date: str,
) -> int | None:
    """Атомарно забирает черновик импорта и пишет все строки одним commit.

    Возвращает число операций или None, если черновик уже забрали.
    """
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT payload FROM app_state
               WHERE scope='import_draft' AND key=? AND user_id=?""",
            (import_id, user_id),
        ).fetchone()
        if not row:
            return None
        rows = json.loads(row["payload"])
        if not isinstance(rows, list) or not rows:
            conn.execute(
                "DELETE FROM app_state WHERE scope='import_draft' AND key=? AND user_id=?",
                (import_id, user_id),
            )
            return 0
        payment_method_id = _owned_payment_id(conn, user_id, payment_method_id)
        now = datetime.now(UTC).isoformat()
        for item in rows:
            tx_type = item.get("type", "expense")
            if tx_type not in TX_ALL_TYPES:
                tx_type = "expense"
            category_id = None
            cat_name = item.get("category")
            if cat_name:
                cat = conn.execute(
                    "SELECT id FROM categories WHERE user_id=? AND name=?",
                    (user_id, cat_name),
                ).fetchone()
                category_id = cat["id"] if cat else None
            category_id = _owned_category_id(conn, user_id, category_id)

            # Способ оплаты из файла берём только если такой уже есть у
            # пользователя: импорт не должен молча плодить новые карты.
            row_payment_id = payment_method_id
            pm_name = item.get("payment")
            if pm_name:
                pm = conn.execute(
                    "SELECT id FROM payment_methods WHERE user_id=? AND name=?",
                    (user_id, pm_name),
                ).fetchone()
                if pm:
                    row_payment_id = pm["id"]

            conn.execute(
                """INSERT INTO transactions(user_id, type, amount, category_id,
                   payment_method_id, store, description, receipt_id, op_date,
                   op_time, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    tx_type,
                    as_stored_tiyn(item["amount"]),
                    category_id,
                    row_payment_id,
                    (item.get("store") or None),
                    item.get("description"),
                    None,
                    item.get("date") or fallback_date,
                    None,
                    now,
                ),
            )
            _remember_fingerprint(
                conn,
                user_id,
                "import",
                import_row_fingerprint(
                    item.get("date") or fallback_date,
                    as_stored_tiyn(item["amount"]),
                    item.get("description"),
                ),
                item.get("date") or fallback_date,
            )
        deleted = conn.execute(
            "DELETE FROM app_state WHERE scope='import_draft' AND key=? AND user_id=?",
            (import_id, user_id),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("Черновик импорта изменился во время сохранения")
        return len(rows)


def delete_state(scope: str, key: str, user_id: int | None = None) -> None:
    with get_conn() as conn:
        if user_id is None:
            conn.execute("DELETE FROM app_state WHERE scope=? AND key=?", (scope, key))
        else:
            conn.execute(
                "DELETE FROM app_state WHERE scope=? AND key=? AND user_id=?",
                (scope, key, user_id),
            )


def purge_stale_state(max_age_days: int = 7) -> int:
    """Чистит брошенные черновики и старые FSM-сессии, чтобы таблица не росла
    бесконечно из-за диалогов, которые никто не довёл до конца."""
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM app_state WHERE updated_at < ?", (cutoff,))
        return cur.rowcount


def get_transactions_by_receipt(user_id: int, receipt_id: int) -> list[sqlite3.Row]:
    """Позиции конкретного чека. Заменяет словарь в памяти: связь товара с
    чеком и так хранится в transactions.receipt_id."""
    user_id = scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            """SELECT t.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE t.user_id=? AND t.receipt_id=?
               ORDER BY t.id""",
            (user_id, receipt_id),
        ).fetchall()


# ---------------------------------------------------------------------------
# Полное удаление данных пользователя (опасная зона)
# ---------------------------------------------------------------------------

def _wipe_user_books(conn, user_id: int) -> None:
    owned = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM books WHERE owner_user_id=?", (user_id,)
        ).fetchall()
    ]
    displaced = []
    if owned:
        placeholders = ",".join("?" for _ in owned)
        displaced = conn.execute(
            f"""SELECT user_id FROM book_members
                WHERE book_id IN ({placeholders}) AND user_id!=?""",
            (*owned, user_id),
        ).fetchall()
        conn.execute(
            f"DELETE FROM book_invites WHERE book_id IN ({placeholders})",
            owned,
        )
        conn.execute(
            f"DELETE FROM audit_log WHERE book_id IN ({placeholders})",
            owned,
        )
        conn.execute(
            f"DELETE FROM book_members WHERE book_id IN ({placeholders})",
            owned,
        )
        conn.execute(f"DELETE FROM books WHERE id IN ({placeholders})", owned)
    conn.execute("DELETE FROM book_members WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM book_invites WHERE created_by=?", (user_id,))
    conn.execute("DELETE FROM payments WHERE user_id=?", (user_id,))
    conn.execute("UPDATE users SET active_book_id=NULL WHERE user_id=?", (user_id,))
    for member in displaced:
        personal = conn.execute(
            "SELECT id FROM books WHERE owner_user_id=? ORDER BY id LIMIT 1",
            (member["user_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE users SET active_book_id=? WHERE user_id=?",
            (personal["id"] if personal else None, member["user_id"]),
        )


def _user_owns_book(conn, user_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM books WHERE owner_user_id=? LIMIT 1",
        (user_id,),
    ).fetchone()
    return bool(row)


def _wipe_user_finance(
    conn, user_id: int, *, delete_user: bool, wipe_books: bool = True
) -> None:
    if wipe_books:
        _wipe_user_books(conn, user_id)
    conn.execute(
        """DELETE FROM recurring_runs WHERE recurring_id IN (
               SELECT id FROM recurring_payments WHERE user_id=?
           )""",
        (user_id,),
    )
    for table in (
        "transactions", "receipts", "recurring_payments", "category_budgets",
        "savings_goals", "learned_categories", "categories", "payment_methods",
        "app_state",
        "gemini_usage",
        "source_fingerprints",
    ):
        conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
    if delete_user:
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        return
    if wipe_books or not _user_owns_book(conn, user_id):
        _create_personal_book(conn, user_id)


def delete_all_user_data(user_id: int) -> None:
    """Полностью стирает финансовые данные пользователя. Сам user_id тоже
    удаляется - при следующем /start всё пересоздастся с нуля (дефолтные
    категории/способы оплаты)."""
    with get_conn() as conn:
        _wipe_user_finance(conn, user_id, delete_user=True)


def count_user_data(user_id: int) -> int:
    """Сколько всего операций у пользователя - показываем перед удалением,
    чтобы человек понимал масштаб того, что стирает."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE user_id=?", (user_id,)).fetchone()
        return row["n"]


# ---------------------------------------------------------------------------
# Полный личный бэкап (для JSON-выгрузки, НЕ вся общая база - см. backup.py)
# ---------------------------------------------------------------------------

def get_all_user_rows(user_id: int) -> dict:
    with get_conn() as conn:
        def rows_as_dicts(query, params=(user_id,)):
            return [dict(r) for r in conn.execute(query, params)]

        receipts = rows_as_dicts(
            """SELECT r.id, r.store, r.receipt_date, r.receipt_time, p.name AS payment_method,
                      r.raw_text, r.created_at
               FROM receipts r
               LEFT JOIN payment_methods p ON p.id = r.payment_method_id
               WHERE r.user_id=?
               ORDER BY r.id"""
        )
        id_map = {row["id"]: index + 1 for index, row in enumerate(receipts)}
        for row in receipts:
            row["backup_id"] = id_map[row.pop("id")]

        goals = rows_as_dicts(
            """SELECT id, name, target_amount, current_amount, deadline, created_at
               FROM savings_goals WHERE user_id=? ORDER BY id"""
        )
        goal_id_map = {row["id"]: index + 1 for index, row in enumerate(goals)}
        for row in goals:
            original_id = row.pop("id")
            row["backup_id"] = goal_id_map[original_id]

        transactions = rows_as_dicts(
            """SELECT t.type, t.amount, c.name AS category, p.name AS payment_method,
                      t.store, t.description, t.receipt_id, t.goal_id, t.goal_delta,
                      t.op_date, t.op_time, t.created_at
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE t.user_id=?
               ORDER BY t.id"""
        )
        for tx in transactions:
            tx["receipt_backup_id"] = id_map.get(tx.pop("receipt_id"))
            goal_id = tx.pop("goal_id")
            tx["goal_backup_id"] = goal_id_map.get(goal_id)

        settings = conn.execute(
            """SELECT language, digest_frequency, bank_import_enabled,
                      idle_reminder_enabled, backup_enabled, last_activity_date, timezone,
                      onboarded, privacy_accepted_version
               FROM users WHERE user_id=?""",
            (user_id,),
        ).fetchone()

        # backup_id + runs: без этого восстановление создавало бы повторы с
        # новыми id и пустой историей recurring_runs, а планировщик списывал
        # бы уже сработавший в этом месяце платёж повторно (см. apply_due_recurring).
        recurring = rows_as_dicts(
            """SELECT r.id, r.type, r.amount, c.name AS category, p.name AS payment_method,
                      r.description, r.day_of_month, r.last_run_date, r.active
               FROM recurring_payments r
               LEFT JOIN categories c ON c.id = r.category_id
               LEFT JOIN payment_methods p ON p.id = r.payment_method_id
               WHERE r.user_id=?
               ORDER BY r.id"""
        )
        recurring_id_map = {row["id"]: index + 1 for index, row in enumerate(recurring)}
        runs_by_recurring_id: dict[int, list[str]] = {}
        for run_row in conn.execute(
            """SELECT rr.recurring_id, rr.period FROM recurring_runs rr
               JOIN recurring_payments r ON r.id = rr.recurring_id
               WHERE r.user_id=?
               ORDER BY rr.recurring_id, rr.period""",
            (user_id,),
        ):
            runs_by_recurring_id.setdefault(run_row["recurring_id"], []).append(run_row["period"])
        for row in recurring:
            original_id = row.pop("id")
            row["backup_id"] = recurring_id_map[original_id]
            row["runs"] = runs_by_recurring_id.get(original_id, [])

        return {
            "version": BACKUP_VERSION,
            "amount_unit": "tiyn",
            "settings": dict(settings) if settings else {},
            "categories": rows_as_dicts("SELECT name, emoji FROM categories WHERE user_id=? ORDER BY id"),
            "payment_methods": rows_as_dicts("SELECT name FROM payment_methods WHERE user_id=? ORDER BY id"),
            "receipts": receipts,
            "transactions": transactions,
            "recurring": recurring,
            "budgets": rows_as_dicts(
                """SELECT c.name AS category, b.monthly_limit FROM category_budgets b
                   LEFT JOIN categories c ON c.id = b.category_id WHERE b.user_id=?"""
            ),
            "goals": goals,
            "learned": rows_as_dicts(
                """SELECT lc.keyword, c.name AS category
                   FROM learned_categories lc
                   JOIN categories c ON c.id = lc.category_id
                   WHERE lc.user_id=?"""
            ),
        }


def restore_user_backup(user_id: int, data: dict) -> dict[str, int]:
    """Полностью заменяет финансовые данные пользователя из бэкапа одной транзакцией."""
    if not isinstance(data, dict):
        raise ValueError("Некорректный бэкап")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            conn.execute(
                """INSERT INTO users(user_id, username, created_at,
                   idle_reminder_enabled, backup_enabled) VALUES (?,?,?,?,?)""",
                (user_id, None, datetime.now(UTC).isoformat(), 0, 0),
            )
        _wipe_user_finance(conn, user_id, delete_user=False, wipe_books=False)

        amount_unit = data.get("amount_unit")
        if not amount_unit:
            amount_unit = "tiyn" if int(data.get("version") or 0) >= 4 else "tenge"

        settings = data.get("settings") or {}
        conn.execute(
            """UPDATE users SET language=?, digest_frequency=?, bank_import_enabled=?,
                   idle_reminder_enabled=?, backup_enabled=?, last_activity_date=?, timezone=?,
                   onboarded=?, privacy_accepted_version=?
               WHERE user_id=?""",
            (
                settings.get("language") or "ru",
                settings.get("digest_frequency") or "off",
                int(bool(settings.get("bank_import_enabled"))),
                int(bool(settings.get("idle_reminder_enabled"))),
                int(bool(settings.get("backup_enabled"))),
                settings.get("last_activity_date"),
                normalize_timezone(settings.get("timezone")),
                int(bool(settings.get("onboarded"))),
                settings.get("privacy_accepted_version"),
                user_id,
            ),
        )

        for cat in data.get("categories") or []:
            conn.execute(
                "INSERT OR IGNORE INTO categories(user_id, name, emoji) VALUES (?,?,?)",
                (user_id, cat["name"], cat.get("emoji") or "🏷"),
            )
        for pm in data.get("payment_methods") or []:
            conn.execute(
                "INSERT OR IGNORE INTO payment_methods(user_id, name) VALUES (?,?)",
                (user_id, pm["name"] if isinstance(pm, dict) else pm),
            )

        def cat_id(name):
            if not name:
                return _owned_category_id(conn, user_id, None)
            row = conn.execute(
                "SELECT id FROM categories WHERE user_id=? AND name=?", (user_id, name)
            ).fetchone()
            return row["id"] if row else _owned_category_id(conn, user_id, None)

        def pm_id(name):
            if not name:
                return None
            row = conn.execute(
                "SELECT id FROM payment_methods WHERE user_id=? AND name=?", (user_id, name)
            ).fetchone()
            return row["id"] if row else None

        receipt_map: dict[int, int] = {}
        for receipt in data.get("receipts") or []:
            cur = conn.execute(
                """INSERT INTO receipts(user_id, store, receipt_date, receipt_time,
                   payment_method_id, raw_text, created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    user_id,
                    receipt.get("store"),
                    receipt.get("receipt_date") or receipt.get("date"),
                    receipt.get("receipt_time") or receipt.get("time"),
                    pm_id(receipt.get("payment_method")),
                    receipt.get("raw_text") or "",
                    receipt.get("created_at") or datetime.now(UTC).isoformat(),
                ),
            )
            backup_id = receipt.get("backup_id")
            if backup_id is not None:
                receipt_map[int(backup_id)] = cur.lastrowid

        now = datetime.now(UTC).isoformat()
        goal_map: dict[int, int] = {}
        for goal in data.get("goals") or []:
            cur = conn.execute(
                """INSERT INTO savings_goals(
                       user_id, name, target_amount, current_amount, deadline, created_at
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    user_id,
                    goal["name"],
                    backup_amount_to_tiyn(goal["target_amount"], amount_unit),
                    backup_amount_to_tiyn(goal.get("current_amount") or 0, amount_unit),
                    _parse_iso_date(goal.get("deadline")),
                    goal.get("created_at") or now,
                ),
            )
            backup_goal_id = goal.get("backup_id")
            if backup_goal_id is not None:
                goal_map[int(backup_goal_id)] = cur.lastrowid

        tx_count = 0
        for tx in data.get("transactions") or []:
            receipt_ref = tx.get("receipt_backup_id")
            tx_type = tx.get("type") if tx.get("type") in TX_ALL_TYPES else "expense"
            goal_ref = tx.get("goal_backup_id")
            goal_id = goal_map.get(int(goal_ref)) if goal_ref else None
            goal_delta = tx.get("goal_delta")
            if goal_delta is not None:
                goal_delta = backup_signed_amount_to_tiyn(goal_delta, amount_unit)
            conn.execute(
                """INSERT INTO transactions(
                       user_id, type, amount, category_id, payment_method_id, store,
                       description, receipt_id, goal_id, goal_delta, op_date, op_time, created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    tx_type,
                    backup_amount_to_tiyn(tx["amount"], amount_unit),
                    cat_id(tx.get("category")),
                    pm_id(tx.get("payment_method")),
                    tx.get("store"),
                    tx.get("description"),
                    receipt_map.get(int(receipt_ref)) if receipt_ref else None,
                    goal_id,
                    goal_delta,
                    tx["op_date"],
                    tx.get("op_time"),
                    tx.get("created_at") or now,
                ),
            )
            tx_count += 1

        rec_count = 0
        for rec in data.get("recurring") or []:
            cur = conn.execute(
                """INSERT INTO recurring_payments
                   (user_id, type, amount, category_id, payment_method_id, description,
                    day_of_month, last_run_date, active, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    rec.get("type") if rec.get("type") in ("expense", "income") else "expense",
                    backup_amount_to_tiyn(rec["amount"], amount_unit),
                    cat_id(rec.get("category")),
                    pm_id(rec.get("payment_method")),
                    rec.get("description"),
                    int(rec["day_of_month"]),
                    rec.get("last_run_date"),
                    int(rec.get("active", 1)),
                    now,
                ),
            )
            new_recurring_id = cur.lastrowid
            # Периоды, когда платёж уже сработал (backup v3+). Без этого
            # apply_due_recurring не видит истории для нового id и в этом же
            # месяце списывает платёж повторно, дублируя транзакцию.
            for period in rec.get("runs") or []:
                conn.execute(
                    "INSERT INTO recurring_runs(recurring_id, period) VALUES (?,?)",
                    (new_recurring_id, period),
                )
            rec_count += 1

        for budget in data.get("budgets") or []:
            cat_name = budget.get("category")
            if cat_name:
                cid = cat_id(cat_name)
                if not cid:
                    continue
            else:
                cid = None
            conn.execute(
                """INSERT INTO category_budgets(user_id, category_id, monthly_limit)
                   VALUES (?,?,?)""",
                (user_id, cid, backup_amount_to_tiyn(budget["monthly_limit"], amount_unit)),
            )

        for learned in data.get("learned") or []:
            cid = cat_id(learned.get("category"))
            if cid and learned.get("keyword"):
                conn.execute(
                    """INSERT INTO learned_categories(user_id, keyword, category_id) VALUES (?,?,?)
                       ON CONFLICT(user_id, keyword) DO UPDATE SET category_id=excluded.category_id""",
                    (user_id, learned["keyword"], cid),
                )

        return {
            "transactions": tx_count,
            "receipts": len(receipt_map),
            "recurring": rec_count,
            "goals": len(data.get("goals") or []),
        }


# ---------------------------------------------------------------------------
# Семейная книга, тарифы, здоровье планировщиков
# ---------------------------------------------------------------------------

SCHEDULER_INTERVALS = {
    "recurring": 6 * 3600,
    "digest": 6 * 3600,
    "idle": 12 * 3600,
    "backup": 24 * 3600,
    "sqlite": 24 * 3600,
}


def active_book_id(user_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active_book_id FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row and row["active_book_id"]:
            return int(row["active_book_id"])
        personal = conn.execute(
            "SELECT id FROM books WHERE owner_user_id=? ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
        return int(personal["id"]) if personal else None


def scope_user(user_id: int) -> int:
    """Владелец активной книги. Данные книги хранятся под его user_id."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(b.owner_user_id, u.user_id) AS owner_id
               FROM users u
               LEFT JOIN books b ON b.id = u.active_book_id
               WHERE u.user_id=?""",
            (user_id,),
        ).fetchone()
        if row and row["owner_id"]:
            return int(row["owner_id"])
    return int(user_id)


def book_role(user_id: int, book_id: int | None = None) -> str | None:
    bid = book_id if book_id is not None else active_book_id(user_id)
    if not bid:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT role FROM book_members WHERE book_id=? AND user_id=?",
            (bid, user_id),
        ).fetchone()
        return row["role"] if row else None


def can_write_book(user_id: int) -> bool:
    role = book_role(user_id)
    if role is None:
        return scope_user(user_id) == user_id
    return role in ("owner", "write")


def _require_write(user_id: int) -> int:
    if not can_write_book(user_id):
        raise PermissionError("read-only book")
    return scope_user(user_id)


def write_audit(
    book_id: int | None,
    actor_id: int,
    action: str,
    entity: str | None = None,
    entity_id: int | None = None,
    conn=None,
) -> None:
    if not book_id:
        return
    payload = (
        book_id,
        actor_id,
        action,
        entity,
        entity_id,
        datetime.now(UTC).isoformat(),
    )
    sql = """INSERT INTO audit_log(
                 book_id, actor_user_id, action, entity, entity_id, created_at
             ) VALUES (?,?,?,?,?,?)"""
    if conn is not None:
        conn.execute(sql, payload)
        return
    with get_conn() as owned:
        owned.execute(sql, payload)


def list_user_books(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT b.id, b.name, b.owner_user_id, m.role,
                      CASE WHEN b.id = u.active_book_id THEN 1 ELSE 0 END AS is_active
               FROM book_members m
               JOIN books b ON b.id = m.book_id
               JOIN users u ON u.user_id = m.user_id
               WHERE m.user_id=?
               ORDER BY is_active DESC, b.id""",
            (user_id,),
        ).fetchall()


def list_book_members(book_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT m.user_id, m.role, m.joined_at, u.username
               FROM book_members m
               LEFT JOIN users u ON u.user_id = m.user_id
               WHERE m.book_id=?
               ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'write' THEN 1 ELSE 2 END,
                        m.joined_at""",
            (book_id,),
        ).fetchall()


def personal_book_id(user_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM books WHERE owner_user_id=? ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
        return int(row["id"]) if row else None


def create_book_invite(user_id: int, role: str = "write") -> str:
    if role not in ("write", "read"):
        raise ValueError("invalid invite role")
    book_id = active_book_id(user_id)
    if book_role(user_id, book_id) != "owner":
        raise PermissionError("only owner can invite")
    expires = datetime.now(UTC) + timedelta(hours=BOOK_INVITE_HOURS)
    with get_conn() as conn:
        for _ in range(8):
            code = secrets.token_hex(4).upper()
            try:
                conn.execute(
                    """INSERT INTO book_invites(
                           code, book_id, created_by, role, expires_at
                       ) VALUES (?,?,?,?,?)""",
                    (code, book_id, user_id, role, expires.isoformat()),
                )
            except sqlite3.IntegrityError:
                continue
            write_audit(book_id, user_id, "invite_create", "invite", None, conn=conn)
            return code
    raise RuntimeError("could not allocate invite code")


def join_book_invite(user_id: int, code: str) -> str:
    """ok / switched / expired / used / invalid / own."""
    raw = (code or "").strip().upper()
    if raw.startswith("JOIN_"):
        raw = raw[5:]
    if not raw:
        return "invalid"
    now = datetime.now(UTC)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        invite = conn.execute(
            "SELECT * FROM book_invites WHERE code=?", (raw,)
        ).fetchone()
        if not invite:
            return "invalid"
        expires_at = datetime.fromisoformat(invite["expires_at"])
        if expires_at <= now:
            return "expired"
        if invite["used_by"] is not None:
            return "used"
        book = conn.execute(
            "SELECT owner_user_id FROM books WHERE id=?",
            (invite["book_id"],),
        ).fetchone()
        if not book:
            return "invalid"
        if int(book["owner_user_id"]) == user_id:
            conn.execute(
                "UPDATE users SET active_book_id=? WHERE user_id=?",
                (invite["book_id"], user_id),
            )
            return "own"
        existing = conn.execute(
            "SELECT role FROM book_members WHERE book_id=? AND user_id=?",
            (invite["book_id"], user_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET active_book_id=? WHERE user_id=?",
                (invite["book_id"], user_id),
            )
            return "switched"
        joined = now.isoformat()
        conn.execute(
            """INSERT INTO book_members(book_id, user_id, role, joined_at)
               VALUES (?,?,?,?)""",
            (invite["book_id"], user_id, invite["role"], joined),
        )
        conn.execute(
            """UPDATE book_invites SET used_by=?, used_at=? WHERE code=?""",
            (user_id, joined, raw),
        )
        conn.execute(
            "UPDATE users SET active_book_id=? WHERE user_id=?",
            (invite["book_id"], user_id),
        )
        write_audit(
            invite["book_id"], user_id, "member_join", "user", user_id, conn=conn
        )
        return "ok"


def switch_active_book(user_id: int, book_id: int) -> bool:
    if book_role(user_id, book_id) is None:
        return False
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET active_book_id=? WHERE user_id=?",
            (book_id, user_id),
        )
    return True


def leave_active_book(user_id: int) -> bool:
    book_id = active_book_id(user_id)
    role = book_role(user_id, book_id)
    if not book_id or role in (None, "owner"):
        return False
    personal = personal_book_id(user_id)
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM book_members WHERE book_id=? AND user_id=?",
            (book_id, user_id),
        )
        if personal:
            conn.execute(
                "UPDATE users SET active_book_id=? WHERE user_id=?",
                (personal, user_id),
            )
        write_audit(book_id, user_id, "member_leave", "user", user_id, conn=conn)
    return True


def _plan_is_active(row) -> bool:
    if not row or row["plan"] != PLAN_PRO:
        return False
    until = row["plan_until"]
    if not until:
        return True
    try:
        return datetime.fromisoformat(until) > datetime.now(UTC)
    except ValueError:
        return False


def is_pro(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT plan, plan_until FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return _plan_is_active(row)


def user_plan_info(user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT plan, plan_until FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    active = is_pro(user_id)
    return {
        "plan": PLAN_PRO if active else PLAN_FREE,
        "plan_until": row["plan_until"] if row and active else None,
        "stored_plan": row["plan"] if row else PLAN_FREE,
        "limit": GEMINI_PRO_LIMIT if active else GEMINI_DAILY_LIMIT,
    }


def gemini_daily_limit(user_id: int) -> int:
    return GEMINI_PRO_LIMIT if is_pro(user_id) else GEMINI_DAILY_LIMIT


def _grant_pro_on_conn(conn, user_id: int, days: int | None = None) -> str:
    duration = PRO_DURATION_DAYS if days is None else max(1, int(days))
    now = datetime.now(UTC)
    row = conn.execute(
        "SELECT plan, plan_until FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    start = now
    if row and row["plan"] == PLAN_PRO and row["plan_until"]:
        try:
            current = datetime.fromisoformat(row["plan_until"])
            if current > now:
                start = current
        except ValueError:
            pass
    until = (start + timedelta(days=duration)).isoformat()
    conn.execute(
        "UPDATE users SET plan=?, plan_until=? WHERE user_id=?",
        (PLAN_PRO, until, user_id),
    )
    return until


def grant_pro(user_id: int, days: int | None = None) -> str:
    with get_conn() as conn:
        return _grant_pro_on_conn(conn, user_id, days)


def revoke_pro(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET plan=?, plan_until=NULL WHERE user_id=?",
            (PLAN_FREE, user_id),
        )


def record_stars_payment(payment_id: str, user_id: int, stars: int) -> bool:
    """True, если платёж новый и Pro выдан. Повтор того же id — False."""
    charge_id = (payment_id or "").strip()
    if not charge_id:
        return False
    now = datetime.now(UTC).isoformat()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO payments(
                       id, user_id, provider, stars, plan, status, paid_at, created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    charge_id,
                    user_id,
                    "stars",
                    int(stars),
                    PLAN_PRO,
                    "paid",
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT user_id, status FROM payments WHERE id=?",
                (charge_id,),
            ).fetchone()
            if row and row["status"] == "paid":
                user = conn.execute(
                    "SELECT plan, plan_until FROM users WHERE user_id=?",
                    (row["user_id"],),
                ).fetchone()
                if not _plan_is_active(user):
                    _grant_pro_on_conn(conn, row["user_id"])
            return False
        _grant_pro_on_conn(conn, user_id)
        return True


def refund_stars_payment(payment_id: str) -> bool:
    """Помечает Stars-платёж возвращённым и снимает Pro."""
    charge_id = (payment_id or "").strip()
    if not charge_id:
        return False
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, status FROM payments WHERE id=?",
            (charge_id,),
        ).fetchone()
        if not row or row["status"] == "refunded":
            return False
        conn.execute(
            "UPDATE payments SET status='refunded' WHERE id=?",
            (charge_id,),
        )
        conn.execute(
            "UPDATE users SET plan=?, plan_until=NULL WHERE user_id=?",
            (PLAN_FREE, row["user_id"]),
        )
        return True


def touch_scheduler(name: str) -> None:
    save_state(
        "scheduler",
        name,
        {"ts": datetime.now(UTC).isoformat()},
    )


def scheduler_health() -> dict:
    now = datetime.now(UTC)
    items: dict[str, dict] = {}
    stale = False
    for name, interval in SCHEDULER_INTERVALS.items():
        payload = load_state("scheduler", name, {})
        ts = payload.get("ts") if isinstance(payload, dict) else None
        if not ts:
            items[name] = {"ok": True, "age_sec": None, "status": "unknown"}
            continue
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds()
        except ValueError:
            items[name] = {"ok": False, "age_sec": None, "status": "invalid"}
            stale = True
            continue
        ok = age <= interval * 2 + 300
        items[name] = {
            "ok": ok,
            "age_sec": int(age),
            "status": "ok" if ok else "stale",
        }
        if not ok:
            stale = True
    return {"schedulers": items, "schedulers_ok": not stale}