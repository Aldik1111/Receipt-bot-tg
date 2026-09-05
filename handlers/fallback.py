"""Soft responses for callbacks left behind by old messages."""

from aiogram import Router
from aiogram.types import CallbackQuery, ErrorEvent

import db
from i18n import t

router = Router(name="fallback")


@router.error()
async def permission_error(event: ErrorEvent) -> bool:
    if not isinstance(event.exception, PermissionError):
        return False
    update = event.update
    if update.callback_query and update.callback_query.from_user:
        lang = db.get_user_language(update.callback_query.from_user.id)
        await update.callback_query.answer(t("family_readonly", lang), show_alert=True)
        return True
    if update.message and update.message.from_user:
        lang = db.get_user_language(update.message.from_user.id)
        await update.message.answer(t("family_readonly", lang))
        return True
    return True


@router.callback_query()
async def outdated_callback(callback: CallbackQuery) -> None:
    lang = db.get_user_language(callback.from_user.id)
    await callback.answer(t("outdated_button", lang), show_alert=True)
