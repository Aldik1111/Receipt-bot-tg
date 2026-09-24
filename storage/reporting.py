"""SQL aggregates for reports in the actor's active book."""

import sqlite3

from storage import books as _books
from storage import conn as _conn
from storage import transactions as _transactions


def get_summary_totals(user_id: int, date_from: str, date_to: str) -> dict[str, int]:
    """Return expense and income totals in tiyn for the inclusive period."""
    owner_id = _books.scope_user(user_id)
    book_sql, book_params = _transactions._book_clause(user_id, "t.book_id")
    with _conn.get_conn() as conn:
        row = conn.execute(
            f"""SELECT
                    COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END), 0) AS expense,
                    COALESCE(SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END), 0) AS income
                FROM transactions t
                WHERE t.user_id=? AND t.op_date BETWEEN ? AND ?
                  AND {book_sql} AND t.type IN ('expense', 'income')""",
            (owner_id, date_from, date_to, *book_params),
        ).fetchone()
    return {"expense": int(row["expense"]), "income": int(row["income"])}


def get_category_totals(user_id: int, date_from: str, date_to: str) -> list[sqlite3.Row]:
    """Return expense groups, largest first; keep missing category labels null."""
    owner_id = _books.scope_user(user_id)
    book_sql, book_params = _transactions._book_clause(user_id, "t.book_id")
    with _conn.get_conn() as conn:
        return conn.execute(
            f"""SELECT c.name AS category_name, c.emoji AS category_emoji,
                       SUM(t.amount) AS amount
                FROM transactions t
                LEFT JOIN categories c ON c.id=t.category_id
                WHERE t.user_id=? AND t.op_date BETWEEN ? AND ?
                  AND {book_sql} AND t.type='expense'
                GROUP BY c.name, c.emoji
                ORDER BY amount DESC, c.name, c.emoji""",
            (owner_id, date_from, date_to, *book_params),
        ).fetchall()
