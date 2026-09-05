"""Telegram command menu and Mini App button, per user language."""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import (
    BotCommandScopeChat,
    MenuButtonDefault,
    MenuButtonWebApp,
    WebAppInfo,
)

import db
from config import WEBAPP_URL
from i18n import bot_commands, t


async def setup_default_commands(bot: Bot) -> None:
    await bot.set_my_commands(bot_commands("ru"))
    for lang in ("ru", "kk", "en"):
        await bot.set_my_commands(bot_commands(lang), language_code=lang)
    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text=t("webapp_button", "ru"),
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        )
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())


async def apply_user_ui(bot: Bot, user_id: int, chat_id: int | None = None) -> None:
    lang = db.get_user_language(user_id)
    target_chat = chat_id or user_id
    await bot.set_my_commands(
        bot_commands(lang),
        scope=BotCommandScopeChat(chat_id=target_chat),
    )
    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            chat_id=target_chat,
            menu_button=MenuButtonWebApp(
                text=t("webapp_button", lang),
                web_app=WebAppInfo(url=WEBAPP_URL),
            ),
        )
