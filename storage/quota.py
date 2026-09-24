"""SQLite persistence operations for quota."""
from __future__ import annotations

from fingerprints import import_row_fingerprint
from money import as_stored_tiyn

from storage import books
from storage.conn import get_conn

def try_consume_gemini_quota(
    user_id: int, day: str, limit: int | None = None
) -> tuple[bool, int]:
    """Атомарно занимает слот распознавания. BEGIN IMMEDIATE закрывает гонку."""
    cap = books.gemini_daily_limit(user_id) if limit is None else limit
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
    user_id = books.scope_user(user_id)
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
    user_id = books.scope_user(user_id)
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
