"""SQLite persistence operations for transactions."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3

from config import protected_category_name
from money import MoneyError, as_stored_tiyn

from storage import books, catalog, quota, users
from storage.conn import (
    PROTECTED_CATEGORY_NAMES,
    RECEIPT_TOTAL_MISMATCH_TIYN,
    TX_REGULAR_TYPES,
    get_conn,
)

def add_transaction(
    user_id: int,
    tx_type: str,
    amount: int,
    category_id: int | None,
    payment_method_id: int | None,
    store: str | None,
    description: str | None,
    op_date: str,
    op_time: str | None = None,
    receipt_id: int | None = None,
) -> int:
    if tx_type not in TX_REGULAR_TYPES:
        raise ValueError("Недопустимый тип операции")
    if not books.can_write_book(user_id):
        raise PermissionError("read-only book")
    actor_id = user_id
    owner_id = books.scope_user(user_id)
    book_id = books.active_book_id(user_id)
    amount_tiyn = as_stored_tiyn(amount)
    with get_conn() as conn:
        category_id = catalog._owned_category_id(conn, owner_id, category_id)
        payment_method_id = catalog._owned_payment_id(conn, owner_id, payment_method_id)
        cur = conn.execute(
            """INSERT INTO transactions(
                   user_id, type, amount, category_id, payment_method_id, store,
                   description, receipt_id, op_date, op_time, created_at,
                   created_by, book_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                owner_id,
                tx_type,
                amount_tiyn,
                category_id,
                payment_method_id,
                store,
                (description or "")[:500] or None,
                receipt_id,
                op_date,
                op_time,
                datetime.now(UTC).isoformat(),
                actor_id,
                book_id,
            ),
        )
        books.write_audit(
            book_id, actor_id, "tx_create", "transaction", cur.lastrowid, conn=conn
        )
        return cur.lastrowid


def save_receipt_draft(
    user_id: int, draft_id: str, fallback_date: str
) -> tuple[int, set[int]] | None:
    """Атомарно сохраняет черновик чека и удаляет его.

    Возвращает (receipt_id, затронутые категории) или None, если черновик уже
    сохранён/отменён. BEGIN IMMEDIATE берёт право на запись до чтения: два
    одновременных нажатия кнопки не смогут оба прочитать один черновик.
    При исключении get_conn откатит шапку, все позиции и удаление черновика.
    """
    if not books.can_write_book(user_id):
        raise PermissionError("read-only book")
    actor_id = user_id
    owner_id = books.scope_user(user_id)
    book_id = books.active_book_id(user_id)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT payload FROM app_state
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (draft_id, user_id),
        ).fetchone()
        if not row:
            return None

        parsed = json.loads(row["payload"])
        items = parsed.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("Черновик чека не содержит позиций")
        item_total = sum(as_stored_tiyn(item["price"]) for item in items)
        receipt_total = parsed.get("total")
        if (
            receipt_total is not None
            and abs(item_total - as_stored_tiyn(receipt_total)) > RECEIPT_TOTAL_MISMATCH_TIYN
        ):
            raise ValueError("Сначала нужно выбрать итог чека")

        payment_method_id = catalog._owned_payment_id(conn, owner_id, parsed.get("payment_method_id"))
        if payment_method_id is None:
            payment = conn.execute(
                """SELECT id FROM payment_methods WHERE user_id=?
                   ORDER BY CASE WHEN name='Наличные' THEN 0 ELSE 1 END, id
                   LIMIT 1""",
                (owner_id,),
            ).fetchone()
            payment_method_id = payment["id"] if payment else None
        now = datetime.now(UTC).isoformat()

        receipt_id = conn.execute(
            """INSERT INTO receipts(user_id, store, receipt_date, receipt_time,
               payment_method_id, raw_text, created_at) VALUES (?,?,?,?,?,?,?)""",
            (
                owner_id,
                parsed.get("store"),
                parsed.get("date"),
                parsed.get("time"),
                payment_method_id,
                "" if parsed.get("source") == "bank_notification" else parsed.get("raw_text", ""),
                now,
            ),
        ).lastrowid

        tx_type = parsed.get("tx_type", "expense")
        if tx_type not in ("expense", "income"):
            raise ValueError("Недопустимый тип операции в черновике")

        touched_categories: set[int] = set()
        op_date = parsed.get("date") or fallback_date
        lang_row = conn.execute(
            "SELECT language FROM users WHERE user_id=?", (owner_id,)
        ).fetchone()
        fallback_name = protected_category_name(
            (lang_row["language"] if lang_row and lang_row["language"] else "ru")
        )
        for item in items:
            category = conn.execute(
                "SELECT id FROM categories WHERE user_id=? AND name=?",
                (owner_id, item.get("category") or fallback_name),
            ).fetchone()
            category_id = category["id"] if category else catalog._owned_category_id(conn, owner_id, None)
            if category_id is not None:
                touched_categories.add(category_id)

            conn.execute(
                """INSERT INTO transactions(
                       user_id, type, amount, category_id, payment_method_id, store,
                       description, receipt_id, op_date, op_time, created_at,
                       created_by, book_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    owner_id,
                    tx_type,
                    as_stored_tiyn(item["price"]),
                    category_id,
                    payment_method_id,
                    parsed.get("store"),
                    item.get("name"),
                    receipt_id,
                    op_date,
                    parsed.get("time"),
                    now,
                    actor_id,
                    book_id,
                ),
            )

        conn.execute(
            "UPDATE users SET last_activity_date=? WHERE user_id=?",
            (fallback_date, user_id),
        )
        kind = "bank" if parsed.get("source") == "bank_notification" else "photo"
        for fingerprint in parsed.get("fingerprints") or []:
            if isinstance(fingerprint, str) and fingerprint:
                quota._remember_fingerprint(conn, owner_id, kind, fingerprint, op_date)
        books.write_audit(book_id, actor_id, "receipt_save", "receipt", receipt_id, conn=conn)
        deleted = conn.execute(
            """DELETE FROM app_state
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (draft_id, actor_id),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("Черновик чека изменился во время сохранения")

        return receipt_id, touched_categories


def get_receipt_draft(user_id: int, draft_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT payload FROM app_state
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (draft_id, user_id),
        ).fetchone()
    if not row:
        return None
    try:
        parsed = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _mutate_receipt_draft(user_id: int, draft_id: str, mutator) -> dict | None:
    """Атомарно меняет принадлежащий пользователю черновик."""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT payload FROM app_state
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (draft_id, user_id),
        ).fetchone()
        if not row:
            return None
        parsed = json.loads(row["payload"])
        if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
            raise ValueError("Некорректный черновик чека")
        mutator(parsed)
        conn.execute(
            """UPDATE app_state SET payload=?, updated_at=?
               WHERE scope='receipt_draft' AND key=? AND user_id=?""",
            (
                json.dumps(parsed, ensure_ascii=False),
                datetime.now(UTC).isoformat(),
                draft_id,
                user_id,
            ),
        )
        return parsed


