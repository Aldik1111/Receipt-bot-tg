"""Фрагмент слоя БД. Имена соседних модулей подставляются из фасада db.py."""
from __future__ import annotations

from storage.conn import *  # noqa: F403

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
                book_id INTEGER,
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
    _ensure_column(conn, "recurring_payments", "book_id", "book_id INTEGER")
    conn.execute(
        """UPDATE recurring_payments SET book_id = (
               SELECT u.active_book_id FROM users u
               WHERE u.user_id = recurring_payments.user_id
           )
           WHERE book_id IS NULL"""
    )
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
