"""
Слой работы с базой данных (SQLite).

Схема:
  users            - пользователи бота
  categories       - категории расходов/доходов (свои у каждого пользователя)
  payment_methods  - способы оплаты (свои у каждого пользователя)
  receipts         - "шапка" чека (магазин, дата, время, способ оплаты)
  transactions     - отдельные операции (расход/доход). Если операция создана
                      из чека - привязана к receipts через receipt_id, и тогда
                      каждая строка чека = отдельная transaction (это даёт
                      точную статистику по категориям и товарам).
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import DB_PATH, DEFAULT_CATEGORIES, DEFAULT_CATEGORY_EMOJIS, DEFAULT_PAYMENT_METHODS


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, table: str, column: str, add_sql: str) -> None:
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {add_sql}")


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

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('expense', 'income')),
                amount REAL NOT NULL,
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
            );

            CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions(user_id, op_date);

            CREATE TABLE IF NOT EXISTS recurring_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('expense', 'income')),
                amount REAL NOT NULL,
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
                category_id INTEGER NOT NULL,
                monthly_limit REAL NOT NULL,
                UNIQUE(user_id, category_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS savings_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                target_amount REAL NOT NULL,
                current_amount REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
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
            """
        )
        _ensure_column(conn, "users", "language", "language TEXT NOT NULL DEFAULT 'ru'")
        _ensure_column(conn, "users", "digest_frequency", "digest_frequency TEXT NOT NULL DEFAULT 'off'")
        _ensure_column(conn, "users", "digest_last_sent", "digest_last_sent TEXT")
        _ensure_column(conn, "users", "bank_import_enabled", "bank_import_enabled INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "users", "idle_reminder_enabled", "idle_reminder_enabled INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "users", "last_activity_date", "last_activity_date TEXT")
        _ensure_column(conn, "users", "idle_reminder_last_sent", "idle_reminder_last_sent TEXT")
        _ensure_column(conn, "users", "backup_last_sent", "backup_last_sent TEXT")
        _ensure_column(conn, "categories", "emoji", "emoji TEXT NOT NULL DEFAULT '🏷'")


