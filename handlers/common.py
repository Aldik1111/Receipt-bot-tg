"""Shared FSM states and constants used by domain routers."""

from aiogram.fsm.state import State, StatesGroup

from i18n import t

TEXT_HINT = t("text_hint", "ru")



def text_hint(lang: str = "ru") -> str:
    return t("text_hint", lang)
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_RESTORE_BYTES = 20 * 1024 * 1024

RECEIPT_SCOPE = "receipt_draft"
IMPORT_SCOPE = "import_draft"
LIST_SCOPE = "list_context"
RESTORE_SCOPE = "restore_draft"
DUP_SCOPE = "dup_photo"

DIGEST_FREQ_LABELS = {
    "off": "digest_off",
    "day": "digest_day",
    "week": "digest_week",
    "month": "digest_month",
    "year": "digest_year",
}


class ManualEntry(StatesGroup):
    choosing_type = State()
    entering_amount = State()
    choosing_category = State()
    choosing_payment = State()
    entering_description = State()


class CategoryEntry(StatesGroup):
    entering_name = State()
    editing_name = State()
    editing_emoji = State()


class PaymentEntry(StatesGroup):
    entering_name = State()
    editing_name = State()


class RestoreEntry(StatesGroup):
    awaiting_file = State()


class ReceiptDraftEdit(StatesGroup):
    entering_item = State()


class StatsCustomPeriod(StatesGroup):
    entering_dates = State()


class TransactionEdit(StatesGroup):
    entering_amount = State()
    entering_date = State()
    entering_store = State()
    entering_description = State()


class RecentSearch(StatesGroup):
    entering_query = State()


class BudgetEntry(StatesGroup):
    entering_limit = State()


class GoalEntry(StatesGroup):
    entering_name = State()
    entering_target = State()
    entering_deadline = State()


class GoalContribute(StatesGroup):
    entering_amount = State()


class GoalWithdraw(StatesGroup):
    entering_amount = State()


class GoalEdit(StatesGroup):
    entering_name = State()
    entering_target = State()
    entering_deadline = State()


class RecurringEntry(StatesGroup):
    choosing_type = State()
    entering_amount = State()
    choosing_category = State()
    choosing_payment = State()
    entering_day = State()
    entering_description = State()


class RecurringEdit(StatesGroup):
    entering_amount = State()
    entering_day = State()
    entering_description = State()


class ExportState(StatesGroup):
    choosing_format = State()


class DangerZone(StatesGroup):
    entering_phrase = State()


class FeedbackEntry(StatesGroup):
    entering_text = State()
