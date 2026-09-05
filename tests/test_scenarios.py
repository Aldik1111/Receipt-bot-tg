"""End-to-end application scenarios with a real temporary SQLite database."""

import tempfile
import unittest
import io
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
from handlers.base import cmd_cancel
from handlers.catalog import _categories_view, _payments_view
from handlers.common import RECEIPT_SCOPE, RESTORE_SCOPE
from handlers.import_export import restore_confirm
from handlers.manual import add_choose_type, add_go_back, quick_add
from handlers.receipt import confirm_receipt_save
from handlers.settings import danger_confirm2
from handlers.stats import _send_stats
from keyboards.common import categories_keyboard, payments_keyboard
from schedulers import (
    run_backup_once,
    run_digest_once,
    run_idle_once,
    run_recurring_once,
)


class FakeState:
    def __init__(self, current=None):
        self.current = current
        self.cleared = False
        self.data = {}

    async def get_state(self):
        return self.current

    async def set_state(self, state):
        self.current = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.current = None
        self.cleared = True


def fake_message(text: str, user_id: int = 1):
    return SimpleNamespace(
        text=text,
        forward_date=None,
        message_id=10,
        from_user=SimpleNamespace(id=user_id, username="tester"),
        chat=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )


def fake_callback(data: str, user_id: int = 1):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, username="tester"),
        message=SimpleNamespace(
            text="Предпросмотр",
            answer=AsyncMock(),
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )


