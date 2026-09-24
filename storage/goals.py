"""SQLite persistence operations for goals."""
from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from money import MoneyError, as_stored_tiyn

from storage import books, catalog, users
from storage.conn import SAVINGS_CATEGORY_EMOJI, SAVINGS_CATEGORY_NAME, get_conn

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
    pay_id = catalog._owned_payment_id(conn, user_id, payment_method_id) or _default_payment_id(conn, user_id)
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
    user_id = books._require_write(user_id)
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
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM savings_goals WHERE user_id=? ORDER BY created_at", (user_id,)
        ).fetchall()


def get_goal_by_id(user_id: int, goal_id: int) -> sqlite3.Row | None:
    user_id = books.scope_user(user_id)
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
    user_id = books._require_write(user_id)
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
    user_id = books._require_write(user_id)
    amount_tiyn = as_stored_tiyn(amount)
    op_date = op_date or users.user_today(user_id).isoformat()
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
    user_id = books._require_write(user_id)
    try:
        amount_tiyn = as_stored_tiyn(amount)
    except MoneyError:
        return None, "bad_amount"
    op_date = op_date or users.user_today(user_id).isoformat()
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
    user_id = books._require_write(user_id)
    op_date = users.user_today(user_id).isoformat()
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
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM categories WHERE id=? AND user_id=?", (category_id, user_id)
        ).fetchone()
        return row["name"] if row else None
