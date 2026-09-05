import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

import db
from config import GEMINI_PRO_LIMIT, PRO_DURATION_DAYS, STARS_PRO_PRICE
from i18n import format_date, t

logger = logging.getLogger(__name__)
router = Router(name="billing")


def _pro_view(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    lang = db.get_user_language(user_id)
    info = db.user_plan_info(user_id)
    until = info["plan_until"]
    until_label = format_date(until[:10], lang) if until else t("dash", lang)
    text = t(
        "pro_title",
        lang,
        plan=t("pro_plan_pro", lang) if info["plan"] == "pro" else t("pro_plan_free", lang),
        until=until_label,
        free_limit=db.GEMINI_DAILY_LIMIT,
        pro_limit=GEMINI_PRO_LIMIT,
        days=PRO_DURATION_DAYS,
        stars=STARS_PRO_PRICE,
    )
    buttons = [
        [InlineKeyboardButton(text=t("pro_buy", lang, stars=STARS_PRO_PRICE), callback_data="pro_buy")]
    ]
    if info["plan"] == "pro":
        buttons.append(
            [InlineKeyboardButton(text=t("pro_cancel", lang), callback_data="pro_cancel")]
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return text, keyboard


@router.message(Command("pro"))
async def cmd_pro(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _pro_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "pro_buy")
async def pro_buy(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    await callback.message.answer_invoice(
        title=t("pro_invoice_title", lang),
        description=t(
            "pro_invoice_desc",
            lang,
            days=PRO_DURATION_DAYS,
            limit=GEMINI_PRO_LIMIT,
        ),
        payload=f"pro:{callback.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label="Pro", amount=STARS_PRO_PRICE)],
        provider_token="",
    )
    await callback.answer()


def stars_pre_checkout_ok(payload: str | None, currency: str | None, amount: int | None) -> bool:
    return (
        bool(payload)
        and payload.startswith("pro:")
        and currency == "XTR"
        and int(amount or 0) == STARS_PRO_PRICE
    )


@router.pre_checkout_query()
async def pro_pre_checkout(query: PreCheckoutQuery):
    ok = stars_pre_checkout_ok(query.invoice_payload, query.currency, query.total_amount)
    await query.answer(ok=ok)


@router.callback_query(F.data == "pro_cancel")
async def pro_cancel(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    if not db.is_pro(callback.from_user.id):
        await callback.answer(t("pro_already", lang), show_alert=True)
        return
    db.revoke_pro(callback.from_user.id)
    text, keyboard = _pro_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(t("pro_cancelled", lang), show_alert=True)


@router.message(F.successful_payment)
async def pro_paid(message: Message):
    payment = message.successful_payment
    lang = db.get_user_language(message.from_user.id)
    charge_id = payment.telegram_payment_charge_id
    created = db.record_stars_payment(
        charge_id,
        message.from_user.id,
        payment.total_amount,
    )
    if created:
        info = db.user_plan_info(message.from_user.id)
        until = info["plan_until"] or ""
        await message.answer(
            t(
                "pro_thanks",
                lang,
                until=format_date(until[:10], lang) if until else t("dash", lang),
                limit=info["limit"],
            )
        )
        return
    logger.info("Повтор Stars charge_id=%s user=%s", charge_id, message.from_user.id)
    await message.answer(t("pro_already", lang))


@router.message(F.refunded_payment)
async def pro_refunded(message: Message):
    payment = message.refunded_payment
    charge_id = getattr(payment, "telegram_payment_charge_id", None)
    db.refund_stars_payment(charge_id)
    lang = db.get_user_language(message.from_user.id)
    await message.answer(t("pro_refunded", lang))