def update_receipt_draft_item(
    user_id: int, draft_id: str, item_index: int, name: str, price
) -> dict | None:
    name = name.strip()
    try:
        price_tiyn = as_stored_tiyn(price)
    except MoneyError as exc:
        raise ValueError("Некорректная цена позиции") from exc
    if not name or len(name) > 500:
        raise ValueError("Некорректное название позиции")

    def mutate(parsed: dict) -> None:
        items = parsed["items"]
        if item_index < 0 or item_index >= len(items):
            raise IndexError("Позиция не найдена")
        items[item_index]["name"] = name
        items[item_index]["price"] = price_tiyn
        parsed.pop("total_resolution", None)

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def delete_receipt_draft_item(user_id: int, draft_id: str, item_index: int) -> dict | None:
    def mutate(parsed: dict) -> None:
        items = parsed["items"]
        if item_index < 0 or item_index >= len(items):
            raise IndexError("Позиция не найдена")
        if len(items) == 1:
            raise ValueError("Нельзя удалить последнюю позицию")
        items.pop(item_index)
        parsed.pop("total_resolution", None)

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def update_receipt_draft_payment(
    user_id: int, draft_id: str, payment_method_id: int
) -> dict | None:
    owner_id = books.scope_user(user_id)
    with get_conn() as conn:
        owned = catalog._owned_payment_id(conn, owner_id, payment_method_id)
        if owned != payment_method_id:
            return None
        row = conn.execute(
            "SELECT name FROM payment_methods WHERE id=? AND user_id=?",
            (payment_method_id, owner_id),
        ).fetchone()
        payment_name = row["name"] if row else None

    def mutate(parsed: dict) -> None:
        parsed["payment_method_id"] = payment_method_id
        parsed["payment_name"] = payment_name

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def update_receipt_draft_type(
    user_id: int,
    draft_id: str,
    tx_type: str,
) -> dict | None:
    if tx_type not in {"expense", "income"}:
        return None

    def mutate(parsed: dict) -> None:
        parsed["tx_type"] = tx_type
        parsed["ambiguous_type"] = False

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def resolve_receipt_draft_total(
    user_id: int, draft_id: str, use_receipt_total: bool
) -> dict | None:
    """Выбирает сумму товаров либо масштабирует позиции до итога чека."""
    def mutate(parsed: dict) -> None:
        items = parsed["items"]
        if not items:
            raise ValueError("Черновик чека не содержит позиций")
        try:
            prices = [as_stored_tiyn(item["price"]) for item in items]
        except MoneyError as exc:
            raise ValueError("Некорректная цена позиции") from exc

        if not use_receipt_total:
            parsed["total"] = sum(prices)
            parsed["total_resolution"] = "items"
            for item, price in zip(items, prices):
                item["price"] = price
            return

        try:
            target_tiyn = as_stored_tiyn(parsed.get("total"))
        except MoneyError as exc:
            raise ValueError("В чеке нет корректного итога") from exc
        if target_tiyn < len(items):
            raise ValueError("Итог слишком мал для количества позиций")

        # Распределяем итог пропорционально исходным ценам в целых тиынах,
        # чтобы сумма записанных операций совпала с итогом чека до тиына.
        price_sum = sum(prices)
        distributable = target_tiyn - len(items)
        shares = [distributable * price // price_sum for price in prices]
        leftovers = [distributable * price % price_sum for price in prices]
        tiyn = [1 + share for share in shares]
        remainder = target_tiyn - sum(tiyn)
        order = sorted(
            range(len(items)),
            key=lambda index: leftovers[index],
            reverse=True,
        )
        for index in order[:remainder]:
            tiyn[index] += 1
        for item, amount_tiyn in zip(items, tiyn):
            item["price"] = amount_tiyn
        parsed["total"] = target_tiyn
        parsed["total_resolution"] = "receipt"

    return _mutate_receipt_draft(user_id, draft_id, mutate)


def _book_clause(actor_id: int, column: str = "book_id") -> tuple[str, tuple]:
    """Условие по активной книге. На личной книге NULL — строки до backfill."""
    book_id = books.active_book_id(actor_id)
    if not book_id:
        return f"({column} IS NULL)", ()
    owner = books.scope_user(actor_id)
    if book_id == books.personal_book_id(owner):
        return f"({column}=? OR {column} IS NULL)", (book_id,)
    return f"{column}=?", (book_id,)


def get_transactions(user_id: int, date_from: str, date_to: str) -> list[sqlite3.Row]:
    """date_from / date_to в формате YYYY-MM-DD, включительно."""
    actor_id = user_id
    user_id = books.scope_user(user_id)
    book_sql, book_params = _book_clause(actor_id, "t.book_id")
    with get_conn() as conn:
        return conn.execute(
            f"""SELECT t.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE t.user_id=? AND t.op_date BETWEEN ? AND ? AND {book_sql}
               ORDER BY t.op_date, t.op_time""",
            (user_id, date_from, date_to, *book_params),
        ).fetchall()


def get_default_payment_method_id(user_id: int) -> int | None:
    """"Наличные", если есть, иначе первый попавшийся способ оплаты пользователя."""
    pms = catalog.get_payment_methods(user_id)
    if not pms:
        return None
    default = next((p for p in pms if p["name"] == "Наличные"), pms[0])
    return default["id"]


def update_transaction_category(user_id: int, tx_id: int, category_id: int) -> bool:
    user_id = books._require_write(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type FROM transactions WHERE id=? AND user_id=?",
            (tx_id, user_id),
        ).fetchone()
        if not row or row["type"] == "transfer":
            return False
        owned = catalog._owned_category_id(conn, user_id, category_id)
        if owned != category_id:
            return False
        cur = conn.execute(
            "UPDATE transactions SET category_id=? WHERE id=? AND user_id=?",
            (owned, tx_id, user_id),
        )
        return cur.rowcount > 0


def rename_category(user_id: int, cat_id: int, new_name: str) -> bool:
    """False, если имя занято, это защищённая «Прочее»/Other, или пытаются так назвать другую."""
    user_id = books._require_write(user_id)
    new_name = new_name.strip()
    if not new_name or new_name in PROTECTED_CATEGORY_NAMES:
        return False
    with get_conn() as conn:
        current = conn.execute(
            "SELECT name FROM categories WHERE id=? AND user_id=?",
            (cat_id, user_id),
        ).fetchone()
        if not current or current["name"] in PROTECTED_CATEGORY_NAMES:
            return False
        try:
            cur = conn.execute(
                "UPDATE categories SET name=? WHERE id=? AND user_id=?",
                (new_name, cat_id, user_id),
            )
        except sqlite3.IntegrityError:
            return False
        return cur.rowcount > 0


def count_transactions_for_category(user_id: int, cat_id: int) -> int:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE user_id=? AND category_id=?",
            (user_id, cat_id),
        ).fetchone()
        return row["n"]


