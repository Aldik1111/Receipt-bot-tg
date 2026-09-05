import asyncio
import logging
from collections import defaultdict
from datetime import date

from aiogram import Bot
from aiogram.types import BufferedInputFile

import backup
import db
from formatting import hx, money
from gemini_engine import generate_insight_text
from handlers.stats import _period_bounds
from i18n import t
from money import tiyn_to_tenge

logger = logging.getLogger(__name__)
BACKGROUND_TASKS: list[asyncio.Task] = []


async def run_recurring_once(bot: Bot) -> None:
    for recurring in db.get_all_active_recurring():
        try:
            today = db.user_today(recurring["user_id"])
            if not db.apply_due_recurring(recurring["id"], today.isoformat()):
                continue
            emoji = "💰" if recurring["type"] == "income" else "💸"
            lang = db.get_user_language(recurring["user_id"])
            await bot.send_message(
                recurring["user_id"],
                t(
                    "sched_recurring",
                    lang,
                    emoji=emoji,
                    amount=money(recurring["amount"]),
                    desc=hx(recurring["description"]) or "",
                ),
            )
        except Exception:
            logger.exception(
                "Не удалось обработать повторяющийся платёж %s",
                recurring["id"],
            )


async def _recurring_scheduler(bot: Bot):
    """Periodically apply due recurring payments."""
    while True:
        try:
            await run_recurring_once(bot)
            db.touch_scheduler("recurring")
        except Exception:
            logger.exception("Сбой итерации планировщика повторяющихся платежей")
        await asyncio.sleep(6 * 3600)

def _digest_due(freq: str, last_sent: str | None, today: date) -> bool:
    if freq == "off":
        return False
    if not last_sent:
        return True
    last = date.fromisoformat(last_sent)
    if freq == "day":
        return last != today
    if freq == "week":
        return (today - last).days >= 7
    if freq == "month":
        return (last.year, last.month) != (today.year, today.month)
    if freq == "year":
        return last.year != today.year
    return False

async def run_digest_once(bot: Bot) -> None:
    for row in db.get_users_for_digest():
        try:
            user_id = row["user_id"]
            today = db.user_today(user_id)
            if not _digest_due(
                row["digest_frequency"],
                row["digest_last_sent"],
                today,
            ):
                continue
            lang = db.get_user_language(user_id)
            frequency = row["digest_frequency"]
            period = (
                frequency
                if frequency in ("day", "week", "month", "year")
                else "month"
            )
            date_from, date_to, label = _period_bounds(period, today)
            total_expense = db.get_total_expense(user_id, date_from, date_to)
            transactions = [
                dict(item)
                for item in db.get_transactions(user_id, date_from, date_to)
                if item["type"] == "expense"
            ]
            if not transactions and total_expense == 0:
                db.mark_digest_sent(user_id, today.isoformat())
                continue
            category_totals: dict[str, int] = defaultdict(int)
            for transaction in transactions:
                category_totals[
                    transaction["category_name"] or t("uncategorized", lang)
                ] += int(transaction["amount"])
            top = sorted(
                category_totals.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:3]
            text = (
                f"🔔 <b>{t('stats_title', lang, label=label)}</b>\n"
                f"{t('expenses', lang)}: {money(total_expense)}"
            )
            if top:
                text += "\n\n" + "\n".join(
                    f"• {hx(name)}: {money(amount)}"
                    for name, amount in top
                )
            insight = await asyncio.to_thread(
                generate_insight_text,
                {
                    "total_expense": str(tiyn_to_tenge(total_expense)),
                    "top_categories": [
                        (name, str(tiyn_to_tenge(amount)))
                        for name, amount in top
                    ],
                },
                lang,
            )
            if insight:
                text += f"\n\n💡 {hx(insight)}"
            await bot.send_message(user_id, text)
            db.mark_digest_sent(user_id, today.isoformat())
        except Exception:
            logger.exception(
                "Не удалось отправить дайджест пользователю %s",
                row["user_id"],
            )


