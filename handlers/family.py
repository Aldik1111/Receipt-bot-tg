import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
from formatting import hx
from i18n import t

logger = logging.getLogger(__name__)
router = Router(name="family")

ROLE_KEYS = {
    "owner": "family_role_owner",
    "write": "family_role_write",
    "read": "family_role_read",
}


def start_join_code(text: str | None) -> str | None:
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    payload = parts[1].strip()
    if payload.lower().startswith("join_"):
        return payload[5:]
    return None


def _role_label(role: str, lang: str) -> str:
    return t(ROLE_KEYS.get(role, "family_role_read"), lang)


def _family_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    lang = db.get_user_language(user_id)
    books = db.list_user_books(user_id)
    active = next((row for row in books if row["is_active"]), None)
    book_id = active["id"] if active else db.active_book_id(user_id)
    role = active["role"] if active else db.book_role(user_id)
    members = db.list_book_members(book_id) if book_id else []
    member_lines = []
    for member in members:
        name = member["username"] or str(member["user_id"])
        member_lines.append(
            f"• {hx(name)} — {_role_label(member['role'], lang)}"
        )
    text = t(
        "family_title",
        lang,
        book=hx(active["name"]) if active else t("family_personal", lang),
        role=_role_label(role or "read", lang),
        members="\n".join(member_lines) or t("family_no_members", lang),
    )
    buttons: list[list[InlineKeyboardButton]] = []
    if role == "owner":
        buttons.append(
            [
                InlineKeyboardButton(
                    text=t("family_invite_write", lang),
                    callback_data="fam_invite:write",
                ),
                InlineKeyboardButton(
                    text=t("family_invite_read", lang),
                    callback_data="fam_invite:read",
                ),
            ]
        )
    switch = [
        InlineKeyboardButton(
            text=("• " if row["is_active"] else "")
            + t("family_switch_btn", lang, name=row["name"]),
            callback_data=f"fam_switch:{row['id']}",
        )
        for row in books
        if not row["is_active"]
    ]
    if switch:
        buttons.append(switch[:2])
        if len(switch) > 2:
            buttons.append(switch[2:4])
    if role not in (None, "owner"):
        buttons.append(
            [
                InlineKeyboardButton(
                    text=t("family_leave_btn", lang),
                    callback_data="fam_leave",
                )
            ]
        )
    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return text, markup


async def apply_join_code(message: Message, code: str) -> None:
    db.ensure_user(message.from_user.id, message.from_user.username)
    lang = db.get_user_language(message.from_user.id)
    status = db.join_book_invite(message.from_user.id, code)
    key = {
        "ok": "family_join_ok",
        "switched": "family_join_switched",
        "own": "family_join_own",
        "expired": "family_join_expired",
        "used": "family_join_used",
        "invalid": "family_join_invalid",
    }.get(status, "family_join_invalid")
    await message.answer(t(key, lang))
    if status in {"ok", "switched", "own"}:
        text, keyboard = _family_view(message.from_user.id)
        await message.answer(text, reply_markup=keyboard)


@router.message(Command("family"))
async def cmd_family(message: Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    text, keyboard = _family_view(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("join"))
async def cmd_join(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(t("family_join_usage", db.get_user_language(message.from_user.id)))
        return
    await apply_join_code(message, parts[1])


@router.callback_query(F.data.startswith("fam_invite:"))
async def family_invite(callback: CallbackQuery):
    role = callback.data.split(":", 1)[1]
    lang = db.get_user_language(callback.from_user.id)
    try:
        code = db.create_book_invite(callback.from_user.id, role)
    except PermissionError:
        await callback.answer(t("family_owner_only", lang), show_alert=True)
        return
    except ValueError:
        await callback.answer(t("family_invite_bad_role", lang), show_alert=True)
        return
    await callback.message.answer(
        t("family_invite_created", lang, code=code, hours=db.BOOK_INVITE_HOURS)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fam_switch:"))
async def family_switch(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    try:
        book_id = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer()
        return
    if not db.switch_active_book(callback.from_user.id, book_id):
        await callback.answer(t("family_not_member", lang), show_alert=True)
        return
    text, keyboard = _family_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(t("family_switched", lang))


@router.callback_query(F.data == "fam_leave")
async def family_leave(callback: CallbackQuery):
    lang = db.get_user_language(callback.from_user.id)
    if not db.leave_active_book(callback.from_user.id):
        await callback.answer(t("family_leave_owner", lang), show_alert=True)
        return
    text, keyboard = _family_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(t("family_leave_ok", lang))
