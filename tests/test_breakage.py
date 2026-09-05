"""Атаки на деньги, даты, семью, оплату и поиск — ломаем то, что уже «готово»."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import db
from handlers.billing import stars_pre_checkout_ok
from i18n import _load_locale, translation_gaps
from money import MAX_TIYN, MoneyError, tenge_to_tiyn
from services.transactions import parse_quick_add_request


class BreakageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tempdir.name) / "break.db")
        db.init_db()
        db.ensure_user(1, "owner")
        db.ensure_user(2, "writer")
        db.ensure_user(3, "reader")
        db.ensure_user(4, "stranger")

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def _food(self, user_id: int = 1) -> int:
        return db.get_category_id_by_name(user_id, "Продукты")

    def test_locale_keys_match_across_languages(self):
        _load_locale.cache_clear()
        self.assertEqual(translation_gaps(), {})

    def test_quick_add_keeps_thousands_separator(self):
        today = date(2026, 9, 5)
        parsed = parse_quick_add_request("1 000 такси", today)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.amount_text, "1 000")
        self.assertEqual(parsed.description, "такси")
        self.assertEqual(tenge_to_tiyn(parsed.amount_text), 100000)

        plus = parse_quick_add_request("+50 000 зарплата", today)
        self.assertEqual(plus.amount_text, "+50 000")
        self.assertEqual(plus.description, "зарплата")

    def test_quick_add_yesterday_works_in_all_ui_languages(self):
        today = date(2026, 9, 5)
        yesterday = "2026-09-04"
        for text in (
            "вчера 500 такси",
            "Вчера 500 такси",
            "кеше 500 такси",
            "yesterday 500 taxi",
            "Yesterday 700 bus",
        ):
            parsed = parse_quick_add_request(text, today)
            self.assertIsNotNone(parsed, text)
            self.assertEqual(parsed.op_date, yesterday, text)

    def test_quick_add_rejects_impossible_dates(self):
        today = date(2026, 9, 5)
        self.assertIsNone(parse_quick_add_request("500 такси 32.13.2026", today))
        self.assertIsNone(parse_quick_add_request("500 такси 29.02.2025", today))
        self.assertIsNotNone(parse_quick_add_request("500 такси 03.09.2026", today))

    def test_like_wildcards_do_not_match_everything(self):
        day = "2026-09-05"
        db.add_transaction(1, "expense", 1000, self._food(), None, "Alpha", "молоко", day)
        db.add_transaction(1, "expense", 2000, self._food(), None, "Beta", "хлеб", day)
        rows = db.get_recent_transactions(1, query="%")
        self.assertEqual(len(rows), 0)
        rows = db.get_recent_transactions(1, query="_олок")
        self.assertEqual(len(rows), 0)
        rows = db.get_recent_transactions(1, query="молоко")
        self.assertEqual([row["description"] for row in rows], ["молоко"])

    def test_search_never_returns_foreign_book(self):
        day = "2026-09-05"
        own = db.add_transaction(1, "expense", 1000, self._food(1), None, "A", "секрет", day)
        db.add_transaction(4, "expense", 1000, self._food(4), None, "A", "секрет", day)
        self.assertIsNone(db.get_transaction_by_id(4, own))
        self.assertEqual(
            [row["id"] for row in db.get_recent_transactions(4, query="секрет")],
            [db.get_recent_transactions(4, query="секрет")[0]["id"]],
        )
        self.assertNotIn(own, [row["id"] for row in db.get_recent_transactions(4, query="секрет")])

    def test_add_recurring_rejects_day_31_and_garbage_type(self):
        cat = self._food()
        with self.assertRaises(ValueError):
            db.add_recurring(1, "expense", 1000, cat, None, "rent", 31)
        with self.assertRaises(ValueError):
            db.add_recurring(1, "expense", 1000, cat, None, "rent", 0)
        with self.assertRaises(ValueError):
            db.add_recurring(1, "transfer", 1000, cat, None, "rent", 1)

    def test_absurd_amount_is_rejected(self):
        with self.assertRaises(MoneyError):
            tenge_to_tiyn(MAX_TIYN)
        with self.assertRaises((MoneyError, Exception)):
            db.add_transaction(
                1, "expense", MAX_TIYN + 1, self._food(), None, None, "x", "2026-09-05"
            )

    def test_member_wipe_and_restore_do_not_touch_owner_book(self):
        tx_id = db.add_transaction(
            1, "expense", 25000, self._food(), None, None, "owner milk", "2026-09-05"
        )
        code = db.create_book_invite(1, "write")
        self.assertEqual(db.join_book_invite(2, code), "ok")
        self.assertIsNotNone(db.get_transaction_by_id(2, tx_id))

        empty = db.get_all_user_rows(2)
        db.restore_user_backup(2, empty)
        self.assertIsNotNone(db.get_transaction_by_id(1, tx_id))
        self.assertEqual(db.scope_user(2), 1)

        db.delete_all_user_data(2)
        self.assertIsNotNone(db.get_transaction_by_id(1, tx_id))
        self.assertIsNone(db.get_transaction_by_id(2, tx_id))

    def test_owner_restore_keeps_family_members(self):
        db.add_transaction(
            1, "expense", 25000, self._food(), None, None, "keep me", "2026-09-05"
        )
        code = db.create_book_invite(1, "write")
        self.assertEqual(db.join_book_invite(2, code), "ok")
        book_id = db.active_book_id(1)
        snapshot = db.get_all_user_rows(1)

        db.add_transaction(
            1, "expense", 1000, self._food(), None, None, "later", "2026-09-06"
        )
        db.restore_user_backup(1, snapshot)

        self.assertEqual(db.active_book_id(1), book_id)
        self.assertEqual(db.book_role(2, book_id), "write")
        self.assertEqual(db.scope_user(2), 1)
        rows = db.get_recent_transactions(2, query="keep")
        self.assertEqual(len(rows), 1)
        self.assertEqual(db.count_all_transactions(1, query="later"), 0)

    def test_read_member_cannot_change_goal_or_recurring(self):
        goal_id = db.create_goal(1, "Отпуск", 100000, None)
        rec_id = db.add_recurring(1, "expense", 5000, self._food(), None, "net", 5)
        code = db.create_book_invite(1, "read")
        self.assertEqual(db.join_book_invite(3, code), "ok")
        with self.assertRaises(PermissionError):
            db.contribute_to_goal(3, goal_id, 1000)
        with self.assertRaises(PermissionError):
            db.update_recurring(3, rec_id, amount=9000)

    def test_stars_duplicate_heals_missing_pro_and_refund_revokes(self):
        self.assertTrue(db.record_stars_payment("charge-heal", 1, 150))
        self.assertTrue(db.is_pro(1))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE users SET plan=?, plan_until=NULL WHERE user_id=?",
                (db.PLAN_FREE, 1),
            )
        self.assertFalse(db.is_pro(1))
        self.assertFalse(db.record_stars_payment("charge-heal", 1, 150))
        self.assertTrue(db.is_pro(1))

        self.assertTrue(db.refund_stars_payment("charge-heal"))
        self.assertFalse(db.is_pro(1))
        self.assertFalse(db.refund_stars_payment("charge-heal"))
        self.assertFalse(db.record_stars_payment("charge-heal", 1, 150))
        self.assertFalse(db.is_pro(1))

        db.grant_pro(1)
        db.revoke_pro(1)
        self.assertFalse(db.is_pro(1))

    def test_pre_checkout_rejects_wrong_amount_or_currency(self):
        from config import STARS_PRO_PRICE

        self.assertTrue(stars_pre_checkout_ok("pro:1", "XTR", STARS_PRO_PRICE))
        self.assertFalse(stars_pre_checkout_ok("pro:1", "XTR", 1))
        self.assertFalse(stars_pre_checkout_ok("pro:1", "USD", STARS_PRO_PRICE))
        self.assertFalse(stars_pre_checkout_ok("gift:1", "XTR", STARS_PRO_PRICE))

    def test_transfer_cannot_be_mutated_into_expense(self):
        goal_id = db.create_goal(1, "Велосипед", 500000, None)
        tx_id = db.contribute_to_goal(1, goal_id, 10000)
        self.assertFalse(db.update_transaction_type(1, tx_id, "expense"))
        self.assertFalse(db.update_transaction_amount(1, tx_id, 1))
        row = db.get_transaction_by_id(1, tx_id)
        self.assertEqual(row["type"], "transfer")
        self.assertEqual(row["amount"], 10000)
        self.assertEqual(db.get_total_expense(1, "2000-01-01", "2099-01-01"), 0)

    def test_withdraw_cannot_race_below_zero(self):
        goal_id = db.create_goal(1, "Резерв", 500000, None)
        db.contribute_to_goal(1, goal_id, 10000)
        first, reason_ok = db.withdraw_from_goal(1, goal_id, 10000)
        self.assertEqual(reason_ok, "ok")
        self.assertIsNotNone(first)
        second, reason = db.withdraw_from_goal(1, goal_id, 10000)
        self.assertIsNone(second)
        self.assertEqual(reason, "insufficient")
        goal = db.get_goal_by_id(1, goal_id)
        self.assertEqual(int(goal["current_amount"]), 0)

    def test_gemini_quota_refunds_on_api_failure_not_empty_receipt(self):
        from handlers.receipt import _recognize_receipt_image
        from unittest.mock import patch

        day = "2026-09-05"
        with patch(
            "handlers.receipt.prepare_receipt_image",
            return_value=str(Path(self.tempdir.name) / "prep.jpg"),
        ), patch("handlers.receipt.sha256_file", return_value="abc"), patch(
            "handlers.receipt.extract_receipt",
            return_value=(None, "network"),
        ):
            Path(self.tempdir.name, "prep.jpg").write_bytes(b"x")
            result = _recognize_receipt_image(1, "src.jpg", day, "uid")
        self.assertEqual(result["error"], "network")
        self.assertEqual(db.get_gemini_quota_used(1, day), 0)

        with patch(
            "handlers.receipt.prepare_receipt_image",
            return_value=str(Path(self.tempdir.name) / "prep.jpg"),
        ), patch("handlers.receipt.sha256_file", return_value="abc"), patch(
            "handlers.receipt.extract_receipt",
            return_value=({"items": []}, "empty"),
        ):
            result = _recognize_receipt_image(1, "src.jpg", day, "uid")
        self.assertEqual(result["error"], "empty")
        self.assertEqual(db.get_gemini_quota_used(1, day), 1)

    def test_backup_v2_tenge_payload_still_restores(self):
        payload = {
            "version": 2,
            "settings": {"language": "ru"},
            "categories": [{"name": "Продукты", "emoji": "🛒"}],
            "payment_methods": [{"name": "Наличные"}],
            "receipts": [],
            "transactions": [{
                "type": "expense",
                "amount": 12.5,
                "category": "Продукты",
                "op_date": "2026-08-01",
                "description": "v2-row",
            }],
            "recurring": [],
            "budgets": [],
            "goals": [],
            "learned": [],
        }
        db.restore_user_backup(1, payload)
        rows = db.get_recent_transactions(1, query="v2-row")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 1250)

    def test_member_create_writes_audit_log(self):
        code = db.create_book_invite(1, "write")
        self.assertEqual(db.join_book_invite(2, code), "ok")
        book_id = db.active_book_id(1)
        db.add_transaction(
            2, "expense", 1000, self._food(), None, None, "audit-me", "2026-09-05"
        )
        with db.get_conn() as conn:
            rows = conn.execute(
                """SELECT action, actor_user_id FROM audit_log
                   WHERE book_id=? AND action='tx_create'""",
                (book_id,),
            ).fetchall()
        self.assertEqual([(row["action"], row["actor_user_id"]) for row in rows], [
            ("tx_create", 2),
        ])

    def test_sqlite_restore_check_is_self_consistent(self):
        from scripts.backup_sqlite import restore_check

        src = Path(db.DB_PATH)
        dest = Path(self.tempdir.name) / "restore-check.db"
        restore_check(src, dest)
        self.assertTrue(dest.exists())
        self.assertGreater(dest.stat().st_size, 0)


class DigestDueTests(unittest.TestCase):
    def test_digest_due_boundaries(self):
        from schedulers import _digest_due

        today = date(2026, 9, 5)
        self.assertFalse(_digest_due("off", None, today))
        self.assertTrue(_digest_due("day", None, today))
        self.assertFalse(_digest_due("day", "2026-09-05", today))
        self.assertTrue(_digest_due("day", "2026-09-04", today))
        self.assertFalse(_digest_due("week", "2026-08-30", today))
        self.assertTrue(_digest_due("week", "2026-08-29", today))
        self.assertFalse(_digest_due("month", "2026-09-01", today))
        self.assertTrue(_digest_due("month", "2026-08-31", today))
        self.assertFalse(_digest_due("year", "2026-01-01", today))
        self.assertTrue(_digest_due("year", "2025-12-31", today))


class QuickAddDateRegression(unittest.TestCase):
    def test_explicit_date_overrides_yesterday_prefix(self):
        parsed = parse_quick_add_request(
            "вчера 500 такси 03.09.2026",
            date(2026, 9, 5),
        )
        self.assertEqual(parsed.op_date, "2026-09-03")
        self.assertEqual(parsed.description, "такси")


if __name__ == "__main__":
    unittest.main()
