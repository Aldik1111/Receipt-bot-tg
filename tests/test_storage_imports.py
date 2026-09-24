"""Storage must work independently of the backwards-compatible db facade."""
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StorageImportTests(unittest.TestCase):
    def run_fresh(self, script):
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_each_module_can_be_imported_first(self):
        for path in sorted((ROOT / "storage").glob("*.py")):
            if path.stem == "__init__":
                continue
            with self.subTest(module=path.stem):
                self.run_fresh(f"""
                    import importlib
                    import inspect
                    import sys
                    import typing
                    module = importlib.import_module('storage.{path.stem}')
                    assert 'db' not in sys.modules
                    for value in vars(module).values():
                        if inspect.isfunction(value) and value.__module__ == module.__name__:
                            typing.get_type_hints(value)
                """)

    def test_storage_operations_without_facade(self):
        self.run_fresh("""
            import sys
            import tempfile
            from pathlib import Path
            from storage import backup, books, budgets, catalog, conn, goals
            from storage import quota, recurring, schema, state, transactions, users, wipe

            with tempfile.TemporaryDirectory() as directory:
                conn.DB_PATH = str(Path(directory) / 'direct.sqlite')
                schema.init_db()
                catalog.ensure_user(1, 'owner', language='en')
                catalog.ensure_user(2, 'member', language='en')
                category = catalog.get_categories(1)[0]['id']
                day = users.user_today(1).isoformat()
                tx = transactions.add_transaction(
                    1, 'expense', 1234, category, None, None, 'direct', day
                )
                assert transactions.get_transaction_by_id(1, tx)['amount'] == 1234
                budgets.set_budget(1, None, 10000)
                assert budgets.get_total_expense(1, day, day) == 1234
                goal = goals.create_goal(1, 'Reserve', 50000)
                goals.contribute_to_goal(1, goal, 2500, op_date=day)
                assert budgets.get_goals_reserved(1) == 2500
                rec = recurring.add_recurring(1, 'expense', 500, category, None, 'rent', 1)
                assert recurring.get_recurring_by_id(1, rec)['amount'] == 500
                state.save_state('direct', 'draft', {'amount': 5}, user_id=1)
                assert state.load_state('direct', 'draft') == {'amount': 5}
                assert quota.try_consume_gemini_quota(1, day, limit=1) == (True, 1)
                assert quota.try_consume_gemini_quota(1, day, limit=1) == (False, 1)
                invite = books.create_book_invite(1, 'write')
                assert books.join_book_invite(2, invite) == 'ok'
                assert transactions.get_transaction_by_id(2, tx)['amount'] == 1234
                snapshot = backup.get_all_user_rows(1)
                backup.restore_user_backup(1, snapshot)
                assert budgets.get_total_expense(2, day, day) == 1234
                wipe.delete_all_user_data(2)
                assert budgets.get_total_expense(1, day, day) == 1234
                assert 'db' not in sys.modules
        """)

    def test_concurrent_first_login_creates_one_complete_user(self):
        self.run_fresh("""
            import tempfile
            import threading
            from concurrent.futures import ThreadPoolExecutor
            from pathlib import Path
            from config import DEFAULT_PAYMENT_METHODS_I18N, default_categories_for
            from storage import catalog, conn, schema

            with tempfile.TemporaryDirectory() as directory:
                conn.DB_PATH = str(Path(directory) / 'concurrent.sqlite')
                schema.init_db()
                barrier = threading.Barrier(16)

                def first_login(_):
                    barrier.wait(timeout=10)
                    catalog.ensure_user(123, 'new-user', language='en')

                with ThreadPoolExecutor(max_workers=16) as pool:
                    list(pool.map(first_login, range(16)))

                with conn.get_conn() as connection:
                    user = connection.execute(
                        'SELECT * FROM users WHERE user_id=123'
                    ).fetchone()
                    assert user is not None
                    categories = connection.execute(
                        'SELECT name FROM categories WHERE user_id=123'
                    ).fetchall()
                    assert sorted(row['name'] for row in categories) == sorted(default_categories_for('en'))
                    payments = connection.execute(
                        'SELECT name FROM payment_methods WHERE user_id=123'
                    ).fetchall()
                    assert sorted(row['name'] for row in payments) == sorted(DEFAULT_PAYMENT_METHODS_I18N['en'])
                    owned = connection.execute(
                        'SELECT id FROM books WHERE owner_user_id=123'
                    ).fetchall()
                    assert len(owned) == 1
                    assert user['active_book_id'] == owned[0]['id']
                    members = connection.execute(
                        'SELECT role FROM book_members WHERE user_id=123'
                    ).fetchall()
                    assert len(members) == 1 and members[0]['role'] == 'owner'
        """)


if __name__ == "__main__":
    unittest.main()
