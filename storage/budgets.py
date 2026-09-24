"""SQLite persistence operations for budgets."""
from __future__ import annotations

import sqlite3

from money import as_stored_tiyn

from storage import books, catalog, transactions
from storage.conn import get_conn

def set_budget(user_id: int, category_id: int | None, monthly_limit) -> None:
    user_id = books._require_write(user_id)
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
        if catalog._owned_category_id(conn, user_id, category_id) != category_id:
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


def get_budget_by_id(user_id: int, budget_id: int) -> sqlite3.Row | None:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            """SELECT b.*, c.name AS category_name, c.emoji AS category_emoji
               FROM category_budgets b
               LEFT JOIN categories c ON c.id = b.category_id
               WHERE b.id=? AND b.user_id=?""",
            (budget_id, user_id),
        ).fetchone()


def get_budgets(user_id: int) -> list[sqlite3.Row]:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            """SELECT b.*, c.name AS category_name, c.emoji AS category_emoji FROM category_budgets b
               LEFT JOIN categories c ON c.id = b.category_id
               WHERE b.user_id=?
               ORDER BY CASE WHEN b.category_id IS NULL THEN 0 ELSE 1 END, c.name""",
            (user_id,),
        ).fetchall()


def get_budget_for_category(user_id: int, category_id: int) -> sqlite3.Row | None:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM category_budgets WHERE user_id=? AND category_id=?",
            (user_id, category_id),
        ).fetchone()


def get_overall_budget(user_id: int) -> sqlite3.Row | None:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM category_budgets WHERE user_id=? AND category_id IS NULL",
            (user_id,),
        ).fetchone()


def delete_budget(user_id: int, budget_id: int) -> bool:
    user_id = books._require_write(user_id)
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM category_budgets WHERE id=? AND user_id=?", (budget_id, user_id)
        )
        return cur.rowcount > 0


def get_category_spent(user_id: int, category_id: int, date_from: str, date_to: str) -> int:
    actor_id = user_id
    user_id = books.scope_user(user_id)
    book_sql, book_params = transactions._book_clause(actor_id, "book_id")
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
               WHERE user_id=? AND category_id=? AND type='expense'
                 AND op_date BETWEEN ? AND ? AND {book_sql}""",
            (user_id, category_id, date_from, date_to, *book_params),
        ).fetchone()
        return int(row["total"] or 0)


def get_total_expense(user_id: int, date_from: str, date_to: str) -> int:
    actor_id = user_id
    user_id = books.scope_user(user_id)
    book_sql, book_params = transactions._book_clause(actor_id, "book_id")
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(amount), 0) AS total FROM transactions
               WHERE user_id=? AND type='expense' AND op_date BETWEEN ? AND ? AND {book_sql}""",
            (user_id, date_from, date_to, *book_params),
        ).fetchone()
        return int(row["total"] or 0)


def get_goal_transfers_total(user_id: int, date_from: str, date_to: str) -> int:
    """Чистый приток в цели за период: пополнения минус снятия."""
    actor_id = user_id
    user_id = books.scope_user(user_id)
    book_sql, book_params = transactions._book_clause(actor_id, "book_id")
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(goal_delta), 0) AS total FROM transactions
               WHERE user_id=? AND type='transfer' AND op_date BETWEEN ? AND ? AND {book_sql}""",
            (user_id, date_from, date_to, *book_params),
        ).fetchone()
        return int(row["total"] or 0)


def get_goals_reserved(user_id: int) -> int:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(current_amount), 0) AS total FROM savings_goals WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return int(row["total"] or 0)
