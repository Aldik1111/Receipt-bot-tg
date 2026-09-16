"""Фрагмент слоя БД. Имена соседних модулей подставляются из фасада db.py."""
from __future__ import annotations

from storage.conn import *  # noqa: F403

def add_recurring(
    user_id: int, tx_type: str, amount, category_id: int | None,
    payment_method_id: int | None, description: str, day_of_month: int,
) -> int:
    book_id = active_book_id(user_id)
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
                day_of_month, last_run_date, active, created_at, book_id)
               VALUES (?,?,?,?,?,?,?,NULL,1,?,?)""",
            (
                user_id, tx_type, amount_tiyn, category_id, payment_method_id,
                description, day_of_month, datetime.now(UTC).isoformat(), book_id,
            ),
        )
        return cur.lastrowid


def get_recurring_list(user_id: int) -> list[sqlite3.Row]:
    actor_id = user_id
    user_id = scope_user(user_id)
    book_sql, book_params = _book_clause(actor_id, "r.book_id")
    with get_conn() as conn:
        return conn.execute(
            f"""SELECT r.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
               FROM recurring_payments r
               LEFT JOIN categories c ON c.id = r.category_id
               LEFT JOIN payment_methods p ON p.id = r.payment_method_id
               WHERE r.user_id=? AND {book_sql} ORDER BY r.day_of_month""",
            (user_id, *book_params),
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
        book_id = row["book_id"] if row["book_id"] else None
        if not book_id:
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