def delete_category(user_id: int, cat_id: int, fallback_name: str | None = None) -> bool:
    """Удаляет категорию, предварительно перенеся все её транзакции в fallback
    (по умолчанию защищённая «Прочее»/Other языка пользователя).
    Отказывает, если удаляют саму fallback-категорию."""
    user_id = books._require_write(user_id)
    if fallback_name is None:
        fallback_name = protected_category_name(users.get_user_language(user_id))
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM categories WHERE id=? AND user_id=?", (cat_id, user_id)
        ).fetchone()
        if not row:
            return False
        if row["name"] == fallback_name or row["name"] in PROTECTED_CATEGORY_NAMES:
            return False  # защищаем базовую категорию от удаления

        fallback = conn.execute(
            "SELECT id FROM categories WHERE user_id=? AND name=?",
            (user_id, fallback_name),
        ).fetchone()
        if not fallback:
            fallback_id = conn.execute(
                "INSERT INTO categories(user_id, name) VALUES (?,?)",
                (user_id, fallback_name),
            ).lastrowid
        else:
            fallback_id = fallback["id"]

        conn.execute(
            "UPDATE transactions SET category_id=? WHERE user_id=? AND category_id=?",
            (fallback_id, user_id, cat_id),
        )
        conn.execute(
            "UPDATE recurring_payments SET category_id=? WHERE user_id=? AND category_id=?",
            (fallback_id, user_id, cat_id),
        )
        conn.execute(
            "UPDATE learned_categories SET category_id=? WHERE user_id=? AND category_id=?",
            (fallback_id, user_id, cat_id),
        )
        conn.execute(
            "DELETE FROM category_budgets WHERE user_id=? AND category_id=?",
            (user_id, cat_id),
        )
        cur = conn.execute(
            "DELETE FROM categories WHERE id=? AND user_id=?", (cat_id, user_id)
        )
        return cur.rowcount > 0