async def _digest_scheduler(bot: Bot):
    while True:
        try:
            await run_digest_once(bot)
            db.touch_scheduler("digest")
        except Exception:
            logger.exception("Сбой итерации планировщика дайджестов")
        await asyncio.sleep(6 * 3600)

async def run_idle_once(bot: Bot) -> None:
    purged = db.purge_stale_state()
    if purged:
        logger.info("Периодическая очистка app_state: %s", purged)
    for row in db.get_users_for_idle_check():
        try:
            today = db.user_today(row["user_id"])
            last_activity = (
                date.fromisoformat(row["last_activity_date"])
                if row["last_activity_date"]
                else None
            )
            if last_activity is None or (today - last_activity).days < 3:
                continue
            last_sent = (
                date.fromisoformat(row["idle_reminder_last_sent"])
                if row["idle_reminder_last_sent"]
                else None
            )
            if last_sent and (today - last_sent).days < 3:
                continue
            await bot.send_message(
                row["user_id"],
                t("sched_idle", db.get_user_language(row["user_id"])),
            )
            db.mark_idle_reminder_sent(row["user_id"], today.isoformat())
        except Exception:
            logger.exception(
                "Не удалось отправить напоминание о простое пользователю %s",
                row["user_id"],
            )


async def _idle_reminder_scheduler(bot: Bot):
    while True:
        try:
            await run_idle_once(bot)
            db.touch_scheduler("idle")
        except Exception:
            logger.exception("Сбой итерации планировщика напоминаний о простое")
        await asyncio.sleep(12 * 3600)

async def run_backup_once(bot: Bot) -> None:
    for row in db.get_users_for_backup():
        try:
            user_id = row["user_id"]
            today = db.user_today(user_id)
            last_sent = (
                date.fromisoformat(row["backup_last_sent"])
                if row["backup_last_sent"]
                else None
            )
            if last_sent and (today - last_sent).days < 7:
                continue
            if db.count_user_data(user_id) == 0:
                continue
            buffer = await asyncio.to_thread(backup.build_backup, user_id)
            await bot.send_document(
                user_id,
                BufferedInputFile(
                    buffer.read(),
                    filename=f"backup_{today.isoformat()}.json",
                ),
                caption=t("sched_backup", db.get_user_language(user_id)),
            )
            db.mark_backup_sent(user_id, today.isoformat())
        except Exception:
            logger.exception(
                "Не удалось отправить автобэкап пользователю %s",
                row["user_id"],
            )


async def run_sqlite_backup_once() -> None:
    from scripts.backup_sqlite import create_timestamped_backup

    dest = await asyncio.to_thread(create_timestamped_backup)
    if dest:
        logger.info("Серверный снимок SQLite: %s", dest)


async def _sqlite_backup_scheduler(bot: Bot):
    while True:
        try:
            await run_sqlite_backup_once()
            db.touch_scheduler("sqlite")
        except Exception:
            logger.exception("Сбой итерации серверного бэкапа SQLite")
        await asyncio.sleep(24 * 3600)


async def _backup_scheduler(bot: Bot):
    while True:
        try:
            await run_backup_once(bot)
            db.touch_scheduler("backup")
        except Exception:
            logger.exception("Сбой итерации планировщика бэкапов")
        await asyncio.sleep(24 * 3600)

async def _supervised(name: str, factory, bot: Bot) -> None:
    while True:
        try:
            await factory(bot)
            logger.error("Фоновая задача %s завершилась, перезапускаю", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Фоновая задача %s упала, перезапускаю", name)
        await asyncio.sleep(30)

async def run_supervised(bot: Bot) -> None:
    BACKGROUND_TASKS[:] = [
        asyncio.create_task(_supervised("recurring", _recurring_scheduler, bot), name="recurring"),
        asyncio.create_task(_supervised("digest", _digest_scheduler, bot), name="digest"),
        asyncio.create_task(_supervised("idle", _idle_reminder_scheduler, bot), name="idle"),
        asyncio.create_task(_supervised("backup", _backup_scheduler, bot), name="backup"),
        asyncio.create_task(_supervised("sqlite", _sqlite_backup_scheduler, bot), name="sqlite"),
    ]
    await asyncio.gather(*BACKGROUND_TASKS)
