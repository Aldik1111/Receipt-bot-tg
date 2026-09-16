"""Фрагмент слоя БД. Имена соседних модулей подставляются из фасада db.py."""
from __future__ import annotations

from storage.conn import *  # noqa: F403

# ---------------------------------------------------------------------------
# Семейная книга, тарифы, здоровье планировщиков
# ---------------------------------------------------------------------------

SCHEDULER_INTERVALS = {
    "recurring": 6 * 3600,
    "digest": 6 * 3600,
    "idle": 12 * 3600,
    "backup": 24 * 3600,
    "sqlite": 24 * 3600,
}


def active_book_id(user_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active_book_id FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row and row["active_book_id"]:
            return int(row["active_book_id"])
        personal = conn.execute(
            "SELECT id FROM books WHERE owner_user_id=? ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
        return int(personal["id"]) if personal else None


def scope_user(user_id: int) -> int:
    """Владелец активной книги. Данные книги хранятся под его user_id."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(b.owner_user_id, u.user_id) AS owner_id
               FROM users u
               LEFT JOIN books b ON b.id = u.active_book_id
               WHERE u.user_id=?""",
            (user_id,),
        ).fetchone()
        if row and row["owner_id"]:
            return int(row["owner_id"])
    return int(user_id)


def book_role(user_id: int, book_id: int | None = None) -> str | None:
    bid = book_id if book_id is not None else active_book_id(user_id)
    if not bid:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT role FROM book_members WHERE book_id=? AND user_id=?",
            (bid, user_id),
        ).fetchone()
        return row["role"] if row else None


def can_write_book(user_id: int) -> bool:
    role = book_role(user_id)
    if role is None:
        return scope_user(user_id) == user_id
    return role in ("owner", "write")


def _require_write(user_id: int) -> int:
    if not can_write_book(user_id):
        raise PermissionError("read-only book")
    return scope_user(user_id)


def write_audit(
    book_id: int | None,
    actor_id: int,
    action: str,
    entity: str | None = None,
    entity_id: int | None = None,
    conn=None,
) -> None:
    if not book_id:
        return
    payload = (
        book_id,
        actor_id,
        action,
        entity,
        entity_id,
        datetime.now(UTC).isoformat(),
    )
    sql = """INSERT INTO audit_log(
                 book_id, actor_user_id, action, entity, entity_id, created_at
             ) VALUES (?,?,?,?,?,?)"""
    if conn is not None:
        conn.execute(sql, payload)
        return
    with get_conn() as owned:
        owned.execute(sql, payload)


def list_user_books(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT b.id, b.name, b.owner_user_id, m.role,
                      CASE WHEN b.id = u.active_book_id THEN 1 ELSE 0 END AS is_active
               FROM book_members m
               JOIN books b ON b.id = m.book_id
               JOIN users u ON u.user_id = m.user_id
               WHERE m.user_id=?
               ORDER BY is_active DESC, b.id""",
            (user_id,),
        ).fetchall()


def list_book_members(book_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT m.user_id, m.role, m.joined_at, u.username
               FROM book_members m
               LEFT JOIN users u ON u.user_id = m.user_id
               WHERE m.book_id=?
               ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'write' THEN 1 ELSE 2 END,
                        m.joined_at""",
            (book_id,),
        ).fetchall()