def rename_payment_method(user_id: int, pm_id: int, new_name: str) -> bool:
    user_id = books._require_write(user_id)
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "UPDATE payment_methods SET name=? WHERE id=? AND user_id=?",
                (new_name, pm_id, user_id),
            )
        except sqlite3.IntegrityError:
            return False
        return cur.rowcount > 0


def count_transactions_for_payment_method(user_id: int, pm_id: int) -> int:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE user_id=? AND payment_method_id=?",
            (user_id, pm_id),
        ).fetchone()
        return row["n"]


def delete_payment_method(user_id: int, pm_id: int) -> bool:
    """Удаляет способ оплаты. У транзакций, где он использовался, payment_method_id
    станет NULL (способ оплаты был не критичен для истории - в отличие от категории,
    отдельного "запасного" способа оплаты не создаём)."""
    user_id = books._require_write(user_id)
    with get_conn() as conn:
        pms = conn.execute(
            "SELECT id FROM payment_methods WHERE user_id=?", (user_id,)
        ).fetchall()
        if len(pms) <= 1:
            return False  # не даём удалить последний способ оплаты
        conn.execute(
            "UPDATE transactions SET payment_method_id=NULL WHERE user_id=? AND payment_method_id=?",
            (user_id, pm_id),
        )
        conn.execute(
            "UPDATE receipts SET payment_method_id=NULL WHERE user_id=? AND payment_method_id=?",
            (user_id, pm_id),
        )
        conn.execute(
            "UPDATE recurring_payments SET payment_method_id=NULL WHERE user_id=? AND payment_method_id=?",
            (user_id, pm_id),
        )
        cur = conn.execute(
            "DELETE FROM payment_methods WHERE id=? AND user_id=?", (pm_id, user_id)
        )
        return cur.rowcount > 0


