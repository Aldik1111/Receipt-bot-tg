"""Регресс-тесты критичных путей без сети и настоящей Telegram/Gemini.

Запуск из корня проекта:
    python -m unittest discover -s tests -v
"""

import hashlib
import hmac
import io
import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from openpyxl import Workbook

import bank_import
import bot
import db
import export
import webapp_api
from categorizer import categorize
from webapp_auth import validate_init_data


class CategorizerTests(unittest.TestCase):
    def test_keyword_match_is_case_insensitive(self):
        self.assertEqual(categorize("МОЛОКО 3.2%"), "Продукты")
        self.assertEqual(categorize("Поездка на ТАКСИ"), "Транспорт")

    def test_unknown_item_falls_back_to_other(self):
        self.assertEqual(categorize("xyz-неизвестный-товар"), "Прочее")

    def test_custom_dictionary_can_be_injected(self):
        categories = {"A": ["alpha"], "B": ["beta"], "Прочее": []}
        self.assertEqual(categorize("contains beta", categories), "B")


class BankImportTests(unittest.TestCase):
    def test_expense_with_thousands_and_non_breaking_space(self):
        parsed = bank_import.parse_bank_notification(
            "Kaspi Gold: оплата 12\u00a0345,67 KZT в MAGNUM"
        )
        self.assertEqual(parsed["amount"], 12345.67)
        self.assertEqual(parsed["type"], "expense")
        self.assertEqual(parsed["store"], "MAGNUM")

    def test_income_is_detected(self):
        parsed = bank_import.parse_bank_notification(
            "Halyk: зачисление 50 000 тенге"
        )
        self.assertEqual(parsed["amount"], 50000.0)
        self.assertEqual(parsed["type"], "income")

    def test_message_without_currency_is_rejected(self):
        self.assertIsNone(bank_import.parse_bank_notification("Покупка на сумму 5000"))


