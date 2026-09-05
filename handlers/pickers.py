"""Pagination callbacks for shared category and payment pickers."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from keyboards.common import categories_keyboard, payments_keyboard

router = Router(name="pickers")


def _keep_back_button(
    callback: CallbackQuery,
    keyboard: InlineKeyboardMarkup,
) -> InlineKeyboardMarkup:
    current = getattr(callback.message, "reply_markup", None)
    if not current or not current.inline_keyboard:
        return keyboard
    last = current.inline_keyboard[-1]
    if (
        len(last) == 1
        and last[0].callback_data
        and not last[0].callback_data.startswith(("pickcat:", "pickpay:"))
    ):
        return InlineKeyboardMarkup(
            inline_keyboard=[*keyboard.inline_keyboard, last]
        )
    return keyboard


@router.callback_query(F.data.startswith("pickcat:"))
async def paginate_categories(callback: CallbackQuery) -> None:
    _, page_raw, prefix = callback.data.split(":", 2)
    keyboard = categories_keyboard(
        callback.from_user.id,
        prefix,
        page=int(page_raw),
    )
    await callback.message.edit_reply_markup(
        reply_markup=_keep_back_button(callback, keyboard)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pickpay:"))
async def paginate_payments(callback: CallbackQuery) -> None:
    _, page_raw, prefix = callback.data.split(":", 2)
    keyboard = payments_keyboard(
        callback.from_user.id,
        prefix,
        page=int(page_raw),
    )
    await callback.message.edit_reply_markup(
        reply_markup=_keep_back_button(callback, keyboard)
    )
    await callback.answer()