def get_recent_transactions(
    user_id: int,
    limit: int = 8,
    offset: int = 0,
    date_from: str | None = None,
    date_to: str | None = None,
    category_name: str | None = None,
    *,
    query: str | None = None,
    category_id: int | None = None,
    tx_type: str | None = None,
) -> list[sqlite3.Row]:
    """Список операций, новые сверху, с пагинацией и фильтрами в SQL."""
    actor_id = user_id
    user_id = books.scope_user(user_id)
    conditions = ["t.user_id=?"]
    params: list = [user_id]
    book_sql, book_params = _book_clause(actor_id, "t.book_id")
    conditions.append(book_sql)
    params.extend(book_params)
    if date_from and date_to:
        conditions.append("t.op_date BETWEEN ? AND ?")
        params.extend((date_from, date_to))
    if category_name:
        conditions.append(
            "t.category_id=(SELECT id FROM categories WHERE user_id=? AND name=?)"
        )
        params.extend((user_id, category_name))
    if category_id is not None:
        conditions.append(
            "t.category_id IN (SELECT id FROM categories WHERE user_id=? AND id=?)"
        )
        params.extend((user_id, category_id))
    if tx_type in {"expense", "income", "transfer"}:
        conditions.append("t.type=?")
        params.append(tx_type)
    if query and query.strip():
        escaped = (
            query.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        conditions.append(
            "(COALESCE(t.description, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(t.store, '') LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        )
        params.extend((f"%{escaped}%", f"%{escaped}%"))

    params.extend((limit, offset))
    where = " AND ".join(conditions)
    with get_conn() as conn:
        return conn.execute(
            f"""SELECT t.*, c.name AS category_name, c.emoji AS category_emoji,
                       p.name AS payment_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               WHERE {where}
               ORDER BY t.op_date DESC, t.op_time DESC, t.id DESC
               LIMIT ? OFFSET ?""",
            params,
        ).fetchall()


def count_all_transactions(
    user_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    category_name: str | None = None,
    *,
    query: str | None = None,
    category_id: int | None = None,
    tx_type: str | None = None,
) -> int:
    actor_id = user_id
    user_id = books.scope_user(user_id)
    conditions = ["t.user_id=?"]
    params: list = [user_id]
    book_sql, book_params = _book_clause(actor_id, "t.book_id")
    conditions.append(book_sql)
    params.extend(book_params)
    if date_from and date_to:
        conditions.append("t.op_date BETWEEN ? AND ?")
        params.extend((date_from, date_to))
    if category_name:
        conditions.append(
            "t.category_id=(SELECT id FROM categories WHERE user_id=? AND name=?)"
        )
        params.extend((user_id, category_name))
    if category_id is not None:
        conditions.append(
            "t.category_id IN (SELECT id FROM categories WHERE user_id=? AND id=?)"
        )
        params.extend((user_id, category_id))
    if tx_type in {"expense", "income", "transfer"}:
        conditions.append("t.type=?")
        params.append(tx_type)
    if query and query.strip():
        escaped = (
            query.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        conditions.append(
            "(COALESCE(t.description, '') LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(t.store, '') LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        )
        params.extend((f"%{escaped}%", f"%{escaped}%"))

    where = " AND ".join(conditions)
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS n FROM transactions t
                LEFT JOIN categories c ON c.id = t.category_id
                WHERE {where}""",
            params,
        ).fetchone()
        return row["n"]


def get_transaction_by_id(user_id: int, tx_id: int) -> sqlite3.Row | None:
    actor_id = user_id
    user_id = books.scope_user(user_id)
    book_sql, book_params = _book_clause(actor_id, "t.book_id")
    with get_conn() as conn:
        return conn.execute(
            f"""SELECT t.*, c.name AS category_name, c.emoji AS category_emoji, p.name AS payment_name,
                      g.name AS goal_name
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN payment_methods p ON p.id = t.payment_method_id
               LEFT JOIN savings_goals g ON g.id = t.goal_id
               WHERE t.id=? AND t.user_id=? AND {book_sql}""",
            (tx_id, user_id, *book_params),
        ).fetchone()


def update_transaction_amount(user_id: int, tx_id: int, amount) -> bool:
    user_id = books._require_write(user_id)
    amount_tiyn = as_stored_tiyn(amount)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type FROM transactions WHERE id=? AND user_id=?",
            (tx_id, user_id),
        ).fetchone()
        if not row or row["type"] == "transfer":
            return False
        cur = conn.execute(
            "UPDATE transactions SET amount=? WHERE id=? AND user_id=?",
            (amount_tiyn, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_date(user_id: int, tx_id: int, op_date: str) -> bool:
    """Обновляет дату только своей операции; принимает ISO YYYY-MM-DD."""
    user_id = books._require_write(user_id)
    try:
        datetime.strptime(op_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET op_date=? WHERE id=? AND user_id=?",
            (op_date, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_type(user_id: int, tx_id: int, tx_type: str) -> bool:
    user_id = books._require_write(user_id)
    if tx_type not in TX_REGULAR_TYPES:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type FROM transactions WHERE id=? AND user_id=?",
            (tx_id, user_id),
        ).fetchone()
        if not row or row["type"] == "transfer":
            return False
        cur = conn.execute(
            "UPDATE transactions SET type=? WHERE id=? AND user_id=?",
            (tx_type, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_store(user_id: int, tx_id: int, store: str | None) -> bool:
    user_id = books._require_write(user_id)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET store=? WHERE id=? AND user_id=?",
            (store, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_description(user_id: int, tx_id: int, description: str | None) -> bool:
    user_id = books._require_write(user_id)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET description=? WHERE id=? AND user_id=?",
            (description, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_payment_method(user_id: int, tx_id: int, payment_method_id: int | None) -> bool:
    user_id = books._require_write(user_id)
    with get_conn() as conn:
        owned = catalog._owned_payment_id(conn, user_id, payment_method_id)
        if payment_method_id is not None and owned != payment_method_id:
            return False
        cur = conn.execute(
            "UPDATE transactions SET payment_method_id=? WHERE id=? AND user_id=?",
            (owned, tx_id, user_id),
        )
        return cur.rowcount > 0


def update_transaction_fields(
    user_id: int,
    tx_id: int,
    *,
    tx_type: str,
    amount,
    category_id: int,
    op_date: str,
    description: str | None,
    store: str | None,
    payment_method_id: int | None,
) -> bool:
    """Одна транзакция БД на все поля карточки. Transfer не трогаем."""
    actor_id = user_id
    user_id = books._require_write(user_id)
    if tx_type not in TX_REGULAR_TYPES:
        return False
    try:
        datetime.strptime(op_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    amount_tiyn = as_stored_tiyn(amount)
    book_sql, book_params = _book_clause(actor_id, "book_id")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT type FROM transactions WHERE id=? AND user_id=? AND {book_sql}",
            (tx_id, user_id, *book_params),
        ).fetchone()
        if not row or row["type"] == "transfer":
            return False
        owned_cat = catalog._owned_category_id(conn, user_id, category_id)
        if owned_cat != category_id:
            return False
        owned_pay = catalog._owned_payment_id(conn, user_id, payment_method_id)
        if payment_method_id is not None and owned_pay != payment_method_id:
            return False
        cur = conn.execute(
            f"""UPDATE transactions SET
                   type=?, amount=?, category_id=?, op_date=?,
                   description=?, store=?, payment_method_id=?
               WHERE id=? AND user_id=? AND type != 'transfer' AND {book_sql}""",
            (
                tx_type,
                amount_tiyn,
                owned_cat,
                op_date,
                description,
                store,
                owned_pay,
                tx_id,
                user_id,
                *book_params,
            ),
        )
        return cur.rowcount > 0


def delete_transaction(user_id: int, tx_id: int) -> bool:
    actor_id = user_id
    user_id = books._require_write(user_id)
    book_sql, book_params = _book_clause(actor_id, "book_id")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"""SELECT receipt_id, type, goal_id, goal_delta
               FROM transactions WHERE id=? AND user_id=? AND {book_sql}""",
            (tx_id, user_id, *book_params),
        ).fetchone()
        if not row:
            return False
        if row["type"] == "transfer" and row["goal_id"] and row["goal_delta"]:
            goal = conn.execute(
                "SELECT current_amount FROM savings_goals WHERE id=? AND user_id=?",
                (row["goal_id"], user_id),
            ).fetchone()
            if goal is not None:
                new_current = int(goal["current_amount"]) - int(row["goal_delta"])
                if new_current < 0:
                    return False
                conn.execute(
                    "UPDATE savings_goals SET current_amount=? WHERE id=? AND user_id=?",
                    (new_current, row["goal_id"], user_id),
                )
        receipt_id = row["receipt_id"]
        cur = conn.execute(
            f"DELETE FROM transactions WHERE id=? AND user_id=? AND {book_sql}",
            (tx_id, user_id, *book_params),
        )
        if receipt_id:
            leftover = conn.execute(
                "SELECT 1 FROM transactions WHERE receipt_id=? AND user_id=?",
                (receipt_id, user_id),
            ).fetchone()
            if not leftover:
                conn.execute(
                    "DELETE FROM receipts WHERE id=? AND user_id=?",
                    (receipt_id, user_id),
                )
        return cur.rowcount > 0
