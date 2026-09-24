"""Regression coverage for money input and localized quick-add defaults."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from money import MAX_TIYN, MoneyError, parse_positive_amount, tenge_to_tiyn
from services.transactions import create_quick_transaction


class MoneyInputTests(unittest.TestCase):
    def test_extreme_amounts_are_rejected_without_decimal_errors(self):
        for value in ("1e100", "1e999999999", "9" * 100):
            with self.subTest(value=value):
                with self.assertRaises(MoneyError):
                    tenge_to_tiyn(value)
                self.assertIsNone(parse_positive_amount(value))

    def test_amounts_rounding_to_zero_are_rejected(self):
        for value in ("0.001", "0.0049", "1e-999999999"):
            with self.subTest(value=value):
                with self.assertRaises(MoneyError):
                    tenge_to_tiyn(value)
                self.assertIsNone(parse_positive_amount(value))

    def test_valid_rounding_and_upper_boundary_are_preserved(self):
        self.assertEqual(parse_positive_amount("0.005"), 1)
        self.assertEqual(parse_positive_amount("1.005"), 101)
        self.assertEqual(parse_positive_amount("10000000000000"), MAX_TIYN)
        self.assertIsNone(parse_positive_amount("10000000000000.01"))


class QuickAddCategoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", str(Path(tmp.name) / "test.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()

    def assert_default_category(self, user_id, expected):
        result = create_quick_transaction(user_id, "500", "   ")
        self.assertIsNotNone(result.category_id)
        self.assertEqual(result.category_name, expected)
        row = db.get_transaction_by_id(user_id, result.transaction_id)
        self.assertEqual(row["category_name"], expected)
        self.assertEqual(row["amount"], 50000)

    def test_bare_amount_uses_localized_category(self):
        for user_id, lang, expected in ((1, "ru", "Прочее"), (2, "en", "Other"), (3, "kk", "Басқа")):
            with self.subTest(language=lang):
                db.ensure_user(user_id, "test", language=lang)
                self.assert_default_category(user_id, expected)

    def test_bare_amount_uses_family_owner_catalog_language(self):
        db.ensure_user(1, "owner", language="en")
        db.ensure_user(2, "member", language="ru")
        code = db.create_book_invite(1, "write")
        self.assertEqual(db.join_book_invite(2, code), "ok")
        self.assert_default_category(2, "Other")
