"""Фрагмент слоя БД. Имена соседних модулей подставляются из фасада db.py."""
from __future__ import annotations

from storage.conn import *  # noqa: F403

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

        goals = rows_as_dicts(
            """SELECT id, name, target_amount, current_amount, deadline, created_at
               FROM savings_goals WHERE user_id=? ORDER BY id"""
        )
        goal_id_map = {row["id"]: index + 1 for index, row in enumerate(goals)}
        for row in goals:
            original_id = row.pop("id")
            row["backup_id"] = goal_id_map[original_id]

        transactions = rows_as_dicts(
            """SELECT t.type, t.amount, c.name AS category, p.name AS payment_method,
                      t.store, t.description, t.receipt_id, t.goal_id, t.goal_delta,
                      t.op_date, t.op_time, t.created_at
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE t.user_id=?
               ORDER BY t.id"""
        )
        for tx in transactions:
            tx["receipt_backup_id"] = id_map.get(tx.pop("receipt_id"))
            goal_id = tx.pop("goal_id")
            tx["goal_backup_id"] = goal_id_map.get(goal_id)

        settings = conn.execute(
            """SELECT language, digest_frequency, bank_import_enabled,
                      idle_reminder_enabled, backup_enabled, last_activity_date, timezone,
                      onboarded, privacy_accepted_version
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
            "version": BACKUP_VERSION,
            "amount_unit": "tiyn",
            "settings": dict(settings) if settings else {},
            "categories": rows_as_dicts("SELECT name, emoji FROM categories WHERE user_id=? ORDER BY id"),
            "payment_methods": rows_as_dicts("SELECT name FROM payment_methods WHERE user_id=? ORDER BY id"),
            "receipts": receipts,
            "transactions": transactions,
            "recurring": recurring,
            "budgets": rows_as_dicts(
                """SELECT c.name AS category, b.monthly_limit FROM category_budgets b
                   LEFT JOIN categories c ON c.id = b.category_id WHERE b.user_id=?"""
            ),
            "goals": goals,
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
        _wipe_user_finance(conn, user_id, delete_user=False, wipe_books=False)

        amount_unit = data.get("amount_unit")
        if not amount_unit:
            amount_unit = "tiyn" if int(data.get("version") or 0) >= 4 else "tenge"

        settings = data.get("settings") or {}
        conn.execute(
            """UPDATE users SET language=?, digest_frequency=?, bank_import_enabled=?,
                   idle_reminder_enabled=?, backup_enabled=?, last_activity_date=?, timezone=?,
                   onboarded=?, privacy_accepted_version=?
               WHERE user_id=?""",
            (
                settings.get("language") or "ru",
                settings.get("digest_frequency") or "off",
                int(bool(settings.get("bank_import_enabled"))),
                int(bool(settings.get("idle_reminder_enabled"))),
                int(bool(settings.get("backup_enabled"))),
                settings.get("last_activity_date"),
                normalize_timezone(settings.get("timezone")),
                int(bool(settings.get("onboarded"))),
                settings.get("privacy_accepted_version"),
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
        goal_map: dict[int, int] = {}
        for goal in data.get("goals") or []:
            cur = conn.execute(
                """INSERT INTO savings_goals(
                       user_id, name, target_amount, current_amount, deadline, created_at
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    user_id,
                    goal["name"],
                    backup_amount_to_tiyn(goal["target_amount"], amount_unit),
                    backup_amount_to_tiyn(goal.get("current_amount") or 0, amount_unit),
                    _parse_iso_date(goal.get("deadline")),
                    goal.get("created_at") or now,
                ),
            )
            backup_goal_id = goal.get("backup_id")
            if backup_goal_id is not None:
                goal_map[int(backup_goal_id)] = cur.lastrowid

        tx_count = 0
        for tx in data.get("transactions") or []:
            receipt_ref = tx.get("receipt_backup_id")
            tx_type = tx.get("type") if tx.get("type") in TX_ALL_TYPES else "expense"
            goal_ref = tx.get("goal_backup_id")
            goal_id = goal_map.get(int(goal_ref)) if goal_ref else None
            goal_delta = tx.get("goal_delta")
            if goal_delta is not None:
                goal_delta = backup_signed_amount_to_tiyn(goal_delta, amount_unit)
            conn.execute(
                """INSERT INTO transactions(
                       user_id, type, amount, category_id, payment_method_id, store,
                       description, receipt_id, goal_id, goal_delta, op_date, op_time, created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    tx_type,
                    backup_amount_to_tiyn(tx["amount"], amount_unit),
                    cat_id(tx.get("category")),
                    pm_id(tx.get("payment_method")),
                    tx.get("store"),
                    tx.get("description"),
                    receipt_map.get(int(receipt_ref)) if receipt_ref else None,
                    goal_id,
                    goal_delta,
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
                    backup_amount_to_tiyn(rec["amount"], amount_unit),
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
            cat_name = budget.get("category")
            if cat_name:
                cid = cat_id(cat_name)
                if not cid:
                    continue
            else:
                cid = None
            conn.execute(
                """INSERT INTO category_budgets(user_id, category_id, monthly_limit)
                   VALUES (?,?,?)""",
                (user_id, cid, backup_amount_to_tiyn(budget["monthly_limit"], amount_unit)),
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
