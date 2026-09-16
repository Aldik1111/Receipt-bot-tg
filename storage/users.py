"""Фрагмент слоя БД. Имена соседних модулей подставляются из фасада db.py."""
from __future__ import annotations

from storage.conn import *  # noqa: F403

# ---------------------------------------------------------------------------
# Язык и настройка автосводки
# ---------------------------------------------------------------------------

def get_user_language(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT language FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["language"] if row and row["language"] else "ru"


def get_user_timezone(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT timezone FROM users WHERE user_id=?", (user_id,)).fetchone()
    name = row["timezone"] if row else None
    return normalize_timezone(name)


def set_user_timezone(user_id: int, tz_name: str) -> bool:
    if tz_name not in TIMEZONE_CHOICES:
        return False
    with get_conn() as conn:
        cur = conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz_name, user_id))
        return cur.rowcount > 0


def user_now(user_id: int, now: datetime | None = None) -> datetime:
    return now_in_tz(get_user_timezone(user_id), now)


def user_today(user_id: int, now: datetime | None = None) -> date:
    return today_in_tz(get_user_timezone(user_id), now)


def set_user_language(user_id: int, lang: str) -> None:
    from i18n import normalize_lang

    normalized = normalize_lang(lang)
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET language=? WHERE user_id=?",
            (normalized, user_id),
        )
    maybe_reseed_default_catalog(user_id, normalized)


def is_onboarded(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT onboarded FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return bool(row and row["onboarded"])


def mark_onboarded(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET onboarded=1 WHERE user_id=?", (user_id,))


def get_privacy_accepted_version(user_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT privacy_accepted_version FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row["privacy_accepted_version"] if row else None


def accept_privacy_policy(user_id: int, version: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE users SET privacy_accepted_version=?, onboarded=1
               WHERE user_id=?""",
            (version, user_id),
        )


def get_digest_frequency(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT digest_frequency FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["digest_frequency"] if row and row["digest_frequency"] else "off"


def set_digest_frequency(user_id: int, freq: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET digest_frequency=? WHERE user_id=?", (freq, user_id))


def get_users_for_digest() -> list[sqlite3.Row]:
    """Все пользователи, у кого автосводка включена (не 'off')."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_id, digest_frequency, digest_last_sent FROM users WHERE digest_frequency != 'off'"
        ).fetchall()


def mark_digest_sent(user_id: int, sent_date: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET digest_last_sent=? WHERE user_id=?", (sent_date, user_id))


# ---------------------------------------------------------------------------
# Обучение категоризации (запоминание слово -> категория)
# ---------------------------------------------------------------------------

def learn_category(user_id: int, keyword: str, category_id: int) -> None:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        if _owned_category_id(conn, user_id, category_id) != category_id:
            return
        conn.execute(
            """INSERT INTO learned_categories(user_id, keyword, category_id) VALUES (?,?,?)
               ON CONFLICT(user_id, keyword) DO UPDATE SET category_id=excluded.category_id""",
            (user_id, keyword.strip().lower(), category_id),
        )


def get_learned_category_name(user_id: int, keyword: str) -> str | None:
    user_id = scope_user(user_id)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT c.name FROM learned_categories lc
               JOIN categories c ON c.id = lc.category_id
               WHERE lc.user_id=? AND lc.keyword=?""",
            (user_id, keyword.strip().lower()),
        ).fetchone()
        return row["name"] if row else None


# ---------------------------------------------------------------------------
# Эмодзи категорий
# ---------------------------------------------------------------------------

def set_category_emoji(user_id: int, category_id: int, emoji: str) -> bool:
    user_id = _require_write(user_id)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE categories SET emoji=? WHERE id=? AND user_id=?", (emoji, category_id, user_id)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Настройки: импорт из банков, напоминание о простое, активность, бэкап
# ---------------------------------------------------------------------------

def get_settings(user_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        return conn.execute(
            """SELECT language, digest_frequency, bank_import_enabled, idle_reminder_enabled,
                      last_activity_date, backup_enabled, backup_last_sent, timezone,
                      onboarded, privacy_accepted_version
               FROM users WHERE user_id=?""",
            (user_id,),
        ).fetchone()


def set_bank_import_enabled(user_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET bank_import_enabled=? WHERE user_id=?", (int(enabled), user_id))


def set_idle_reminder_enabled(user_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET idle_reminder_enabled=? WHERE user_id=?", (int(enabled), user_id))


def set_backup_enabled(user_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET backup_enabled=? WHERE user_id=?", (int(enabled), user_id))


def touch_activity(user_id: int, today: str) -> None:
    """Отмечает, что пользователь сегодня что-то делал - сбрасывает счётчик простоя."""
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_activity_date=? WHERE user_id=?", (today, user_id))


def get_users_for_idle_check() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT user_id, last_activity_date, idle_reminder_last_sent FROM users
               WHERE idle_reminder_enabled=1"""
        ).fetchall()


def mark_idle_reminder_sent(user_id: int, sent_date: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET idle_reminder_last_sent=? WHERE user_id=?", (sent_date, user_id))


def get_users_for_backup() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT user_id, backup_last_sent FROM users
               WHERE backup_enabled=1"""
        ).fetchall()


def mark_backup_sent(user_id: int, sent_date: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET backup_last_sent=? WHERE user_id=?", (sent_date, user_id))


def get_users_bank_import_enabled() -> set[int]:
    with get_conn() as conn:
        return {r["user_id"] for r in conn.execute("SELECT user_id FROM users WHERE bank_import_enabled=1")}
