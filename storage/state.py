"""SQLite persistence operations for state."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3

from fingerprints import import_row_fingerprint
from money import as_stored_tiyn

from storage import books, catalog, quota, transactions
from storage.conn import TX_ALL_TYPES, get_conn

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
        payment_method_id = catalog._owned_payment_id(conn, user_id, payment_method_id)
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
            category_id = catalog._owned_category_id(conn, user_id, category_id)

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
            quota._remember_fingerprint(
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
    actor_id = user_id
    user_id = books.scope_user(user_id)
    book_sql, book_params = transactions._book_clause(actor_id, "t.book_id")
    with get_conn() as conn:
        return conn.execute(
            f"""SELECT t.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE t.user_id=? AND t.receipt_id=? AND {book_sql}
               ORDER BY t.id""",
            (user_id, receipt_id, *book_params),
        ).fetchall()