class HandlerScenarios(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tempdir.name) / "scenarios.db")
        db.init_db()
        db.ensure_user(1, "tester")
        db.ensure_user(2, "other")

    async def asyncTearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    async def test_quick_add_persists_transaction(self):
        message = fake_message("500 такси")
        with patch(
            "services.transactions.categorize_smart",
            return_value="Транспорт",
        ):
            await quick_add(message, FakeState())

        rows = db.get_transactions(
            1,
            db.user_today(1).isoformat(),
            db.user_today(1).isoformat(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 50000)
        self.assertEqual(rows[0]["description"], "такси")
        self.assertIn("Записано", message.answer.await_args_list[0].args[0])

    async def test_quick_add_accepts_yesterday_and_explicit_date(self):
        today = db.user_today(1)
        with patch(
            "services.transactions.categorize_smart",
            return_value="Транспорт",
        ):
            await quick_add(fake_message("вчера 500 такси"), FakeState())
            await quick_add(
                fake_message("700 автобус 03.09.2026"),
                FakeState(),
            )
        rows = db.get_recent_transactions(1, limit=10)
        dates = {row["description"]: row["op_date"] for row in rows}
        self.assertEqual(
            dates["такси"],
            (today - timedelta(days=1)).isoformat(),
        )
        self.assertEqual(dates["автобус"], "2026-09-03")

    async def test_sql_search_and_filters_never_leak_other_user(self):
        own_category = db.get_category_id_by_name(1, "Транспорт")
        other_category = db.get_category_id_by_name(2, "Транспорт")
        day = db.user_today(1).isoformat()
        db.add_transaction(
            1, "expense", 1000, own_category, None, "Alpha Market",
            "такси домой", day,
        )
        db.add_transaction(
            1, "income", 2000, own_category, None, "Работа",
            "зарплата", day,
        )
        db.add_transaction(
            2, "expense", 9999, other_category, None, "Alpha Market",
            "такси чужое", day,
        )
        rows = db.get_recent_transactions(
            1,
            query="alpha",
            category_id=own_category,
            tx_type="expense",
        )
        self.assertEqual([row["description"] for row in rows], ["такси домой"])
        self.assertEqual(
            db.count_all_transactions(
                1,
                query="такси",
                category_id=other_category,
            ),
            0,
        )

    async def test_catalog_views_are_paginated_and_keep_compound_emoji(self):
        family = "👨‍👩‍👧‍👦"
        db.add_category(1, "Семья", family)
        for index in range(10):
            db.add_category(1, f"Категория {index}")
            db.add_payment_method(1, f"Карта {index}")
        categories_text, categories_markup = _categories_view(1)
        payments_text, payments_markup = _payments_view(1)
        self.assertIn("Страница 1", categories_text)
        self.assertIn("Страница 1", payments_text)
        self.assertTrue(any(
            button.callback_data == "catalog_cat_page:1"
            for row in categories_markup.inline_keyboard
            for button in row
        ))
        self.assertTrue(any(
            button.callback_data == "catalog_pay_page:1"
            for row in payments_markup.inline_keyboard
            for button in row
        ))
        self.assertEqual(
            next(
                row["emoji"]
                for row in db.get_categories(1)
                if row["name"] == "Семья"
            ),
            family,
        )
        category_picker = categories_keyboard(1, "cat")
        payment_picker = payments_keyboard(1, "pay")
        self.assertLessEqual(len(category_picker.inline_keyboard), 9)
        self.assertLessEqual(len(payment_picker.inline_keyboard), 9)
        self.assertTrue(any(
            button.callback_data == "pickcat:1:cat"
            for row in category_picker.inline_keyboard
            for button in row
        ))
        self.assertTrue(any(
            button.callback_data == "pickpay:1:pay"
            for row in payment_picker.inline_keyboard
            for button in row
        ))

    async def test_stats_with_expenses_builds_charts_off_event_loop(self):
        day = db.user_today(1).isoformat()
        db.add_transaction(
            1,
            "expense",
            15000,
            db.get_category_id_by_name(1, "Продукты"),
            None,
            "MAGNUM",
            "Молоко",
            day,
        )
        message = SimpleNamespace(
            answer=AsyncMock(),
            answer_photo=AsyncMock(),
        )
        image = io.BytesIO(b"png")
        with patch(
            "handlers.stats.charts.pie_chart_by_category",
            return_value=image,
        ):
            await _send_stats(message, 1, day, day, "за сегодня")
        message.answer_photo.assert_awaited_once()

    async def test_add_can_go_back_to_type_after_amount(self):
        from handlers.common import ManualEntry

        state = FakeState()
        await add_choose_type(fake_callback("type:expense"), state)
        self.assertEqual(state.current, ManualEntry.entering_amount)
        callback = fake_callback("add_back:type")
        await add_go_back(callback, state)
        self.assertEqual(state.current, ManualEntry.choosing_type)
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        callbacks = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }
        self.assertEqual(callbacks, {"type:expense", "type:income"})

    async def test_cancel_clears_active_scenario(self):
        message = fake_message("/cancel")
        state = FakeState("ManualEntry:entering_amount")
        await cmd_cancel(message, state)
        self.assertTrue(state.cleared)
        self.assertIn("сброшен", message.answer.await_args.args[0])

    async def test_danger_zone_requires_exact_phrase(self):
        category_id = db.get_category_id_by_name(1, "Продукты")
        db.add_transaction(
            1, "expense", 10000, category_id, None, None, "keep",
            db.user_today(1).isoformat(),
        )
        wrong = fake_message("удалить всё")
        await danger_confirm2(wrong, FakeState("DangerZone:entering_phrase"))
        self.assertEqual(db.count_user_data(1), 1)

        exact = fake_message("УДАЛИТЬ ВСЁ")
        await danger_confirm2(exact, FakeState("DangerZone:entering_phrase"))
        self.assertEqual(db.count_user_data(1), 0)

    async def test_double_receipt_save_callback_is_idempotent(self):
        draft = {
            "store": "Test",
            "date": db.user_today(1).isoformat(),
            "time": "12:00",
            "items": [
                {
                    "name": "Молоко",
                    "price": 50000,
                    "category": "Продукты",
                }
            ],
            "total": 50000,
            "source": "test",
        }
        db.save_state(RECEIPT_SCOPE, "double", draft, user_id=1)
        callback = fake_callback("recv_save:double")

        await confirm_receipt_save(callback)
        await confirm_receipt_save(callback)

        with db.get_conn() as conn:
            receipts = conn.execute(
                "SELECT COUNT(*) FROM receipts WHERE user_id=1"
            ).fetchone()[0]
            transactions = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id=1"
            ).fetchone()[0]
        self.assertEqual((receipts, transactions), (1, 1))
        self.assertIn(
            "устарел",
            callback.answer.await_args_list[-1].args[0],
        )

    async def test_ambiguous_bank_draft_requires_explicit_type(self):
        draft = {
            "store": "MAGNUM",
            "date": db.user_today(1).isoformat(),
            "time": None,
            "items": [
                {
                    "name": "MAGNUM",
                    "price": 500000,
                    "category": "Продукты",
                }
            ],
            "total": 500000,
            "source": "bank_notification",
            "tx_type": "expense",
            "ambiguous_type": True,
        }
        db.save_state(RECEIPT_SCOPE, "bank_amb", draft, user_id=1)
        callback = fake_callback("recv_save:bank_amb")
        await confirm_receipt_save(callback)
        with db.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertIn("выбери", callback.answer.await_args.args[0])

        db.update_receipt_draft_type(1, "bank_amb", "income")
        await confirm_receipt_save(callback)
        with db.get_conn() as conn:
            stored_type = conn.execute(
                "SELECT type FROM transactions"
            ).fetchone()[0]
        self.assertEqual(stored_type, "income")

    async def test_restore_confirmation_replaces_current_data(self):
        category_id = db.get_category_id_by_name(1, "Продукты")
        db.add_transaction(
            1, "expense", 1000, category_id, None, None, "из копии",
            db.user_today(1).isoformat(),
        )
        payload = db.get_all_user_rows(1)
        db.add_transaction(
            1, "expense", 2000, category_id, None, None, "удалить",
            db.user_today(1).isoformat(),
        )
        draft_id = "1_restore"
        db.save_state(RESTORE_SCOPE, draft_id, payload, user_id=1)
        callback = fake_callback(f"restore_go:{draft_id}")

        await restore_confirm(callback)

        rows = db.get_transactions(
            1,
            "2000-01-01",
            db.user_today(1).isoformat(),
        )
        self.assertEqual([row["description"] for row in rows], ["из копии"])
        self.assertIsNone(db.load_state(RESTORE_SCOPE, draft_id))
        self.assertIn(
            "Данные восстановлены",
            callback.message.edit_text.await_args.args[0],
        )