class ImportExportTests(unittest.TestCase):
    def test_csv_round_trip(self):
        source = [{
            "op_date": "2026-08-20",
            "op_time": "12:30",
            "type": "expense",
            "amount": 1250.5,
            "category_name": "Продукты",
            "payment_name": "Карта",
            "store": "Magnum",
            "description": "Молоко",
        }]
        data = export.export_csv(source).read()
        parsed = export.parse_import_file(data, "operations.csv")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["date"], "2026-08-20")
        self.assertEqual(parsed[0]["amount"], 1250.5)
        self.assertEqual(parsed[0]["description"], "Молоко")
        self.assertEqual(parsed[0]["type"], "expense")
        self.assertEqual(parsed[0]["category"], "Продукты")
        self.assertEqual(parsed[0]["payment"], "Карта")
        self.assertEqual(parsed[0]["store"], "Magnum")

    def test_own_export_keeps_income_and_expense_apart(self):
        """Своя выгрузка пишет только положительные суммы, направление — в
        колонке «Тип». Раньше импорт смотрел лишь на знак и превращал в расход
        весь файл, включая зарплату."""
        source = [
            {
                "op_date": "2026-08-20", "op_time": "10:00", "type": "income",
                "amount": 50000.0, "category_name": "Прочее",
                "payment_name": "Карта", "store": None, "description": "Зарплата",
            },
            {
                "op_date": "2026-08-21", "op_time": "12:00", "type": "expense",
                "amount": 700.0, "category_name": "Транспорт",
                "payment_name": "Наличные", "store": None, "description": "Такси",
            },
        ]
        data = export.export_csv(source).read()
        parsed = export.parse_import_file(data, "operations.csv")

        self.assertEqual([r["type"] for r in parsed], ["income", "expense"])
        self.assertEqual([r["amount"] for r in parsed], [50000.0, 700.0])
        self.assertEqual([r["category"] for r in parsed], ["Прочее", "Транспорт"])

    def test_own_xlsx_export_keeps_type(self):
        source = [{
            "op_date": "2026-08-20", "op_time": "10:00", "type": "income",
            "amount": 1000.0, "category_name": "Прочее",
            "payment_name": "Карта", "store": None, "description": "Возврат",
        }]
        data = export.export_xlsx(source).read()
        parsed = export.parse_import_file(data, "operations.xlsx")
        self.assertEqual(parsed[0]["type"], "income")
        self.assertEqual(parsed[0]["amount"], 1000.0)

    def test_semicolon_delimited_bank_csv(self):
        data = (
            "Дата;Сумма;Описание\r\n"
            "20.08.2026;-1 500,50;Такси\r\n"
        ).encode("utf-8")
        parsed = export.parse_import_file(data, "bank.csv")
        self.assertEqual(parsed, [{
            "amount": 1500.5,
            "type": "expense",
            "date": "2026-08-20",
            "description": "Такси",
            "category": None,
            "payment": None,
            "store": None,
        }])

    def test_bank_csv_without_type_column_still_uses_sign(self):
        """Выписка без колонки типа и без минусов - по-прежнему считаем расходом."""
        data = (
            "Дата;Сумма;Описание\r\n"
            "20.08.2026;1 500,50;Такси\r\n"
            "21.08.2026;300;Кофе\r\n"
        ).encode("utf-8")
        parsed = export.parse_import_file(data, "bank.csv")
        self.assertEqual([r["type"] for r in parsed], ["expense", "expense"])

    def test_explicit_type_survives_when_file_has_no_minuses(self):
        """Смешанный файл с колонкой типа: эвристика «нет минусов - всё расход»
        не должна перетирать явно указанный доход."""
        data = (
            "Дата;Тип;Сумма;Описание\r\n"
            "20.08.2026;Доход;50000;Зарплата\r\n"
            "21.08.2026;Расход;700;Такси\r\n"
        ).encode("utf-8")
        parsed = export.parse_import_file(data, "bank.csv")
        self.assertEqual([r["type"] for r in parsed], ["income", "expense"])

    def test_unknown_type_word_falls_back_to_sign(self):
        data = (
            "Дата;Тип;Сумма;Описание\r\n"
            "20.08.2026;непонятно;-700;Такси\r\n"
        ).encode("utf-8")
        parsed = export.parse_import_file(data, "bank.csv")
        self.assertEqual(parsed[0]["type"], "expense")
        self.assertEqual(parsed[0]["amount"], 700.0)

    def test_zero_and_non_finite_amounts_are_skipped(self):
        data = (
            "Дата;Сумма;Описание\r\n"
            "20.08.2026;0;Ноль\r\n"
            "20.08.2026;nan;Мусор\r\n"
            "20.08.2026;-500;Такси\r\n"
        ).encode("utf-8")
        parsed = export.parse_import_file(data, "bank.csv")
        self.assertEqual([r["description"] for r in parsed], ["Такси"])

    def test_cp1251_csv(self):
        data = "Дата;Сумма;Описание\r\n20.08.2026;-100;Такси\r\n".encode("cp1251")
        parsed = export.parse_import_file(data, "bank.csv")
        self.assertEqual(parsed[0]["description"], "Такси")
        self.assertEqual(parsed[0]["amount"], 100.0)

    def test_xlsx_with_datetime_cell(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Дата", "Сумма", "Описание"])
        sheet.append([datetime(2026, 8, 20), -700, "Кофе"])
        buf = io.BytesIO()
        workbook.save(buf)

        parsed = export.parse_import_file(buf.getvalue(), "bank.xlsx")
        self.assertEqual(parsed[0]["date"], "2026-08-20")
        self.assertEqual(parsed[0]["type"], "expense")

    def test_xls_is_not_parsed_as_xlsx(self):
        parsed = export.parse_import_file(b"not-excel", "bank.xls")
        self.assertEqual(parsed, [])


    def test_parse_positive_amount_rejects_nan(self):
        from formatting import parse_positive_amount, hx

        self.assertIsNone(parse_positive_amount("nan"))
        self.assertIsNone(parse_positive_amount("inf"))
        self.assertEqual(parse_positive_amount("12,5"), 12.5)
        self.assertEqual(hx("<b>x</b>"), "&lt;b&gt;x&lt;/b&gt;")
    def test_three_months_starts_two_calendar_months_back(self):
        from bot import _month_start

        self.assertEqual(_month_start(date(2026, 8, 20), months_back=2), date(2026, 6, 1))
        self.assertEqual(_month_start(date(2026, 1, 15), months_back=2), date(2025, 11, 1))


class GeminiParseTests(unittest.TestCase):
    def test_comma_price_does_not_drop_other_items(self):
        from gemini_engine import _parse_item_price

        self.assertEqual(_parse_item_price("123,45"), 123.45)
        self.assertEqual(_parse_item_price(10), 10.0)
        self.assertIsNone(_parse_item_price("abc"))
        self.assertIsNone(_parse_item_price(float("nan")))
        self.assertIsNone(_parse_item_price(float("inf")))
        self.assertIsNone(_parse_item_price(-5))

    def test_sanitize_drops_invalid_fields_keeps_good_items(self):
        from gemini_engine import sanitize_gemini_receipt

        parsed = sanitize_gemini_receipt({
            "store": "x" * 500,
            "date": "20.08.2026",
            "time": "99:99",
            "items": [
                {"name": "ok", "price": "12,5"},
                {"name": "bad", "price": -1},
                {"name": "nan", "price": float("nan")},
            ],
            "total": "not-a-number",
        })
        self.assertEqual(parsed["items"], [{"name": "ok", "price": 12.5}])
        self.assertEqual(parsed["total"], 12.5)
        self.assertIsNone(parsed["date"])
        self.assertEqual(len(parsed["store"]), 120)

    def test_mime_from_magic_bytes(self):
        from gemini_engine import _guess_mime_type

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
            path = fh.name
        try:
            self.assertEqual(_guess_mime_type(path), "image/png")
        finally:
            os.unlink(path)


def _signed_init_data(
    bot_token: str,
    *,
    user: dict | str = None,
    auth_date: int | str = None,
) -> str:
    if user is None:
        user = {"id": 42, "first_name": "Test"}
    if not isinstance(user, str):
        user = json.dumps(user, separators=(",", ":"))
    if auth_date is None:
        auth_date = int(time.time())

    pairs = {"auth_date": str(auth_date), "query_id": "q1", "user": user}
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class WebAppAuthTests(unittest.TestCase):
    TOKEN = "123456:test-token"

    def test_valid_signature_returns_user(self):
        user = validate_init_data(_signed_init_data(self.TOKEN), self.TOKEN)
        self.assertEqual(user["id"], 42)

    def test_tampered_payload_is_rejected(self):
        data = _signed_init_data(self.TOKEN).replace("%22id%22%3A42", "%22id%22%3A99")
        self.assertIsNone(validate_init_data(data, self.TOKEN))

    def test_expired_and_far_future_payloads_are_rejected(self):
        old = int(time.time()) - 3600 - 1
        future = int(time.time()) + 5 * 60
        self.assertIsNone(validate_init_data(
            _signed_init_data(self.TOKEN, auth_date=old), self.TOKEN
        ))
        self.assertIsNone(validate_init_data(
            _signed_init_data(self.TOKEN, auth_date=future), self.TOKEN
        ))

    def test_malformed_signed_fields_return_none_instead_of_raising(self):
        malformed_date = _signed_init_data(self.TOKEN, auth_date="not-a-number")
        malformed_user = _signed_init_data(self.TOKEN, user="{broken-json")
        self.assertIsNone(validate_init_data(malformed_date, self.TOKEN))
        self.assertIsNone(validate_init_data(malformed_user, self.TOKEN))


class AtomicReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tempdir.name) / "test.db")
        db.init_db()
        db.ensure_user(1, "owner")
        db.ensure_user(2, "other")

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    @staticmethod
    def _draft():
        return {
            "store": "Test Store",
            "date": "2026-08-20",
            "time": "12:30",
            "items": [
                {"name": "good", "price": 100.0, "category": "Продукты"},
                {"name": "bad", "price": 200.0, "category": "Транспорт"},
            ],
            "total": 300.0,
            "raw_text": "raw",
            "source": "test",
        }

    def _counts(self):
        with db.get_conn() as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            )

    def test_second_click_does_not_duplicate_receipt(self):
        draft = self._draft()
        db.save_state("receipt_draft", "draft", draft, user_id=1)
        self.assertIsNotNone(db.save_receipt_draft(1, "draft", "2026-08-20"))
        self.assertIsNone(db.save_receipt_draft(1, "draft", "2026-08-20"))
        self.assertEqual(self._counts(), (1, 2))

    def test_failure_rolls_back_and_preserves_draft(self):
        draft = self._draft()
        db.save_state("receipt_draft", "draft", draft, user_id=1)
        with db.get_conn() as conn:
            conn.execute(
                """CREATE TRIGGER fail_item BEFORE INSERT ON transactions
                   WHEN NEW.description='bad'
                   BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""
            )

        with self.assertRaises(Exception):
            db.save_receipt_draft(1, "draft", "2026-08-20")

        self.assertEqual(self._counts(), (0, 0))
        self.assertEqual(db.load_state("receipt_draft", "draft"), draft)

    def test_other_user_cannot_save_draft(self):
        db.save_state("receipt_draft", "draft", self._draft(), user_id=1)
        self.assertIsNone(db.save_receipt_draft(2, "draft", "2026-08-20"))
        self.assertEqual(self._counts(), (0, 0))
        self.assertIsNotNone(db.load_state("receipt_draft", "draft"))

    def test_receipt_draft_items_can_be_edited_and_deleted_only_by_owner(self):
        db.save_state("receipt_draft", "draft", self._draft(), user_id=1)

        self.assertIsNone(
            db.update_receipt_draft_item(2, "draft", 0, "Чужая правка", 1)
        )
        parsed = db.update_receipt_draft_item(1, "draft", 0, "Молоко", 450.5)
        self.assertEqual(parsed["items"][0]["name"], "Молоко")
        self.assertEqual(parsed["items"][0]["price"], 450.5)

        parsed = db.delete_receipt_draft_item(1, "draft", 1)
        self.assertEqual(len(parsed["items"]), 1)
        with self.assertRaises(ValueError):
            db.delete_receipt_draft_item(1, "draft", 0)
        self.assertEqual(len(db.get_receipt_draft(1, "draft")["items"]), 1)
        self.assertIsNone(db.get_receipt_draft(2, "draft"))

    def test_receipt_total_mismatch_requires_choice_and_resolves_exactly(self):
        draft = self._draft()
        draft["total"] = 250
        db.save_state("receipt_draft", "items", draft, user_id=1)
        with self.assertRaises(ValueError):
            db.save_receipt_draft(1, "items", "2026-08-20")
        self.assertEqual(self._counts(), (0, 0))
        self.assertIsNotNone(db.get_receipt_draft(1, "items"))

        parsed = db.resolve_receipt_draft_total(1, "items", use_receipt_total=False)
        self.assertEqual(parsed["total"], 300)
        receipt_id, _ = db.save_receipt_draft(1, "items", "2026-08-20")
        self.assertIsInstance(receipt_id, int)

        draft = self._draft()
        draft["total"] = 250
        db.save_state("receipt_draft", "receipt", draft, user_id=1)
        parsed = db.resolve_receipt_draft_total(1, "receipt", use_receipt_total=True)
        self.assertEqual(round(sum(item["price"] for item in parsed["items"]), 2), 250)
        self.assertTrue(all(item["price"] > 0 for item in parsed["items"]))
        db.save_receipt_draft(1, "receipt", "2026-08-20")
        with db.get_conn() as conn:
            stored_total = conn.execute(
                """SELECT SUM(t.amount) FROM transactions t
                   JOIN receipts r ON r.id=t.receipt_id
                   WHERE r.user_id=1 AND r.id=(
                       SELECT MAX(id) FROM receipts WHERE user_id=1
                   )"""
            ).fetchone()[0]
        self.assertEqual(stored_total, 250)

    def test_receipt_preview_blocks_save_until_total_is_resolved(self):
        draft = self._draft()
        draft["total"] = 250
        text, keyboard = bot._receipt_draft_view(draft, "draft")
        self.assertIn("Итоги расходятся", text)
        callbacks = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("recv_total_items:draft", callbacks)
        self.assertIn("recv_total_receipt:draft", callbacks)
        self.assertNotIn("recv_save:draft", callbacks)
        self.assertIn("recv_item:draft:0", callbacks)

    def test_bank_income_draft_is_saved_as_income(self):
        draft = self._draft()
        draft["tx_type"] = "income"
        draft["source"] = "bank_notification"
        db.save_state("receipt_draft", "bank", draft, user_id=1)
        self.assertIsNotNone(db.save_receipt_draft(1, "bank", "2026-08-20"))
        with db.get_conn() as conn:
            types = [r[0] for r in conn.execute("SELECT type FROM transactions").fetchall()]
        self.assertEqual(types, ["income", "income"])
        with db.get_conn() as conn:
            raw = conn.execute("SELECT raw_text FROM receipts").fetchone()[0]
        self.assertEqual(raw, "")

    def test_delete_category_with_budget_and_recurring(self):
        cat_id = db.get_category_id_by_name(1, "Продукты")
        other_id = db.get_category_id_by_name(1, "Транспорт")
        db.set_budget(1, cat_id, 1000)
        db.add_recurring(1, "expense", 10, cat_id, None, "rent", 1)
        db.learn_category(1, "молоко тест", cat_id)
        db.add_transaction(1, "expense", 5, cat_id, None, None, "milk", date.today().isoformat())
        self.assertTrue(db.delete_category(1, cat_id))
        fallback = db.get_category_id_by_name(1, "Прочее")
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM category_budgets").fetchone()[0], 0)
            rec_cat = conn.execute("SELECT category_id FROM recurring_payments").fetchone()[0]
            learned = conn.execute("SELECT category_id FROM learned_categories").fetchone()[0]
            tx_cat = conn.execute("SELECT category_id FROM transactions").fetchone()[0]
        self.assertEqual(rec_cat, fallback)
        self.assertEqual(learned, fallback)
        self.assertEqual(tx_cat, fallback)
        self.assertIsNone(db.get_category_name(1, cat_id))
        self.assertEqual(db.get_category_name(1, other_id), "Транспорт")

    def test_apply_due_recurring_is_idempotent(self):
        cat_id = db.get_category_id_by_name(1, "Продукты")
        rec_id = db.add_recurring(1, "expense", 99, cat_id, None, "sub", 1)
        today = date.today().replace(day=28).isoformat() if date.today().day >= 1 else date.today().isoformat()
        self.assertTrue(db.apply_due_recurring(rec_id, today))
        self.assertFalse(db.apply_due_recurring(rec_id, today))
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM transactions WHERE description='sub'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_foreign_category_is_not_attached(self):
        other_cat = db.get_category_id_by_name(2, "Продукты")
        fallback = db.get_category_id_by_name(1, "Прочее")
        db.add_transaction(1, "expense", 10, other_cat, None, None, "x", "2026-08-20")
        with db.get_conn() as conn:
            stored = conn.execute("SELECT category_id FROM transactions WHERE user_id=1").fetchone()[0]
        self.assertEqual(stored, fallback)
        self.assertFalse(db.update_transaction_category(1, 1, other_cat))

    def test_transaction_date_type_and_store_updates_are_owner_scoped(self):
        cat_id = db.get_category_id_by_name(1, "Продукты")
        tx_id = db.add_transaction(
            1, "expense", 500, cat_id, None, "Старый", "покупка", "2026-08-20"
        )

        self.assertTrue(db.update_transaction_date(1, tx_id, "2026-09-04"))
        self.assertTrue(db.update_transaction_type(1, tx_id, "income"))
        self.assertTrue(db.update_transaction_store(1, tx_id, "Новый магазин"))
        row = db.get_transaction_by_id(1, tx_id)
        self.assertEqual(row["op_date"], "2026-09-04")
        self.assertEqual(row["type"], "income")
        self.assertEqual(row["store"], "Новый магазин")
        # После смены на доход операция больше не расходует бюджет категории.
        self.assertEqual(db.get_category_spent(1, cat_id, "2026-09-01", "2026-09-30"), 0)

        for update, value in (
            (db.update_transaction_date, "2026-09-05"),
            (db.update_transaction_type, "expense"),
            (db.update_transaction_store, "Чужой магазин"),
        ):
            self.assertFalse(update(2, tx_id, value))
            self.assertFalse(update(1, 999_999, value))

        self.assertFalse(db.update_transaction_date(1, tx_id, "04.09.2026"))
        self.assertFalse(db.update_transaction_type(1, tx_id, "transfer"))
        row = db.get_transaction_by_id(1, tx_id)
        self.assertEqual(row["op_date"], "2026-09-04")
        self.assertEqual(row["type"], "income")
        self.assertEqual(row["store"], "Новый магазин")

    def test_transaction_detail_has_new_edit_controls_and_local_date(self):
        cat_id = db.get_category_id_by_name(1, "Продукты")
        tx_id = db.add_transaction(
            1, "expense", 500, cat_id, None, "Магазин", "покупка", "2026-09-04"
        )
        text, keyboard = bot._tx_detail_view(1, tx_id)
        self.assertIn("Тип: Расход", text)
        self.assertIn("Дата: 04.09.2026", text)
        callbacks = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn(f"tx_date:{tx_id}", callbacks)
        self.assertIn(f"tx_type:{tx_id}", callbacks)
        self.assertIn(f"tx_store:{tx_id}", callbacks)

    def test_protected_category_cannot_be_renamed(self):
        other_id = db.get_category_id_by_name(1, "Прочее")
        self.assertFalse(db.rename_category(1, other_id, "Разное"))
        self.assertEqual(db.get_category_name(1, other_id), "Прочее")

    def test_deleting_last_receipt_item_removes_orphan_receipt(self):
        draft = self._draft()
        draft["items"] = [{"name": "only", "price": 10.0, "category": "Продукты"}]
        draft["total"] = 10.0
        db.save_state("receipt_draft", "one", draft, user_id=1)
        receipt_id, _ = db.save_receipt_draft(1, "one", "2026-08-20")
        with db.get_conn() as conn:
            tx_id = conn.execute("SELECT id FROM transactions WHERE receipt_id=?", (receipt_id,)).fetchone()[0]
        self.assertTrue(db.delete_transaction(1, tx_id))
        self.assertEqual(self._counts(), (0, 0))

    def test_import_draft_commit_is_atomic_and_idempotent(self):
        rows = [
            {"amount": 10, "type": "expense", "date": "2026-08-20", "description": "A", "category": "Продукты"},
            {"amount": 20, "type": "income", "date": "2026-08-20", "description": "B", "category": "Прочее"},
        ]
        db.save_state("import_draft", "1_9", rows, user_id=1)
        pm_id = db.get_default_payment_method_id(1)
        self.assertEqual(db.commit_import_draft(1, "1_9", pm_id, "2026-08-20"), 2)
        self.assertIsNone(db.commit_import_draft(1, "1_9", pm_id, "2026-08-20"))
        with db.get_conn() as conn:
            types = [r[0] for r in conn.execute("SELECT type FROM transactions ORDER BY id").fetchall()]
        self.assertEqual(types, ["expense", "income"])

    def test_import_applies_type_store_and_payment_from_file(self):
        rows = [{
            "amount": 5000, "type": "income", "date": "2026-08-20",
            "description": "Зарплата", "category": "Прочее",
            "payment": "Карта (основная)", "store": "Работа",
        }]
        db.save_state("import_draft", "1_11", rows, user_id=1)
        self.assertEqual(
            db.commit_import_draft(1, "1_11", db.get_default_payment_method_id(1), "2026-08-20"),
            1,
        )
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT type, store, payment_method_id FROM transactions WHERE user_id=1"
            ).fetchone()
        self.assertEqual(row["type"], "income")
        self.assertEqual(row["store"], "Работа")
        self.assertEqual(
            row["payment_method_id"], db.get_payment_method_id_by_name(1, "Карта (основная)")
        )

    def test_unknown_payment_from_file_falls_back_to_default(self):
        rows = [{
            "amount": 100, "type": "expense", "date": "2026-08-20",
            "description": "Кофе", "category": "Прочее",
            "payment": "Банк которого нет", "store": None,
        }]
        db.save_state("import_draft", "1_12", rows, user_id=1)
        default_pm = db.get_default_payment_method_id(1)
        self.assertEqual(db.commit_import_draft(1, "1_12", default_pm, "2026-08-20"), 1)
        with db.get_conn() as conn:
            stored = conn.execute(
                "SELECT payment_method_id FROM transactions WHERE user_id=1"
            ).fetchone()[0]
        self.assertEqual(stored, default_pm)
        with db.get_conn() as conn:
            pm_count = conn.execute(
                "SELECT COUNT(*) FROM payment_methods WHERE user_id=1"
            ).fetchone()[0]
        self.assertEqual(pm_count, 2)  # импорт не создал новую карту

    def test_import_failure_keeps_draft(self):
        rows = [
            {"amount": 1, "type": "expense", "date": "2026-08-20", "description": "ok", "category": "Продукты"},
            {"amount": 1, "type": "expense", "date": "2026-08-20", "description": "bad", "category": "Продукты"},
        ]
        db.save_state("import_draft", "1_10", rows, user_id=1)
        with db.get_conn() as conn:
            conn.execute(
                """CREATE TRIGGER fail_import BEFORE INSERT ON transactions
                   WHEN NEW.description='bad'
                   BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""
            )
        with self.assertRaises(Exception):
            db.commit_import_draft(1, "1_10", db.get_default_payment_method_id(1), "2026-08-20")
        self.assertEqual(self._counts(), (0, 0))
        self.assertEqual(len(db.load_state("import_draft", "1_10")), 2)

    def test_backup_v3_round_trip_is_idempotent(self):
        cat_id = db.get_category_id_by_name(1, "Продукты")
        db.add_transaction(1, "income", 50, cat_id, None, "Shop", "salary", "2026-08-20", "10:00")
        db.add_recurring(1, "expense", 9, cat_id, None, "net", 5)
        db.set_budget(1, cat_id, 100)
        db.create_goal(1, "Отпуск", 1000)
        db.learn_category(1, "кофе", cat_id)
        data = db.get_all_user_rows(1)
        self.assertEqual(data["version"], 3)
        db.restore_user_backup(1, data)
        db.restore_user_backup(1, data)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions WHERE user_id=1").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM recurring_payments WHERE user_id=1").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM savings_goals WHERE user_id=1").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT type FROM transactions WHERE user_id=1").fetchone()[0], "income")

    def test_backup_v3_preserves_recurring_runs_and_prevents_double_apply(self):
        """До этого теста recurring_runs не попадал в бэкап: restore создавал
        повтор с новым id и пустой историей, а планировщик списывал тот же
        платёж повторно в том же месяце."""
        cat_id = db.get_category_id_by_name(1, "Продукты")
        rec_id = db.add_recurring(1, "expense", 500, cat_id, None, "rent", 1)
        today = date.today().replace(day=28).isoformat()
        self.assertTrue(db.apply_due_recurring(rec_id, today))

        data = db.get_all_user_rows(1)
        period = today[:7]
        self.assertEqual(data["recurring"][0]["runs"], [period])

        db.restore_user_backup(1, data)

        with db.get_conn() as conn:
            new_id = conn.execute(
                "SELECT id FROM recurring_payments WHERE user_id=1 AND description='rent'"
            ).fetchone()[0]
        # Тот же месяц, платёж уже шёл до бэкапа - повторный прогон
        # планировщика не должен списать деньги второй раз.
        self.assertFalse(db.apply_due_recurring(new_id, today))
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id=1 AND description='rent'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_restore_rejects_non_dict_payload_and_keeps_existing_data(self):
        cat_id = db.get_category_id_by_name(1, "Продукты")
        db.add_transaction(1, "expense", 10, cat_id, None, None, "keep me", "2026-08-20")
        with self.assertRaises(ValueError):
            db.restore_user_backup(1, ["not", "a", "dict"])
        with db.get_conn() as conn:
            desc = conn.execute("SELECT description FROM transactions WHERE user_id=1").fetchone()[0]
        self.assertEqual(desc, "keep me")

    def test_restore_failure_rolls_back_wipe(self):
        """Если восстановление падает на середине (битые данные внутри
        валидного dict), старые данные пользователя не должны стираться."""
        cat_id = db.get_category_id_by_name(1, "Продукты")
        db.add_transaction(1, "expense", 10, cat_id, None, None, "keep me", "2026-08-20")
        broken = {
            "settings": {}, "categories": [], "payment_methods": [],
            "receipts": [], "recurring": [], "budgets": [], "goals": [], "learned": [],
            # transactions без обязательного op_date - KeyError внутри транзакции
            "transactions": [{"type": "expense", "amount": 5, "description": "x"}],
        }
        with self.assertRaises(Exception):
            db.restore_user_backup(1, broken)
        with db.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM transactions WHERE user_id=1").fetchone()[0]
            desc = conn.execute("SELECT description FROM transactions WHERE user_id=1").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(desc, "keep me")

    def test_language_update_creates_user(self):
        db.ensure_user(9, "newbie")
        db.set_user_language(9, "en")
        self.assertEqual(db.get_user_language(9), "en")


