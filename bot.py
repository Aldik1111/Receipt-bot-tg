"""Application assembly for the Telegram bot."""

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import db
from config import BOT_TOKEN
from fsm_storage import SQLiteStorage
from handlers import (
    base,
    billing,
    budgets,
    catalog,
    fallback,
    family,
    goals,
    import_export,
    manual,
    pickers,
    receipt,
    recent,
    recurring,
    settings,
    stats,
)
from logutil import configure_logging
from schedulers import run_supervised
from telegram_ui import setup_default_commands

# Compatibility exports used by existing integrations and regression tests.
from handlers.budgets import _budget_view
from handlers.goals import _goal_detail_view, _goals_view
from handlers.receipt import _receipt_draft_view, _receipt_error_text
from handlers.recent import _tx_detail_view
from handlers.recurring import _recurring_detail_view, _recurring_view
from handlers.stats import (
    _budget_warning_text,
    _budget_warnings_text,
    _month_start,
    _period_bounds,
)

configure_logging()
logger = logging.getLogger(__name__)

ROUTERS = (
    base.router,
    family.router,
    billing.router,
    receipt.router,
    manual.router,
    stats.router,
    catalog.router,
    recent.router,
    budgets.router,
    goals.router,
    recurring.router,
    settings.router,
    import_export.router,
    pickers.router,
    fallback.router,
)


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(storage=SQLiteStorage())
    for domain_router in ROUTERS:
        dispatcher.include_router(domain_router)
    return dispatcher


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Задайте переменную окружения BOT_TOKEN "
            "в .env, полученную у @BotFather."
        )

    db.init_db()
    purged = db.purge_stale_state()
    if purged:
        logger.info("Очищены устаревшие черновики в FSM-хранилище: %s", purged)

    telegram_bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await setup_default_commands(telegram_bot)
    dispatcher = create_dispatcher()
    for name in db.SCHEDULER_INTERVALS:
        db.touch_scheduler(name)
    scheduler_task = asyncio.create_task(run_supervised(telegram_bot))
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(dispatcher.stop_polling()))
    except NotImplementedError:
        pass
    try:
        await dispatcher.start_polling(telegram_bot)
    finally:
        scheduler_task.cancel()
        await asyncio.gather(scheduler_task, return_exceptions=True)
        await telegram_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