def personal_book_id(user_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM books WHERE owner_user_id=? ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
        return int(row["id"]) if row else None


def create_book_invite(user_id: int, role: str = "write") -> str:
    if role not in ("write", "read"):
        raise ValueError("invalid invite role")
    book_id = active_book_id(user_id)
    if book_role(user_id, book_id) != "owner":
        raise PermissionError("only owner can invite")
    expires = datetime.now(UTC) + timedelta(hours=BOOK_INVITE_HOURS)
    with get_conn() as conn:
        for _ in range(8):
            code = secrets.token_hex(4).upper()
            try:
                conn.execute(
                    """INSERT INTO book_invites(
                           code, book_id, created_by, role, expires_at
                       ) VALUES (?,?,?,?,?)""",
                    (code, book_id, user_id, role, expires.isoformat()),
                )
            except sqlite3.IntegrityError:
                continue
            write_audit(book_id, user_id, "invite_create", "invite", None, conn=conn)
            return code
    raise RuntimeError("could not allocate invite code")


def join_book_invite(user_id: int, code: str) -> str:
    """ok / switched / expired / used / invalid / own."""
    raw = (code or "").strip().upper()
    if raw.startswith("JOIN_"):
        raw = raw[5:]
    if not raw:
        return "invalid"
    now = datetime.now(UTC)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        invite = conn.execute(
            "SELECT * FROM book_invites WHERE code=?", (raw,)
        ).fetchone()
        if not invite:
            return "invalid"
        expires_at = datetime.fromisoformat(invite["expires_at"])
        if expires_at <= now:
            return "expired"
        if invite["used_by"] is not None:
            return "used"
        book = conn.execute(
            "SELECT owner_user_id FROM books WHERE id=?",
            (invite["book_id"],),
        ).fetchone()
        if not book:
            return "invalid"
        if int(book["owner_user_id"]) == user_id:
            conn.execute(
                "UPDATE users SET active_book_id=? WHERE user_id=?",
                (invite["book_id"], user_id),
            )
            return "own"
        existing = conn.execute(
            "SELECT role FROM book_members WHERE book_id=? AND user_id=?",
            (invite["book_id"], user_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET active_book_id=? WHERE user_id=?",
                (invite["book_id"], user_id),
            )
            return "switched"
        joined = now.isoformat()
        conn.execute(
            """INSERT INTO book_members(book_id, user_id, role, joined_at)
               VALUES (?,?,?,?)""",
            (invite["book_id"], user_id, invite["role"], joined),
        )
        conn.execute(
            """UPDATE book_invites SET used_by=?, used_at=? WHERE code=?""",
            (user_id, joined, raw),
        )
        conn.execute(
            "UPDATE users SET active_book_id=? WHERE user_id=?",
            (invite["book_id"], user_id),
        )
        write_audit(
            invite["book_id"], user_id, "member_join", "user", user_id, conn=conn
        )
        return "ok"


def switch_active_book(user_id: int, book_id: int) -> bool:
    if book_role(user_id, book_id) is None:
        return False
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET active_book_id=? WHERE user_id=?",
            (book_id, user_id),
        )
    return True


def leave_active_book(user_id: int) -> bool:
    book_id = active_book_id(user_id)
    role = book_role(user_id, book_id)
    if not book_id or role in (None, "owner"):
        return False
    personal = personal_book_id(user_id)
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM book_members WHERE book_id=? AND user_id=?",
            (book_id, user_id),
        )
        if personal:
            conn.execute(
                "UPDATE users SET active_book_id=? WHERE user_id=?",
                (personal, user_id),
            )
        write_audit(book_id, user_id, "member_leave", "user", user_id, conn=conn)
    return True


def _plan_is_active(row) -> bool:
    if not row or row["plan"] != PLAN_PRO:
        return False
    until = row["plan_until"]
    if not until:
        return True
    try:
        return datetime.fromisoformat(until) > datetime.now(UTC)
    except ValueError:
        return False