class HtmlEscapingTests(unittest.IsolatedAsyncioTestCase):
    """Магазины, категории, цели и повторы - это текст пользователя (или
    Gemini), а бот отправляет сообщения с parse_mode=HTML. Без hx() строка
    вроде <b>x</b> или <script> ломает разметку сообщения или всплывает как
    чужой HTML. Раньше это было защищено только в превью чека и списке
    операций - здесь регресс на остальные экраны."""

    PAYLOAD = "<script>hack</script>"
    ESCAPED = "&lt;script&gt;hack&lt;/script&gt;"

    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tempdir.name) / "html.db")
        db.init_db()
        db.ensure_user(1, "owner")

    async def asyncTearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def _assert_escaped(self, text: str):
        self.assertNotIn(self.PAYLOAD, text)
        self.assertIn(self.ESCAPED, text)

    async def test_budget_warning_escapes_category_name(self):
        cat_id = db.add_category(1, self.PAYLOAD)
        db.set_budget(1, cat_id, 100)
        db.add_transaction(1, "expense", 150, cat_id, None, None, "over", date.today().isoformat())

        warning = await bot._budget_warning_text(1, cat_id)
        self.assertIsNotNone(warning)
        self._assert_escaped(warning)

    def test_budget_view_escapes_category_name(self):
        cat_id = db.add_category(1, self.PAYLOAD)
        db.set_budget(1, cat_id, 1000)
        text, _ = bot._budget_view(1)
        self._assert_escaped(text)

    def test_tx_detail_view_escapes_all_free_text_fields(self):
        cat_id = db.add_category(1, self.PAYLOAD)
        pm_id = db.add_payment_method(1, self.PAYLOAD)
        tx_id = db.add_transaction(
            1, "expense", 500, cat_id, pm_id, self.PAYLOAD, self.PAYLOAD,
            date.today().isoformat(),
        )
        text, _ = bot._tx_detail_view(1, tx_id)
        # Магазин, описание, категория и способ оплаты - четыре разных поля,
        # каждое должно быть экранировано независимо от других.
        self.assertEqual(text.count(self.ESCAPED), 4)
        self.assertNotIn(self.PAYLOAD, text)

    def test_goals_view_escapes_goal_name(self):
        db.create_goal(1, self.PAYLOAD, 1000)
        text, _ = bot._goals_view(1)
        self._assert_escaped(text)

    def test_recurring_view_escapes_description(self):
        cat_id = db.get_category_id_by_name(1, "Прочее")
        db.add_recurring(1, "expense", 100, cat_id, None, self.PAYLOAD, 5)
        text, _ = bot._recurring_view(1)
        self._assert_escaped(text)