def ensure_user(user_id: int, username: str | None):
    """Создаёт пользователя и его дефолтные категории/способы оплаты, если его ещё нет."""
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return
        conn.execute(
            "INSERT INTO users(user_id, username, created_at) VALUES (?,?,?)",
            (user_id, username, datetime.utcnow().isoformat()),
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


def get_categories(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM categories WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall()


def add_category(user_id: int, name: str, emoji: str = "🏷") -> int:
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
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM categories WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        return row["id"] if row else None


def get_payment_methods(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM payment_methods WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall()


def add_payment_method(user_id: int, name: str) -> int:
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


def create_receipt(
    user_id: int,
    store: str | None,
    receipt_date: str | None,
    receipt_time: str | None,
    payment_method_id: int | None,
    raw_text: str,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO receipts(user_id, store, receipt_date, receipt_time,
               payment_method_id, raw_text, created_at) VALUES (?,?,?,?,?,?,?)""",
            (
                user_id,
                store,
                receipt_date,
                receipt_time,
                payment_method_id,
                raw_text,
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def add_transaction(
    user_id: int,
    tx_type: str,
    amount: float,
    category_id: int | None,
    payment_method_id: int | None,
    store: str | None,
    description: str | None,
    op_date: str,
    op_time: str | None = None,
    receipt_id: int | None = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO transactions(user_id, type, amount, category_id,
               payment_method_id, store, description, receipt_id, op_date, op_time, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                tx_type,
                amount,
                category_id,
                payment_method_id,
                store,
                description,
                receipt_id,
                op_date,
                op_time,
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def get_transactions(user_id: int, date_from: str, date_to: str) -> list[sqlite3.Row]:
    """date_from / date_to в формате YYYY-MM-DD, включительно."""
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
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET category_id=? WHERE id=? AND user_id=?",
            (category_id, tx_id, user_id),
        )
        return cur.rowcount > 0


def rename_category(user_id: int, cat_id: int, new_name: str) -> bool:
    """False, если категория с таким именем уже есть у пользователя."""
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "UPDATE categories SET name=? WHERE id=? AND user_id=?",
                (new_name, cat_id, user_id),
            )
        except sqlite3.IntegrityError:
            return False
        return cur.rowcount > 0


def count_transactions_for_category(user_id: int, cat_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE user_id=? AND category_id=?",
            (user_id, cat_id),
        ).fetchone()
        return row["n"]


def delete_category(user_id: int, cat_id: int, fallback_name: str = "Прочее") -> bool:
    """Удаляет категорию, предварительно перенеся все её транзакции в fallback
    (по умолчанию "Прочее"). Отказывает, если удаляют саму fallback-категорию."""
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
        cur = conn.execute(
            "DELETE FROM categories WHERE id=? AND user_id=?", (cat_id, user_id)
        )
        return cur.rowcount > 0


def rename_payment_method(user_id: int, pm_id: int, new_name: str) -> bool:
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
) -> list[sqlite3.Row]:
    """Список операций, новые сверху. Если даты не заданы - без ограничения по периоду."""
    with get_conn() as conn:
        if date_from and date_to:
            return conn.execute(
                """SELECT t.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
                   FROM transactions t
                   LEFT JOIN categories c ON c.id = t.category_id
                   LEFT JOIN payment_methods p ON p.id = t.payment_method_id
                   WHERE t.user_id=? AND t.op_date BETWEEN ? AND ?
                   ORDER BY t.op_date DESC, t.op_time DESC, t.id DESC
                   LIMIT ? OFFSET ?""",
                (user_id, date_from, date_to, limit, offset),
            ).fetchall()
        return conn.execute(
            """SELECT t.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE t.user_id=?
               ORDER BY t.op_date DESC, t.op_time DESC, t.id DESC
               LIMIT ? OFFSET ?""",
            (user_id, limit, offset),
        ).fetchall()


def count_all_transactions(
    user_id: int, date_from: str | None = None, date_to: str | None = None
) -> int:
    with get_conn() as conn:
        if date_from and date_to:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM transactions WHERE user_id=? AND op_date BETWEEN ? AND ?",
                (user_id, date_from, date_to),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM transactions WHERE user_id=?", (user_id,)
            ).fetchone()
        return row["n"]


def get_transaction_by_id(user_id: int, tx_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """SELECT t.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE t.id=? AND t.user_id=?""",
            (tx_id, user_id),
        ).fetchone()


def update_transaction_amount(user_id: int, tx_id: int, amount: float) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET amount=? WHERE id=? AND user_id=?",
            (amount, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_description(user_id: int, tx_id: int, description: str | None) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET description=? WHERE id=? AND user_id=?",
            (description, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_payment_method(user_id: int, tx_id: int, payment_method_id: int | None) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET payment_method_id=? WHERE id=? AND user_id=?",
            (payment_method_id, tx_id, user_id),
        )
        return cur.rowcount > 0


def delete_transaction(user_id: int, tx_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM transactions WHERE id=? AND user_id=?", (tx_id, user_id)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Повторяющиеся платежи
# ---------------------------------------------------------------------------

def add_recurring(
    user_id: int, tx_type: str, amount: float, category_id: int | None,
    payment_method_id: int | None, description: str, day_of_month: int,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO recurring_payments
               (user_id, type, amount, category_id, payment_method_id, description,
                day_of_month, last_run_date, active, created_at)
               VALUES (?,?,?,?,?,?,?,NULL,1,?)""",
            (user_id, tx_type, amount, category_id, payment_method_id, description,
             day_of_month, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def get_recurring_list(user_id: int) -> list[sqlite3.Row]:
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
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM recurring_payments WHERE id=? AND user_id=?", (rec_id, user_id)
        ).fetchone()


def delete_recurring(user_id: int, rec_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM recurring_payments WHERE id=? AND user_id=?", (rec_id, user_id)
        )
        return cur.rowcount > 0


def toggle_recurring_active(user_id: int, rec_id: int) -> bool:
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


def mark_recurring_run(rec_id: int, run_date: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE recurring_payments SET last_run_date=? WHERE id=?", (run_date, rec_id)
        )


# ---------------------------------------------------------------------------
# Бюджеты по категориям
# ---------------------------------------------------------------------------

def set_budget(user_id: int, category_id: int, monthly_limit: float) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO category_budgets(user_id, category_id, monthly_limit)
               VALUES (?,?,?)
               ON CONFLICT(user_id, category_id) DO UPDATE SET monthly_limit=excluded.monthly_limit""",
            (user_id, category_id, monthly_limit),
        )


def get_budgets(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT b.*, c.name AS category_name, c.emoji AS category_emoji FROM category_budgets b
               JOIN categories c ON c.id = b.category_id
               WHERE b.user_id=? ORDER BY c.name""",
            (user_id,),
        ).fetchall()


def get_budget_for_category(user_id: int, category_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM category_budgets WHERE user_id=? AND category_id=?",
            (user_id, category_id),
        ).fetchone()


def delete_budget(user_id: int, budget_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM category_budgets WHERE id=? AND user_id=?", (budget_id, user_id)
        )
        return cur.rowcount > 0


def get_category_spent(user_id: int, category_id: int, date_from: str, date_to: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
               WHERE user_id=? AND category_id=? AND type='expense' AND op_date BETWEEN ? AND ?""",
            (user_id, category_id, date_from, date_to),
        ).fetchone()
        return row["total"]


def get_total_expense(user_id: int, date_from: str, date_to: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
               WHERE user_id=? AND type='expense' AND op_date BETWEEN ? AND ?""",
            (user_id, date_from, date_to),
        ).fetchone()
        return row["total"]


# ---------------------------------------------------------------------------
# Накопительные цели
# ---------------------------------------------------------------------------

def create_goal(user_id: int, name: str, target_amount: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO savings_goals(user_id, name, target_amount, current_amount, created_at) VALUES (?,?,?,0,?)",
            (user_id, name, target_amount, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def get_goals(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM savings_goals WHERE user_id=? ORDER BY created_at", (user_id,)
        ).fetchall()


def get_goal_by_id(user_id: int, goal_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM savings_goals WHERE id=? AND user_id=?", (goal_id, user_id)
        ).fetchone()


def contribute_to_goal(user_id: int, goal_id: int, amount: float) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE savings_goals SET current_amount = current_amount + ? WHERE id=? AND user_id=?",
            (amount, goal_id, user_id),
        )
        return cur.rowcount > 0


def delete_goal(user_id: int, goal_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM savings_goals WHERE id=? AND user_id=?", (goal_id, user_id)
        )
        return cur.rowcount > 0


def get_category_name(user_id: int, category_id: int) -> str | None:
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


def set_user_language(user_id: int, lang: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET language=? WHERE user_id=?", (lang, user_id))


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
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO learned_categories(user_id, keyword, category_id) VALUES (?,?,?)
               ON CONFLICT(user_id, keyword) DO UPDATE SET category_id=excluded.category_id""",
            (user_id, keyword.strip().lower(), category_id),
        )


def get_learned_category_name(user_id: int, keyword: str) -> str | None:
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
                      last_activity_date, backup_last_sent
               FROM users WHERE user_id=?""",
            (user_id,),
        ).fetchone()


def set_bank_import_enabled(user_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET bank_import_enabled=? WHERE user_id=?", (int(enabled), user_id))


def set_idle_reminder_enabled(user_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET idle_reminder_enabled=? WHERE user_id=?", (int(enabled), user_id))


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
        return conn.execute("SELECT user_id, backup_last_sent FROM users").fetchall()


def mark_backup_sent(user_id: int, sent_date: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET backup_last_sent=? WHERE user_id=?", (sent_date, user_id))


def get_users_bank_import_enabled() -> set[int]:
    with get_conn() as conn:
        return {r["user_id"] for r in conn.execute("SELECT user_id FROM users WHERE bank_import_enabled=1")}


# ---------------------------------------------------------------------------
# Полное удаление данных пользователя (опасная зона)
# ---------------------------------------------------------------------------

def delete_all_user_data(user_id: int) -> None:
    """Полностью стирает финансовые данные пользователя. Сам user_id тоже
    удаляется - при следующем /start всё пересоздастся с нуля (дефолтные
    категории/способы оплаты)."""
    with get_conn() as conn:
        for table in (
            "transactions", "receipts", "recurring_payments", "category_budgets",
            "savings_goals", "learned_categories", "categories", "payment_methods",
        ):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))


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
        def rows_as_dicts(query):
            return [dict(r) for r in conn.execute(query, (user_id,))]

        return {
            "categories": rows_as_dicts("SELECT name, emoji FROM categories WHERE user_id=?"),
            "payment_methods": rows_as_dicts("SELECT name FROM payment_methods WHERE user_id=?"),
            "transactions": rows_as_dicts(
                """SELECT t.type, t.amount, c.name AS category, p.name AS payment_method,
                          t.store, t.description, t.op_date, t.op_time
                   FROM transactions t
                   LEFT JOIN categories c ON c.id = t.category_id
                   LEFT JOIN payment_methods p ON p.id = t.payment_method_id
                   WHERE t.user_id=?"""
            ),
            "budgets": rows_as_dicts(
                """SELECT c.name AS category, b.monthly_limit FROM category_budgets b
                   JOIN categories c ON c.id = b.category_id WHERE b.user_id=?"""
            ),
            "goals": rows_as_dicts("SELECT name, target_amount, current_amount FROM savings_goals WHERE user_id=?"),
        }