def is_pro(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT plan, plan_until FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return _plan_is_active(row)


def user_plan_info(user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT plan, plan_until FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    active = is_pro(user_id)
    return {
        "plan": PLAN_PRO if active else PLAN_FREE,
        "plan_until": row["plan_until"] if row and active else None,
        "stored_plan": row["plan"] if row else PLAN_FREE,
        "limit": GEMINI_PRO_LIMIT if active else GEMINI_DAILY_LIMIT,
    }


def gemini_daily_limit(user_id: int) -> int:
    import sys

    facade = sys.modules.get("db")
    pro = getattr(facade, "GEMINI_PRO_LIMIT", GEMINI_PRO_LIMIT) if facade else GEMINI_PRO_LIMIT
    free = getattr(facade, "GEMINI_DAILY_LIMIT", GEMINI_DAILY_LIMIT) if facade else GEMINI_DAILY_LIMIT
    return pro if is_pro(user_id) else free


def _grant_pro_on_conn(conn, user_id: int, days: int | None = None) -> str:
    duration = PRO_DURATION_DAYS if days is None else max(1, int(days))
    now = datetime.now(UTC)
    row = conn.execute(
        "SELECT plan, plan_until FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    start = now
    if row and row["plan"] == PLAN_PRO and row["plan_until"]:
        try:
            current = datetime.fromisoformat(row["plan_until"])
            if current > now:
                start = current
        except ValueError:
            pass
    until = (start + timedelta(days=duration)).isoformat()
    conn.execute(
        "UPDATE users SET plan=?, plan_until=? WHERE user_id=?",
        (PLAN_PRO, until, user_id),
    )
    return until


def grant_pro(user_id: int, days: int | None = None) -> str:
    with get_conn() as conn:
        return _grant_pro_on_conn(conn, user_id, days)


def revoke_pro(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET plan=?, plan_until=NULL WHERE user_id=?",
            (PLAN_FREE, user_id),
        )


def record_stars_payment(payment_id: str, user_id: int, stars: int) -> bool:
    """True, если платёж новый и Pro выдан. Повтор того же id — False."""
    charge_id = (payment_id or "").strip()
    if not charge_id:
        return False
    now = datetime.now(UTC).isoformat()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO payments(
                       id, user_id, provider, stars, plan, status, paid_at, created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    charge_id,
                    user_id,
                    "stars",
                    int(stars),
                    PLAN_PRO,
                    "paid",
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT user_id, status FROM payments WHERE id=?",
                (charge_id,),
            ).fetchone()
            if row and row["status"] == "paid":
                user = conn.execute(
                    "SELECT plan, plan_until FROM users WHERE user_id=?",
                    (row["user_id"],),
                ).fetchone()
                if not _plan_is_active(user):
                    _grant_pro_on_conn(conn, row["user_id"])
            return False
        _grant_pro_on_conn(conn, user_id)
        return True


def refund_stars_payment(payment_id: str) -> bool:
    """Помечает Stars-платёж возвращённым и снимает Pro."""
    charge_id = (payment_id or "").strip()
    if not charge_id:
        return False
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, status FROM payments WHERE id=?",
            (charge_id,),
        ).fetchone()
        if not row or row["status"] == "refunded":
            return False
        conn.execute(
            "UPDATE payments SET status='refunded' WHERE id=?",
            (charge_id,),
        )
        conn.execute(
            "UPDATE users SET plan=?, plan_until=NULL WHERE user_id=?",
            (PLAN_FREE, row["user_id"]),
        )
        return True


def touch_scheduler(name: str) -> None:
    save_state(
        "scheduler",
        name,
        {"ts": datetime.now(UTC).isoformat()},
    )


def scheduler_health() -> dict:
    now = datetime.now(UTC)
    items: dict[str, dict] = {}
    stale = False
    for name, interval in SCHEDULER_INTERVALS.items():
        payload = load_state("scheduler", name, {})
        ts = payload.get("ts") if isinstance(payload, dict) else None
        if not ts:
            items[name] = {"ok": True, "age_sec": None, "status": "unknown"}
            continue
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds()
        except ValueError:
            items[name] = {"ok": False, "age_sec": None, "status": "invalid"}
            stale = True
            continue
        ok = age <= interval * 2 + 300
        items[name] = {
            "ok": ok,
            "age_sec": int(age),
            "status": "ok" if ok else "stale",
        }
        if not ok:
            stale = True
    return {"schedulers": items, "schedulers_ok": not stale}
