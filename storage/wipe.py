"""SQLite persistence operations for wipe."""
from __future__ import annotations

from storage import schema
from storage.conn import get_conn

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
        schema._create_personal_book(conn, user_id)


def list_owned_book_member_ids(user_id: int) -> list[int]:
    """Члены книг, которыми владеет user_id, кроме него самого."""
    with get_conn() as conn:
        owned = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM books WHERE owner_user_id=?", (user_id,)
            ).fetchall()
        ]
        if not owned:
            return []
        placeholders = ",".join("?" for _ in owned)
        rows = conn.execute(
            f"""SELECT DISTINCT user_id FROM book_members
                WHERE book_id IN ({placeholders}) AND user_id!=?""",
            (*owned, user_id),
        ).fetchall()
        return [int(row["user_id"]) for row in rows]


def delete_all_user_data(user_id: int) -> list[int]:
    """Полностью стирает финансовые данные пользователя. Сам user_id тоже
    удаляется - при следующем /start всё пересоздастся с нуля (дефолтные
    категории/способы оплаты). Возвращает id членов снесённых книг."""
    members = list_owned_book_member_ids(user_id)
    with get_conn() as conn:
        _wipe_user_finance(conn, user_id, delete_user=True)
    return members


def count_user_data(user_id: int) -> int:
    """Сколько всего операций у пользователя - показываем перед удалением,
    чтобы человек понимал масштаб того, что стирает."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE user_id=?", (user_id,)).fetchone()
        return row["n"]
