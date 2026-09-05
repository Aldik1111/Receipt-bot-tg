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

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from config import DB_PATH, DEFAULT_CATEGORIES, DEFAULT_CATEGORY_EMOJIS, DEFAULT_PAYMENT_METHODS

PROTECTED_CATEGORY_NAME = "Прочее"


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
            CREATE INDEX IF NOT EXISTS idx_tx_user_category_date
                ON transactions(user_id, category_id, op_date);

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
        _ensure_column(conn, "categories", "emoji", "emoji TEXT NOT NULL DEFAULT '🏷'")

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


def get_categories(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM categories WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall()


def add_category(user_id: int, name: str, emoji: str = "🏷") -> int:
    name = name.strip()[:64]
    emoji = (emoji or "🏷")[:8]
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


def get_payment_method_id_by_name(user_id: int, name: str) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM payment_methods WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        return row["id"] if row else None


def get_payment_methods(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM payment_methods WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall()


def add_payment_method(user_id: int, name: str) -> int:
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
        category_id = _owned_category_id(conn, user_id, category_id)
        payment_method_id = _owned_payment_id(conn, user_id, payment_method_id)
        cur = conn.execute(
            """INSERT INTO transactions(user_id, type, amount, category_id,
               payment_method_id, store, description, receipt_id, op_date, op_time, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                tx_type,
                round(float(amount), 2),
                category_id,
                payment_method_id,
                store,
                (description or "")[:500] or None,
                receipt_id,
                op_date,
                op_time,
                datetime.now(UTC).isoformat(),
            ),
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

        payment = conn.execute(
            """SELECT id FROM payment_methods WHERE user_id=?
               ORDER BY CASE WHEN name='Наличные' THEN 0 ELSE 1 END, id
               LIMIT 1""",
            (user_id,),
        ).fetchone()
        payment_method_id = payment["id"] if payment else None
        now = datetime.now(UTC).isoformat()

        receipt_id = conn.execute(
            """INSERT INTO receipts(user_id, store, receipt_date, receipt_time,
               payment_method_id, raw_text, created_at) VALUES (?,?,?,?,?,?,?)""",
            (
                user_id,
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
                (user_id, item.get("category") or PROTECTED_CATEGORY_NAME),
            ).fetchone()
            category_id = category["id"] if category else _owned_category_id(conn, user_id, None)
            if category_id is not None:
                touched_categories.add(category_id)

            conn.execute(
                """INSERT INTO transactions(user_id, type, amount, category_id,
                   payment_method_id, store, description, receipt_id, op_date,
                   op_time, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    tx_type,
                    round(float(item["price"]), 2),
                    category_id,
                    payment_method_id,
                    parsed.get("store"),
                    item.get("name"),
                    receipt_id,
                    op_date,
                    parsed.get("time"),
                    now,
                ),
            )

        conn.execute(
            "UPDATE users SET last_activity_date=? WHERE user_id=?",
            (fallback_date, user_id),
        )
        deleted = conn.execute(
            """DELETE FROM app_state
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (draft_id, user_id),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("Черновик чека изменился во время сохранения")

        return receipt_id, touched_categories


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
) -> list[sqlite3.Row]:
    """Список операций, новые сверху, с пагинацией и фильтрами в SQL."""
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
) -> int:
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
        owned = _owned_payment_id(conn, user_id, payment_method_id)
        if payment_method_id is not None and owned != payment_method_id:
            return False
        cur = conn.execute(
            "UPDATE transactions SET payment_method_id=? WHERE id=? AND user_id=?",
            (owned, tx_id, user_id),
        )
        return cur.rowcount > 0


def delete_transaction(user_id: int, tx_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT receipt_id FROM transactions WHERE id=? AND user_id=?",
            (tx_id, user_id),
        ).fetchone()
        if not row:
            return False
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
    user_id: int, tx_type: str, amount: float, category_id: int | None,
    payment_method_id: int | None, description: str, day_of_month: int,
) -> int:
    with get_conn() as conn:
        category_id = _owned_category_id(conn, user_id, category_id)
        payment_method_id = _owned_payment_id(conn, user_id, payment_method_id)
        cur = conn.execute(
            """INSERT INTO recurring_payments
               (user_id, type, amount, category_id, payment_method_id, description,
                day_of_month, last_run_date, active, created_at)
               VALUES (?,?,?,?,?,?,?,NULL,1,?)""",
            (user_id, tx_type, amount, category_id, payment_method_id, description,
             day_of_month, datetime.now(UTC).isoformat()),
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
        conn.execute("DELETE FROM recurring_runs WHERE recurring_id=?", (rec_id,))
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
        if not row or day < row["day_of_month"]:
            return False
        try:
            conn.execute(
                "INSERT INTO recurring_runs(recurring_id, period) VALUES (?,?)",
                (recurring_id, period),
            )
        except sqlite3.IntegrityError:
            return False
        conn.execute(
            """INSERT INTO transactions(user_id, type, amount, category_id,
               payment_method_id, store, description, receipt_id, op_date,
               op_time, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["user_id"],
                row["type"],
                round(float(row["amount"]), 2),
                row["category_id"],
                row["payment_method_id"],
                None,
                row["description"],
                None,
                today,
                None,
                datetime.now(UTC).isoformat(),
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

def set_budget(user_id: int, category_id: int, monthly_limit: float) -> None:
    with get_conn() as conn:
        if _owned_category_id(conn, user_id, category_id) != category_id:
            return
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
    name = name.strip()[:64]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO savings_goals(user_id, name, target_amount, current_amount, created_at) VALUES (?,?,?,0,?)",
            (user_id, name, target_amount, datetime.now(UTC).isoformat()),
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
        if _owned_category_id(conn, user_id, category_id) != category_id:
            return
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
                      last_activity_date, backup_enabled, backup_last_sent
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
            if tx_type not in ("expense", "income"):
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
                    round(float(item["amount"]), 2),
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

def _wipe_user_finance(conn, user_id: int, *, delete_user: bool) -> None:
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
    ):
        conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
    if delete_user:
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))


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

        transactions = rows_as_dicts(
            """SELECT t.type, t.amount, c.name AS category, p.name AS payment_method,
                      t.store, t.description, t.receipt_id, t.op_date, t.op_time, t.created_at
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE t.user_id=?
               ORDER BY t.id"""
        )
        for tx in transactions:
            tx["receipt_backup_id"] = id_map.get(tx.pop("receipt_id"))

        settings = conn.execute(
            """SELECT language, digest_frequency, bank_import_enabled,
                      idle_reminder_enabled, backup_enabled, last_activity_date
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
            "version": 3,
            "settings": dict(settings) if settings else {},
            "categories": rows_as_dicts("SELECT name, emoji FROM categories WHERE user_id=? ORDER BY id"),
            "payment_methods": rows_as_dicts("SELECT name FROM payment_methods WHERE user_id=? ORDER BY id"),
            "receipts": receipts,
            "transactions": transactions,
            "recurring": recurring,
            "budgets": rows_as_dicts(
                """SELECT c.name AS category, b.monthly_limit FROM category_budgets b
                   JOIN categories c ON c.id = b.category_id WHERE b.user_id=?"""
            ),
            "goals": rows_as_dicts(
                "SELECT name, target_amount, current_amount, created_at FROM savings_goals WHERE user_id=?"
            ),
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
        _wipe_user_finance(conn, user_id, delete_user=False)

        settings = data.get("settings") or {}
        conn.execute(
            """UPDATE users SET language=?, digest_frequency=?, bank_import_enabled=?,
                   idle_reminder_enabled=?, backup_enabled=?, last_activity_date=?
               WHERE user_id=?""",
            (
                settings.get("language") or "ru",
                settings.get("digest_frequency") or "off",
                int(bool(settings.get("bank_import_enabled"))),
                int(bool(settings.get("idle_reminder_enabled"))),
                int(bool(settings.get("backup_enabled"))),
                settings.get("last_activity_date"),
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
        tx_count = 0
        for tx in data.get("transactions") or []:
            receipt_ref = tx.get("receipt_backup_id")
            conn.execute(
                """INSERT INTO transactions(user_id, type, amount, category_id,
                   payment_method_id, store, description, receipt_id, op_date,
                   op_time, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    tx.get("type") if tx.get("type") in ("expense", "income") else "expense",
                    round(float(tx["amount"]), 2),
                    cat_id(tx.get("category")),
                    pm_id(tx.get("payment_method")),
                    tx.get("store"),
                    tx.get("description"),
                    receipt_map.get(int(receipt_ref)) if receipt_ref else None,
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
                    round(float(rec["amount"]), 2),
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
            cid = cat_id(budget.get("category"))
            if cid:
                conn.execute(
                    """INSERT INTO category_budgets(user_id, category_id, monthly_limit)
                       VALUES (?,?,?)
                       ON CONFLICT(user_id, category_id) DO UPDATE SET monthly_limit=excluded.monthly_limit""",
                    (user_id, cid, round(float(budget["monthly_limit"]), 2)),
                )

        for goal in data.get("goals") or []:
            conn.execute(
                """INSERT INTO savings_goals(user_id, name, target_amount, current_amount, created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    user_id,
                    goal["name"],
                    round(float(goal["target_amount"]), 2),
                    round(float(goal.get("current_amount") or 0), 2),
                    goal.get("created_at") or now,
                ),
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