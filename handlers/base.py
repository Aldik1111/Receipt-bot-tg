import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

import db
from config import ADMIN_USER_ID
from formatting import hx
from i18n import LANGUAGES, PRIVACY_VERSION, t
from telegram_ui import apply_user_ui

from handlers.common import FeedbackEntry, text_hint
from handlers.family import apply_join_code, start_join_code

logger = logging.getLogger(__name__)

router = Router(name="base")


def _privacy_accepted_label(user_id: int, lang: str) -> str:
    accepted = db.get_privacy_accepted_version(user_id)
    return accepted or t("privacy_not_accepted", lang)


def _onboarding_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("onboarding_accept", lang), callback_data="privacy_accept")],
            [
                InlineKeyboardButton(text=name, callback_data=f"lang:{code}")
                for code, name in LANGUAGES.items()
            ],
        ]
    )


def _privacy_keyboard(user_id: int, lang: str) -> InlineKeyboardMarkup | None:
    if db.get_privacy_accepted_version(user_id) == PRIVACY_VERSION:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=t("privacy_accept", lang), callback_data="privacy_accept")
        ]]
    )


async def _send_onboarding(message: Message, lang: str) -> None:
    await message.answer(t("onboarding_text", lang), reply_markup=_onboarding_keyboard(lang))


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    db.ensure_user(message.from_user.id, message.from_user.username)
    join_code = start_join_code(message.text)
    if join_code:
        await apply_join_code(message, join_code)
        return
    lang = db.get_user_language(message.from_user.id)
    await apply_user_ui(bot, message.from_user.id, message.chat.id)
    if not db.is_onboarded(message.from_user.id):
        await _send_onboarding(message, lang)
        return
    await message.answer(t("start_greeting", lang) + t("help_text", lang))


@router.message(Command("help"))
async def cmd_help(message: Message):
    lang = db.get_user_language(message.from_user.id)
    await message.answer(t("help_text", lang))


@router.message(Command("privacy"))
async def cmd_privacy(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    lang = db.get_user_language(message.from_user.id)
    await message.answer(
        t(
            "privacy_text",
            lang,
            version=PRIVACY_VERSION,
            accepted=_privacy_accepted_label(message.from_user.id, lang),
        ),
        reply_markup=_privacy_keyboard(message.from_user.id, lang),
    )


@router.callback_query(F.data == "privacy_accept")
async def privacy_accept(callback: CallbackQuery, bot: Bot):
    db.ensure_user(callback.from_user.id, callback.from_user.username)
    first_time = not db.is_onboarded(callback.from_user.id)
    db.accept_privacy_policy(callback.from_user.id, PRIVACY_VERSION)
    lang = db.get_user_language(callback.from_user.id)
    await apply_user_ui(bot, callback.from_user.id, callback.message.chat.id)
    await callback.message.edit_text(t("privacy_accepted", lang, version=PRIVACY_VERSION))
    if first_time:
        await callback.message.answer(t("onboarding_done", lang))
    await callback.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer(t("cancel_cleared", lang))
    else:
        await message.answer(t("cancel_idle", lang))


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    lang = db.get_user_language(message.from_user.id)
    if not ADMIN_USER_ID:
        await message.answer(t("feedback_disabled", lang))
        return
    await state.set_state(FeedbackEntry.entering_text)
    await message.answer(t("feedback_prompt", lang))


@router.message(FeedbackEntry.entering_text)
async def feedback_send(message: Message, state: FSMContext, bot: Bot):
    lang = db.get_user_language(message.from_user.id)
    if not message.text:
        await message.answer(text_hint(lang))
        return
    await state.clear()
    user = message.from_user
    username = f"@{user.username}" if user.username else user.full_name
    try:
        await bot.send_message(
            ADMIN_USER_ID,
            f"📩 <b>Фидбэк от {hx(username)}</b> (id {user.id}):\n\n{hx(message.text)}",
        )
        await message.answer(t("feedback_sent", lang))
    except Exception:
        logger.exception("Не удалось переслать фидбэк админу")
        await message.answer(t("feedback_failed", lang))