class TransactionsApiTests(unittest.IsolatedAsyncioTestCase):
    TOKEN = "123456:test-token"

    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        self.old_bot_token = webapp_api.BOT_TOKEN
        db.DB_PATH = str(Path(self.tempdir.name) / "api.db")
        webapp_api.BOT_TOKEN = self.TOKEN
        db.init_db()
        db.ensure_user(1, "owner")
        db.ensure_user(2, "other")

        product_id = db.get_category_id_by_name(1, "Продукты")
        transport_id = db.get_category_id_by_name(1, "Транспорт")
        other_product_id = db.get_category_id_by_name(2, "Продукты")
        today = date.today().isoformat()
        rows = []
        for index in range(525):
            rows.append((
                1, "expense", index + 1, product_id,
                f"product-{index}", today, "12:00", f"p-{index}",
            ))
        for index in range(15):
            rows.append((
                1, "expense", index + 1, transport_id,
                f"transport-{index}", today, "12:00", f"t-{index}",
            ))
        rows.append((
            2, "expense", 999, other_product_id,
            "other-user", today, "12:00", "other",
        ))
        # Та же категория, но за пределами любого текущего периода: endpoint
        # обязан исключить её при period=month.
        rows.append((
            1, "expense", 777, product_id,
            "old-product", "2000-01-01", "12:00", "old",
        ))
        with db.get_conn() as conn:
            conn.executemany(
                """INSERT INTO transactions(
                       user_id, type, amount, category_id, description,
                       op_date, op_time, created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )

        self.client = TestClient(TestServer(webapp_api.create_app()))
        await self.client.start_server()
        self.headers = {
            "X-Telegram-Init-Data": _signed_init_data(self.TOKEN, user={"id": 1})
        }

    async def asyncTearDown(self):
        await self.client.close()
        webapp_api.BOT_TOKEN = self.old_bot_token
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    async def test_filtered_total_and_page_are_computed_over_all_rows(self):
        response = await self.client.get(
            "/api/transactions?period=month&category=Продукты&limit=20&offset=510",
            headers=self.headers,
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["total"], 525)
        self.assertEqual(len(payload["items"]), 15)
        self.assertTrue(all(row["category"] == "Продукты" for row in payload["items"]))
        self.assertTrue(all(row["description"] != "other-user" for row in payload["items"]))
        self.assertTrue(all(row["description"] != "old-product" for row in payload["items"]))

    async def test_other_category_has_its_own_total(self):
        response = await self.client.get(
            "/api/transactions?category=Транспорт&limit=200",
            headers=self.headers,
        )
        payload = await response.json()
        self.assertEqual(payload["total"], 15)
        self.assertEqual(len(payload["items"]), 15)

    async def test_pagination_is_bounded_and_invalid_values_return_400(self):
        bounded = await self.client.get(
            "/api/transactions?limit=-10&offset=-5", headers=self.headers
        )
        self.assertEqual(bounded.status, 200)
        self.assertEqual(len((await bounded.json())["items"]), 1)

        invalid = await self.client.get(
            "/api/transactions?limit=many", headers=self.headers
        )
        self.assertEqual(invalid.status, 400)

        incomplete_dates = await self.client.get(
            "/api/transactions?date_from=2026-08-01", headers=self.headers
        )
        self.assertEqual(incomplete_dates.status, 400)

        reversed_dates = await self.client.get(
            "/api/transactions?date_from=2026-09-01&date_to=2026-08-01",
            headers=self.headers,
        )
        self.assertEqual(reversed_dates.status, 400)


if __name__ == "__main__":
    unittest.main()
