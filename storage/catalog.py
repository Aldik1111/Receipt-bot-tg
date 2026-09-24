"""SQLite persistence operations for catalog."""
from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from config import (
    DEFAULT_PAYMENT_METHODS,
    DEFAULT_PAYMENT_METHODS_I18N,
    default_categories_for,
    default_emojis_for,
    protected_category_name,
)

from storage import books, schema, users, wipe
from storage.conn import PROTECTED_CATEGORY_NAMES, get_conn

def ensure_user(user_id: int, username: str | None, language: str | None = None):
    """Создаёт пользователя и его дефолтные категории/способы оплаты, если его ещё нет."""
    from i18n import normalize_lang

    lang = normalize_lang(language) if language else "ru"
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return
        # Serialize first-time setup, then recheck after any competing creator commits.
        # Existing users keep the read-only path above.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return
        conn.execute(
            """INSERT INTO users(
                   user_id, username, created_at,
                   idle_reminder_enabled, backup_enabled, language
               ) VALUES (?,?,?,?,?,?)""",
            (user_id, username, datetime.now(UTC).isoformat(), 0, 0, lang),
        )
        _seed_default_catalog(conn, user_id, lang)
        schema._create_personal_book(conn, user_id)


def _seed_default_catalog(conn, user_id: int, lang: str) -> None:
    emojis = default_emojis_for(lang)
    for cat_name in default_categories_for(lang):
        conn.execute(
            "INSERT OR IGNORE INTO categories(user_id, name, emoji) VALUES (?,?,?)",
            (user_id, cat_name, emojis.get(cat_name, "🏷")),
        )
    for pm_name in DEFAULT_PAYMENT_METHODS_I18N.get(lang, DEFAULT_PAYMENT_METHODS):
        conn.execute(
            "INSERT OR IGNORE INTO payment_methods(user_id, name) VALUES (?,?)",
            (user_id, pm_name),
        )


def maybe_reseed_default_catalog(user_id: int, lang: str) -> None:
    """На онбординге меняет дефолтный каталог под язык, если ещё нет операций."""
    if users.is_onboarded(user_id) or wipe.count_user_data(user_id) > 0:
        return
    known = set()
    for pack_lang in ("ru", "en", "kk"):
        known.update(default_categories_for(pack_lang))
        known.update(DEFAULT_PAYMENT_METHODS_I18N.get(pack_lang, []))
    with get_conn() as conn:
        cat_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM categories WHERE user_id=?", (user_id,)
            )
        }
        pay_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM payment_methods WHERE user_id=?", (user_id,)
            )
        }
        if (cat_names - known) or (pay_names - known):
            return
        conn.execute("DELETE FROM learned_categories WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM categories WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM payment_methods WHERE user_id=?", (user_id,))
        _seed_default_catalog(conn, user_id, lang)


def get_categories(user_id: int) -> list[sqlite3.Row]:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM categories WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall()


def add_category(user_id: int, name: str, emoji: str = "🏷") -> int:
    user_id = books._require_write(user_id)
    name = name.strip()[:64]
    emoji = (emoji or "🏷")[:16]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO categories(user_id, name, emoji) VALUES (?,?,?)",
            (user_id, name, emoji),
        )
        row = conn.execute(
            "SELECT id FROM categories WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        return row["id"]


def get_category_id_by_name(user_id: int, name: str) -> int | None:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM categories WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        return row["id"] if row else None


def get_payment_method_id_by_name(user_id: int, name: str) -> int | None:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM payment_methods WHERE user_id=? AND name=?", (user_id, name)
        ).fetchone()
        return row["id"] if row else None


def get_payment_methods(user_id: int) -> list[sqlite3.Row]:
    user_id = books.scope_user(user_id)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM payment_methods WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall()


def add_payment_method(user_id: int, name: str) -> int:
    user_id = books._require_write(user_id)
    name = name.strip()[:64]
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO payment_methods(user_id, name) VALUES (?,?)",
            (user_id, name),
        )
        row = conn.execute(
            "SELECT id FROM payment_methods WHERE user_id=? AND name=?",
            (user_id, name),
        ).fetchone()
        return row["id"]


def _owned_category_id(conn, user_id: int, category_id: int | None) -> int | None:
    if category_id is not None:
        row = conn.execute(
            "SELECT id FROM categories WHERE id=? AND user_id=?",
            (category_id, user_id),
        ).fetchone()
        if row:
            return row["id"]
    lang_row = conn.execute(
        "SELECT language FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    lang = lang_row["language"] if lang_row and lang_row["language"] else "ru"
    names: list[str] = [protected_category_name(lang)]
    names.extend(name for name in PROTECTED_CATEGORY_NAMES if name not in names)
    for name in names:
        fallback = conn.execute(
            "SELECT id FROM categories WHERE user_id=? AND name=?",
            (user_id, name),
        ).fetchone()
        if fallback:
            return fallback["id"]
    return None


def _owned_payment_id(conn, user_id: int, payment_method_id: int | None) -> int | None:
    if payment_method_id is None:
        return None
    row = conn.execute(
        "SELECT id FROM payment_methods WHERE id=? AND user_id=?",
        (payment_method_id, user_id),
    ).fetchone()
    return row["id"] if row else None