class SchedulerScenarios(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tempdir.name) / "schedulers.db")
        db.init_db()
        db.ensure_user(1, "tester")
        self.bot = SimpleNamespace(
            send_message=AsyncMock(),
            send_document=AsyncMock(),
        )

    async def asyncTearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    async def test_recurring_scheduler_is_idempotent(self):
        today = db.user_today(1)
        if today.day > 28:
            self.skipTest("Recurring payments intentionally allow days 1-28")
        recurring_id = db.add_recurring(
            1,
            "expense",
            90000,
            db.get_category_id_by_name(1, "Продукты"),
            db.get_default_payment_method_id(1),
            "Аренда",
            today.day,
        )
        await run_recurring_once(self.bot)
        await run_recurring_once(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM recurring_runs WHERE recurring_id=?",
                (recurring_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_digest_scheduler_marks_successful_delivery(self):
        db.set_digest_frequency(1, "day")
        db.add_transaction(
            1,
            "expense",
            12345,
            db.get_category_id_by_name(1, "Продукты"),
            None,
            None,
            "Обед",
            db.user_today(1).isoformat(),
        )
        with patch("schedulers.generate_insight_text", return_value=None):
            await run_digest_once(self.bot)
        self.bot.send_message.assert_awaited_once()
        with db.get_conn() as conn:
            sent = conn.execute(
                "SELECT digest_last_sent FROM users WHERE user_id=1"
            ).fetchone()[0]
        self.assertEqual(sent, db.user_today(1).isoformat())

    async def test_empty_digest_is_marked_without_sending(self):
        db.set_digest_frequency(1, "day")
        with patch("schedulers.generate_insight_text") as insight:
            await run_digest_once(self.bot)
        self.bot.send_message.assert_not_awaited()
        insight.assert_not_called()
        with db.get_conn() as conn:
            sent = conn.execute(
                "SELECT digest_last_sent FROM users WHERE user_id=1"
            ).fetchone()[0]
        self.assertEqual(sent, db.user_today(1).isoformat())

    async def test_idle_scheduler_respects_three_day_threshold(self):
        today = db.user_today(1)
        db.set_idle_reminder_enabled(1, True)
        db.touch_activity(1, (today - timedelta(days=3)).isoformat())
        await run_idle_once(self.bot)
        await run_idle_once(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_backup_scheduler_sends_non_empty_backup_once(self):
        db.set_backup_enabled(1, True)
        db.add_transaction(
            1,
            "expense",
            1000,
            db.get_category_id_by_name(1, "Продукты"),
            None,
            None,
            "Кофе",
            db.user_today(1).isoformat(),
        )
        await run_backup_once(self.bot)
        await run_backup_once(self.bot)
        self.assertEqual(self.bot.send_document.await_count, 1)
        document = self.bot.send_document.await_args.args[1]
        self.assertTrue(document.filename.startswith("backup_"))

    async def test_first_start_is_short_onboarding(self):
        from handlers.base import cmd_start

        db.ensure_user(50, "newbie")
        self.assertFalse(db.is_onboarded(50))
        message = fake_message("/start", user_id=50)
        with patch("handlers.base.apply_user_ui", new_callable=AsyncMock):
            await cmd_start(message, AsyncMock())
        text = message.answer.call_args.args[0]
        self.assertIn("500 такси", text)
        self.assertIn("/privacy", text)
        self.assertNotIn("/export", text)

    async def test_repeat_start_shows_help(self):
        from handlers.base import cmd_start

        db.mark_onboarded(1)
        message = fake_message("/start")
        with patch("handlers.base.apply_user_ui", new_callable=AsyncMock):
            await cmd_start(message, AsyncMock())
        text = message.answer.call_args.args[0]
        self.assertIn("/stats", text)
        self.assertIn("/feedback", text)

    async def test_privacy_accept_stores_version(self):
        from handlers.base import privacy_accept
        from i18n import PRIVACY_VERSION

        db.ensure_user(50, "newbie")
        callback = fake_callback("privacy_accept", user_id=50)
        callback.message.chat = SimpleNamespace(id=50)
        callback.message.answer = AsyncMock()
        with patch("handlers.base.apply_user_ui", new_callable=AsyncMock):
            await privacy_accept(callback, AsyncMock())
        self.assertEqual(db.get_privacy_accepted_version(50), PRIVACY_VERSION)
        self.assertTrue(db.is_onboarded(50))
