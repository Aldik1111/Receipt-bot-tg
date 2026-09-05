"""Семейная книга, тариф Pro и серверные бэкапы — без сети."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import db
from handlers.family import start_join_code
from scripts.backup_sqlite import prune_old_backups


class FamilyBillingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tempdir.name) / "family.db")
        db.init_db()
        db.ensure_user(1, "owner")
        db.ensure_user(2, "writer")
        db.ensure_user(3, "reader")
        db.ensure_user(4, "stranger")

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def _add(self, user_id: int, desc: str = "milk") -> int:
        cat = db.get_category_id_by_name(user_id, "Продукты")
        return db.add_transaction(
            user_id, "expense", 10000, cat, None, None, desc, "2026-09-05"
        )

    def test_personal_books_are_isolated(self):
        tx_id = self._add(1)
        self.assertIsNotNone(db.get_transaction_by_id(1, tx_id))
        self.assertIsNone(db.get_transaction_by_id(2, tx_id))
        self.assertEqual(db.scope_user(1), 1)
        self.assertEqual(db.book_role(1), "owner")

    def test_write_member_sees_and_adds_owner_data(self):
        owner_tx = self._add(1, "owner milk")
        code = db.create_book_invite(1, "write")
        self.assertEqual(db.join_book_invite(2, code), "ok")
        self.assertEqual(db.scope_user(2), 1)
        self.assertEqual(db.book_role(2), "write")
        self.assertIsNotNone(db.get_transaction_by_id(2, owner_tx))
        member_tx = self._add(2, "member bread")
        row = db.get_transaction_by_id(1, member_tx)
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], 1)
        self.assertEqual(row["created_by"], 2)
        self.assertEqual(db.get_transaction_by_id(4, member_tx), None)

    def test_read_member_cannot_write(self):
        owner_tx = self._add(1)
        code = db.create_book_invite(1, "read")
        self.assertEqual(db.join_book_invite(3, code), "ok")
        self.assertTrue(db.can_write_book(1))
        self.assertFalse(db.can_write_book(3))
        self.assertIsNotNone(db.get_transaction_by_id(3, owner_tx))
        with self.assertRaises(PermissionError):
            self._add(3)
        self.assertIsNone(db.get_transaction_by_id(4, owner_tx))

    def test_invite_is_single_use_and_can_expire(self):
        code = db.create_book_invite(1, "write")
        self.assertEqual(db.join_book_invite(2, f"join_{code}"), "ok")
        self.assertEqual(db.join_book_invite(3, code), "used")
        fresh = db.create_book_invite(1, "read")
        expired = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE book_invites SET expires_at=? WHERE code=?",
                (expired, fresh),
            )
        self.assertEqual(db.join_book_invite(3, fresh), "expired")
        self.assertEqual(db.join_book_invite(3, "NOSUCH"), "invalid")

    def test_leave_returns_to_personal_book(self):
        code = db.create_book_invite(1, "write")
        db.join_book_invite(2, code)
        owner_tx = self._add(1)
        self.assertTrue(db.leave_active_book(2))
        self.assertEqual(db.scope_user(2), 2)
        self.assertIsNone(db.get_transaction_by_id(2, owner_tx))
        self.assertFalse(db.leave_active_book(1))

    def test_start_payload_parses_join_code(self):
        self.assertEqual(start_join_code("/start join_AB12CD34"), "AB12CD34")
        self.assertIsNone(start_join_code("/start"))

    def test_stars_payment_is_idempotent(self):
        self.assertTrue(db.record_stars_payment("charge-1", 1, 150))
        self.assertTrue(db.is_pro(1))
        self.assertFalse(db.record_stars_payment("charge-1", 1, 150))
        with db.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        self.assertEqual(count, 1)

    def test_pro_quota_is_higher_until_expiry(self):
        db.grant_pro(1)
        for expected in range(1, 12):
            ok, used = db.try_consume_gemini_quota(1, "2026-09-05")
            self.assertTrue(ok)
            self.assertEqual(used, expected)
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE users SET plan=?, plan_until=? WHERE user_id=?",
                (db.PLAN_PRO, past, 2),
            )
        self.assertFalse(db.is_pro(2))
        self.assertEqual(db.gemini_daily_limit(2), db.GEMINI_DAILY_LIMIT)

    def test_scheduler_health_after_touch(self):
        for name in db.SCHEDULER_INTERVALS:
            db.touch_scheduler(name)
        health = db.scheduler_health()
        self.assertTrue(health["schedulers_ok"])
        self.assertTrue(all(item["ok"] for item in health["schedulers"].values()))

    def test_backup_prune_removes_old_files(self):
        folder = Path(self.tempdir.name) / "backups"
        folder.mkdir()
        old = folder / "budget-20200101-000000.db"
        fresh = folder / "budget-20260905-000000.db"
        old.write_bytes(b"old")
        fresh.write_bytes(b"new")
        old_time = datetime.now().timestamp() - 30 * 86400
        import os

        os.utime(old, (old_time, old_time))
        removed = prune_old_backups(folder, retain_days=14)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
