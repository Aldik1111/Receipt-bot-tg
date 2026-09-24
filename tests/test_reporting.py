"""SQL reporting scope, money precision, and period regression tests."""

import importlib
import tempfile
import unittest
from pathlib import Path

import db


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tempdir.name) / "reporting.db")
        db.init_db()
        for user in (1, 2, 3, 4):
            db.ensure_user(user, f"user-{user}", language="en")
        self.reporting = importlib.import_module("storage.reporting")
        self.category = db.get_category_id_by_name(1, "Groceries")

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.tempdir.cleanup()

    def add(self, amount, *, user=1, kind="expense", day="2026-09-15", category=None):
        return db.add_transaction(user, kind, amount, category, None, None, None, day)

    def totals(self, user=1):
        return self.reporting.get_summary_totals(user, "2026-09-01", "2026-09-30")

    def categories(self, user=1):
        return [dict(row) for row in self.reporting.get_category_totals(
            user, "2026-09-01", "2026-09-30"
        )]

    def test_empty_period_has_integer_zero_totals_and_no_categories(self):
        self.assertEqual(self.totals(), {"expense": 0, "income": 0})
        self.assertEqual(self.categories(), [])

    def test_inclusive_dates_and_exact_integer_amounts(self):
        self.add(101, day="2026-09-01", category=self.category)
        self.add(202, day="2026-09-30", category=self.category)
        self.add(1000, day="2026-08-31", category=self.category)
        self.add(2000, day="2026-10-01", category=self.category)
        self.add(123456789, kind="income")
        totals = self.totals()
        self.assertEqual(totals, {"expense": 303, "income": 123456789})
        self.assertTrue(all(type(value) is int for value in totals.values()))
        self.assertEqual(self.categories()[0]["amount"], 303)

    def test_categories_group_nulls_sort_descending_and_exclude_income_transfers(self):
        other = db.get_category_id_by_name(1, "Other")
        self.add(300, category=self.category)
        self.add(200, category=self.category)
        self.add(700, category=other)
        uncategorized = [self.add(70), self.add(30)]
        transfer = self.add(9000, category=self.category)
        self.add(8000, kind="income", category=self.category)
        with db.get_conn() as conn:
            conn.executemany("UPDATE transactions SET category_id=NULL WHERE id=?",
                             [(tx,) for tx in uncategorized])
            conn.execute("UPDATE transactions SET type='transfer' WHERE id=?", (transfer,))
        rows = self.categories()
        self.assertEqual([(r["category_name"], r["amount"]) for r in rows],
                         [("Other", 700), ("Groceries", 500), (None, 100)])
        self.assertIsNone(rows[-1]["category_emoji"])
        self.assertTrue(all(type(r["amount"]) is int for r in rows))
        self.assertEqual(self.totals(), {"expense": 1300, "income": 8000})

    def test_active_book_isolation_legacy_nulls_and_foreign_user(self):
        personal = db.active_book_id(1)
        self.add(100, category=self.category)
        legacy = self.add(50, category=self.category)
        self.add(9999, user=4)
        with db.get_conn() as conn:
            conn.execute("UPDATE transactions SET book_id=NULL WHERE id=?", (legacy,))
            family = conn.execute(
                "INSERT INTO books(owner_user_id,name,created_at) VALUES (1,'Family','now')"
            ).lastrowid
            conn.execute("INSERT INTO book_members(book_id,user_id,role,joined_at) "
                         "VALUES (?,1,'owner','now')", (family,))
        self.assertEqual(self.totals(), {"expense": 150, "income": 0})
        self.assertTrue(db.switch_active_book(1, family))
        self.add(700, category=self.category)
        self.assertEqual(self.totals(), {"expense": 700, "income": 0})
        self.assertEqual(self.categories()[0]["amount"], 700)
        self.assertTrue(db.switch_active_book(1, personal))
        self.assertEqual(self.totals(), {"expense": 150, "income": 0})
        self.assertEqual(self.categories()[0]["amount"], 150)

    def test_family_writer_and_reader_see_owner_totals(self):
        self.add(100, category=self.category)
        self.add(9999, user=2)
        for user, role in ((2, "write"), (3, "read")):
            code = db.create_book_invite(1, role)
            self.assertEqual(db.join_book_invite(user, code), "ok")
        self.add(201, user=2, category=self.category)
        for user in (1, 2, 3):
            with self.subTest(user=user):
                self.assertEqual(self.totals(user), {"expense": 301, "income": 0})
                self.assertEqual(self.categories(user)[0]["amount"], 301)
        self.assertEqual(self.categories(4), [])
