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
import threading
import time
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch
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


class EmojiGraphemeTests(unittest.TestCase):
    def test_compound_family_emoji_is_one_grapheme(self):
        from formatting import grapheme_count, is_single_emoji

        family = "👨‍👩‍👧‍👦"
        self.assertEqual(grapheme_count(family), 1)
        self.assertTrue(is_single_emoji(family))
        self.assertFalse(is_single_emoji("ab"))
        self.assertFalse(is_single_emoji("😀 😀"))


class ChartTests(unittest.TestCase):
    def test_category_chart_keeps_top_seven_and_groups_remainder(self):
        from charts import top_categories_with_other

        rows = [
            {"category_name": f"C{index}", "amount": (10 - index) * 100}
            for index in range(9)
        ]
        visible = top_categories_with_other(rows)
        self.assertEqual(len(visible), 8)
        self.assertEqual(visible[:2], [("C0", 1000), ("C1", 900)])
        self.assertEqual(visible[-1], ("Прочее", 500))


class BankImportTests(unittest.TestCase):
    def test_expense_with_thousands_and_non_breaking_space(self):
        parsed = bank_import.parse_bank_notification(
            "Kaspi Gold: оплата 12\u00a0345,67 KZT в MAGNUM"
        )
        self.assertEqual(parsed["amount"], 1234567)
        self.assertEqual(parsed["type"], "expense")
        self.assertEqual(parsed["store"], "MAGNUM")

    def test_income_is_detected(self):
        parsed = bank_import.parse_bank_notification(
            "Halyk: зачисление 50 000 тенге"
        )
        self.assertEqual(parsed["amount"], 5000000)
        self.assertEqual(parsed["type"], "income")

    def test_message_without_currency_is_rejected(self):
        self.assertIsNone(bank_import.parse_bank_notification("Покупка на сумму 5000"))

    def test_operation_amount_wins_over_balance(self):
        parsed = bank_import.parse_bank_notification(
            "Оплата 1 200 KZT в COFFEE. Доступно 99 000 KZT"
        )
        self.assertEqual(parsed["amount"], 120000)
        self.assertEqual(parsed["store"], "COFFEE")
        self.assertFalse(parsed["ambiguous"])

    def test_missing_direction_is_marked_ambiguous(self):
        parsed = bank_import.parse_bank_notification(
            "Kaspi Gold: 5 000 KZT в MAGNUM"
        )
        self.assertTrue(parsed["ambiguous"])

    def test_anonymized_bank_notification_fixtures(self):
        fixture_path = (
            Path(__file__).parent / "fixtures" / "bank_notifications.json"
        )
        fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
        for fixture in fixtures:
            with self.subTest(text=fixture["text"]):
                parsed = bank_import.parse_bank_notification(fixture["text"])
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["amount"], fixture["amount"])
                self.assertEqual(parsed["type"], fixture["type"])
                self.assertEqual(parsed["store"], fixture["store"])


class ImportExportTests(unittest.TestCase):
    def test_csv_round_trip(self):
        source = [{
            "op_date": "2026-08-20",
            "op_time": "12:30",
            "type": "expense",
            "amount": 125050,
            "category_name": "Продукты",
            "payment_name": "Карта",
            "store": "Magnum",
            "description": "Молоко",
        }]
        data = export.export_csv(source).read()
        parsed = export.parse_import_file(data, "operations.csv")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["date"], "2026-08-20")
        self.assertEqual(parsed[0]["amount"], 125050)
        self.assertEqual(parsed[0]["description"], "Молоко")
        self.assertEqual(parsed[0]["type"], "expense")
        self.assertEqual(parsed[0]["category"], "Продукты")
        self.assertEqual(parsed[0]["payment"], "Карта")
        self.assertEqual(parsed[0]["store"], "Magnum")

    def test_transfer_type_survives_csv_round_trip(self):
        source = [{
            "op_date": "2026-09-05",
            "op_time": "",
            "type": "transfer",
            "amount": 1000000,
            "category_name": "Накопления",
            "payment_name": "Карта",
            "store": None,
            "description": "Пополнение цели: Отпуск",
        }]
        data = export.export_csv(source).read()
        parsed = export.parse_import_file(data, "operations.csv")
        self.assertEqual(parsed[0]["type"], "transfer")
        self.assertEqual(parsed[0]["amount"], 1000000)

    def test_own_export_keeps_income_and_expense_apart(self):
        """Своя выгрузка пишет только положительные суммы, направление — в
        колонке «Тип». Раньше импорт смотрел лишь на знак и превращал в расход
        весь файл, включая зарплату."""
        source = [
            {
                "op_date": "2026-08-20", "op_time": "10:00", "type": "income",
                "amount": 5000000, "category_name": "Прочее",
                "payment_name": "Карта", "store": None, "description": "Зарплата",
            },
            {
                "op_date": "2026-08-21", "op_time": "12:00", "type": "expense",
                "amount": 70000, "category_name": "Транспорт",
                "payment_name": "Наличные", "store": None, "description": "Такси",
            },
        ]
        data = export.export_csv(source).read()
        parsed = export.parse_import_file(data, "operations.csv")

        self.assertEqual([r["type"] for r in parsed], ["income", "expense"])
        self.assertEqual([r["amount"] for r in parsed], [5000000, 70000])
        self.assertEqual([r["category"] for r in parsed], ["Прочее", "Транспорт"])

    def test_own_xlsx_export_keeps_type(self):
        source = [{
            "op_date": "2026-08-20", "op_time": "10:00", "type": "income",
            "amount": 100000, "category_name": "Прочее",
            "payment_name": "Карта", "store": None, "description": "Возврат",
        }]
        data = export.export_xlsx(source).read()
        parsed = export.parse_import_file(data, "operations.xlsx")
        self.assertEqual(parsed[0]["type"], "income")
        self.assertEqual(parsed[0]["amount"], 100000)

    def test_semicolon_delimited_bank_csv(self):
        data = (
            "Дата;Сумма;Описание\r\n"
            "20.08.2026;-1 500,50;Такси\r\n"
        ).encode("utf-8")
        parsed = export.parse_import_file(data, "bank.csv")
        self.assertEqual(parsed, [{
            "amount": 150050,
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
        self.assertEqual(parsed[0]["amount"], 70000)

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
        self.assertEqual(parsed[0]["amount"], 10000)

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
        self.assertEqual(parse_positive_amount("12,5"), 1250)
        self.assertEqual(hx("<b>x</b>"), "&lt;b&gt;x&lt;/b&gt;")
    def test_three_months_starts_two_calendar_months_back(self):
        from bot import _month_start

        self.assertEqual(_month_start(date(2026, 8, 20), months_back=2), date(2026, 6, 1))
        self.assertEqual(_month_start(date(2026, 1, 15), months_back=2), date(2025, 11, 1))


class ConfigEnvTests(unittest.TestCase):
    def test_env_strips_inline_comment_and_quotes(self):
        from config import _env

        with patch.dict(os.environ, {"GEMINI_API_KEY": ' "abc123" # comment '}, clear=False):
            self.assertEqual(_env("GEMINI_API_KEY"), "abc123")


class GeminiParseTests(unittest.TestCase):
    def test_comma_price_does_not_drop_other_items(self):
        from gemini_engine import _parse_item_price

        self.assertEqual(_parse_item_price("123,45"), 12345)
        self.assertEqual(_parse_item_price("450 ₸"), 45000)
        self.assertEqual(_parse_item_price(10), 1000)
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
        self.assertEqual(parsed["items"], [{"name": "ok", "price": 1250}])
        self.assertEqual(parsed["total"], 1250)
        self.assertIsNone(parsed["date"])
        self.assertEqual(len(parsed["store"]), 120)

    def test_candidate_text_skips_gemini3_thoughts(self):
        from gemini_engine import _candidate_text, _loads_model_json

        payload = {
            "candidates": [{
                "content": {
                    "parts": [
                        {"thought": True, "text": "сначала подумаю"},
                        {
                            "thoughtSignature": "opaque",
                            "text": '```json\n{"items":[{"name":"хлеб","price":100}]}\n```',
                        },
                    ]
                }
            }]
        }
        text = _candidate_text(payload)
        data = _loads_model_json(text)
        self.assertEqual(data["items"][0]["name"], "хлеб")

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
                {"name": "good", "price": 10000, "category": "Продукты"},
                {"name": "bad", "price": 20000, "category": "Транспорт"},
            ],
            "total": 30000,
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
        parsed = db.update_receipt_draft_item(1, "draft", 0, "Молоко", 45050)
        self.assertEqual(parsed["items"][0]["name"], "Молоко")
        self.assertEqual(parsed["items"][0]["price"], 45050)

        parsed = db.delete_receipt_draft_item(1, "draft", 1)
        self.assertEqual(len(parsed["items"]), 1)
        with self.assertRaises(ValueError):
            db.delete_receipt_draft_item(1, "draft", 0)
        self.assertEqual(len(db.get_receipt_draft(1, "draft")["items"]), 1)
        self.assertIsNone(db.get_receipt_draft(2, "draft"))

    def test_receipt_total_mismatch_requires_choice_and_resolves_exactly(self):
        draft = self._draft()
        draft["total"] = 25000
        db.save_state("receipt_draft", "items", draft, user_id=1)
        with self.assertRaises(ValueError):
            db.save_receipt_draft(1, "items", "2026-08-20")
        self.assertEqual(self._counts(), (0, 0))
        self.assertIsNotNone(db.get_receipt_draft(1, "items"))

        parsed = db.resolve_receipt_draft_total(1, "items", use_receipt_total=False)
        self.assertEqual(parsed["total"], 30000)
        receipt_id, _ = db.save_receipt_draft(1, "items", "2026-08-20")
        self.assertIsInstance(receipt_id, int)

        draft = self._draft()
        draft["total"] = 25000
        db.save_state("receipt_draft", "receipt", draft, user_id=1)
        parsed = db.resolve_receipt_draft_total(1, "receipt", use_receipt_total=True)
        self.assertEqual(sum(item["price"] for item in parsed["items"]), 25000)
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
        self.assertEqual(stored_total, 25000)

    def test_receipt_preview_blocks_save_until_total_is_resolved(self):
        draft = self._draft()
        draft["total"] = 25000
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
        self.assertIn("recv_paypick:draft", callbacks)
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
        db.set_budget(1, cat_id, 100000)
        db.add_recurring(1, "expense", 1000, cat_id, None, "rent", 1)
        db.learn_category(1, "молоко тест", cat_id)
        db.add_transaction(1, "expense", 500, cat_id, None, None, "milk", date.today().isoformat())
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
        rec_id = db.add_recurring(1, "expense", 9900, cat_id, None, "sub", 1)
        today = date.today().replace(day=28).isoformat() if date.today().day >= 1 else date.today().isoformat()
        self.assertTrue(db.apply_due_recurring(rec_id, today))
        self.assertFalse(db.apply_due_recurring(rec_id, today))
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM transactions WHERE description='sub'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_foreign_category_is_not_attached(self):
        other_cat = db.get_category_id_by_name(2, "Продукты")
        fallback = db.get_category_id_by_name(1, "Прочее")
        db.add_transaction(1, "expense", 1000, other_cat, None, None, "x", "2026-08-20")
        with db.get_conn() as conn:
            stored = conn.execute("SELECT category_id FROM transactions WHERE user_id=1").fetchone()[0]
        self.assertEqual(stored, fallback)
        self.assertFalse(db.update_transaction_category(1, 1, other_cat))

    def test_transaction_date_type_and_store_updates_are_owner_scoped(self):
        cat_id = db.get_category_id_by_name(1, "Продукты")
        tx_id = db.add_transaction(
            1, "expense", 50000, cat_id, None, "Старый", "покупка", "2026-08-20"
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
            1, "expense", 50000, cat_id, None, "Магазин", "покупка", "2026-09-04"
        )
        text, keyboard = bot._tx_detail_view(1, tx_id)
        self.assertIn("Тип:", text)
        self.assertIn("Расход", text)
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
        draft["items"] = [{"name": "only", "price": 1000, "category": "Продукты"}]
        draft["total"] = 1000
        db.save_state("receipt_draft", "one", draft, user_id=1)
        receipt_id, _ = db.save_receipt_draft(1, "one", "2026-08-20")
        with db.get_conn() as conn:
            tx_id = conn.execute("SELECT id FROM transactions WHERE receipt_id=?", (receipt_id,)).fetchone()[0]
        self.assertTrue(db.delete_transaction(1, tx_id))
        self.assertEqual(self._counts(), (0, 0))

    def test_import_draft_commit_is_atomic_and_idempotent(self):
        rows = [
            {"amount": 1000, "type": "expense", "date": "2026-08-20", "description": "A", "category": "Продукты"},
            {"amount": 2000, "type": "income", "date": "2026-08-20", "description": "B", "category": "Прочее"},
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
            "amount": 500000, "type": "income", "date": "2026-08-20",
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
            "amount": 10000, "type": "expense", "date": "2026-08-20",
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
            {"amount": 100, "type": "expense", "date": "2026-08-20", "description": "ok", "category": "Продукты"},
            {"amount": 100, "type": "expense", "date": "2026-08-20", "description": "bad", "category": "Продукты"},
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
        db.add_transaction(1, "income", 5000, cat_id, None, "Shop", "salary", "2026-08-20", "10:00")
        db.add_recurring(1, "expense", 900, cat_id, None, "net", 5)
        db.set_budget(1, cat_id, 10000)
        db.create_goal(1, "Отпуск", 100000)
        db.learn_category(1, "кофе", cat_id)
        data = db.get_all_user_rows(1)
        self.assertEqual(data["version"], 5)
        self.assertEqual(data["amount_unit"], "tiyn")
        self.assertEqual(data["transactions"][0]["amount"], 5000)
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
        rec_id = db.add_recurring(1, "expense", 50000, cat_id, None, "rent", 1)
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
        db.add_transaction(1, "expense", 1000, cat_id, None, None, "keep me", "2026-08-20")
        with self.assertRaises(ValueError):
            db.restore_user_backup(1, ["not", "a", "dict"])
        with db.get_conn() as conn:
            desc = conn.execute("SELECT description FROM transactions WHERE user_id=1").fetchone()[0]
        self.assertEqual(desc, "keep me")

    def test_restore_failure_rolls_back_wipe(self):
        """Если восстановление падает на середине (битые данные внутри
        валидного dict), старые данные пользователя не должны стираться."""
        cat_id = db.get_category_id_by_name(1, "Продукты")
        db.add_transaction(1, "expense", 1000, cat_id, None, None, "keep me", "2026-08-20")
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

    def test_backup_v3_tenge_amounts_convert_to_tiyn(self):
        payload = {
            "version": 3,
            "settings": {"language": "ru", "digest_frequency": "off", "timezone": "UTC"},
            "categories": [{"name": "Продукты", "emoji": "🛒"}],
            "payment_methods": [{"name": "Наличные"}],
            "receipts": [],
            "transactions": [{
                "type": "expense",
                "amount": 100.40,
                "category": "Продукты",
                "op_date": "2026-08-20",
                "description": "old-tenge",
            }],
            "recurring": [],
            "budgets": [{"category": "Продукты", "monthly_limit": 50}],
            "goals": [{"name": "Банка", "target_amount": 10, "current_amount": 0}],
            "learned": [],
        }
        db.restore_user_backup(1, payload)
        with db.get_conn() as conn:
            amount = conn.execute(
                "SELECT amount FROM transactions WHERE user_id=1 AND description='old-tenge'"
            ).fetchone()[0]
            limit = conn.execute(
                "SELECT monthly_limit FROM category_budgets WHERE user_id=1"
            ).fetchone()[0]
            target = conn.execute(
                "SELECT target_amount FROM savings_goals WHERE user_id=1"
            ).fetchone()[0]
            tz = conn.execute("SELECT timezone FROM users WHERE user_id=1").fetchone()[0]
        self.assertEqual(amount, 10040)
        self.assertEqual(limit, 5000)
        self.assertEqual(target, 1000)
        self.assertEqual(tz, "UTC")

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
        db.set_budget(1, cat_id, 10000)
        db.add_transaction(1, "expense", 15000, cat_id, None, None, "over", date.today().isoformat())

        warning = await bot._budget_warning_text(1, cat_id)
        self.assertIsNotNone(warning)
        self._assert_escaped(warning)

    def test_budget_view_escapes_category_name(self):
        cat_id = db.add_category(1, self.PAYLOAD)
        db.set_budget(1, cat_id, 100000)
        text, _ = bot._budget_view(1)
        self._assert_escaped(text)

    def test_tx_detail_view_escapes_all_free_text_fields(self):
        cat_id = db.add_category(1, self.PAYLOAD)
        pm_id = db.add_payment_method(1, self.PAYLOAD)
        tx_id = db.add_transaction(
            1, "expense", 50000, cat_id, pm_id, self.PAYLOAD, self.PAYLOAD,
            date.today().isoformat(),
        )
        text, _ = bot._tx_detail_view(1, tx_id)
        # Магазин, описание, категория и способ оплаты - четыре разных поля,
        # каждое должно быть экранировано независимо от других.
        self.assertEqual(text.count(self.ESCAPED), 4)
        self.assertNotIn(self.PAYLOAD, text)

    def test_goals_view_escapes_goal_name(self):
        db.create_goal(1, self.PAYLOAD, 100000)
        text, _ = bot._goals_view(1)
        self._assert_escaped(text)

    def test_goal_detail_view_escapes_goal_name(self):
        goal_id = db.create_goal(1, self.PAYLOAD, 100000)
        text, _ = bot._goal_detail_view(1, goal_id)
        self._assert_escaped(text)

    def test_recurring_view_escapes_description(self):
        cat_id = db.get_category_id_by_name(1, "Прочее")
        db.add_recurring(1, "expense", 10000, cat_id, None, self.PAYLOAD, 5)
        text, _ = bot._recurring_view(1)
        self._assert_escaped(text)

    def test_recurring_detail_view_escapes_description(self):
        cat_id = db.get_category_id_by_name(1, "Прочее")
        rec_id = db.add_recurring(1, "expense", 10000, cat_id, None, self.PAYLOAD, 5)
        text, _ = bot._recurring_detail_view(1, rec_id)
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
        today = db.user_today(1).isoformat()
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

    async def test_create_requires_init_data(self):
        response = await self.client.post(
            "/api/transactions",
            json={"type": "expense", "amount": "500", "category_id": 1},
        )
        self.assertEqual(response.status, 401)

    async def test_create_rejects_foreign_category(self):
        foreign = db.get_category_id_by_name(2, "Продукты")
        response = await self.client.post(
            "/api/transactions",
            json={
                "type": "expense",
                "amount": "500",
                "category_id": foreign,
                "description": "такси",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status, 403)

    async def test_create_valid_transaction(self):
        category_id = db.get_category_id_by_name(1, "Транспорт")
        today = db.user_today(1).isoformat()
        response = await self.client.post(
            "/api/transactions",
            json={
                "type": "expense",
                "amount": "500",
                "category_id": category_id,
                "date": today,
                "description": "такси",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status, 201)
        payload = await response.json()
        self.assertEqual(payload["description"], "такси")
        self.assertEqual(payload["amount"], 50000)
        self.assertEqual(payload["type"], "expense")
        stored = db.get_transaction_by_id(1, payload["id"])
        self.assertEqual(stored["amount"], 50000)

    async def test_patch_foreign_transaction_is_404(self):
        with db.get_conn() as conn:
            other_id = conn.execute(
                "SELECT id FROM transactions WHERE user_id=2 LIMIT 1"
            ).fetchone()[0]
        response = await self.client.patch(
            f"/api/transactions/{other_id}",
            json={"amount": "10"},
            headers=self.headers,
        )
        self.assertEqual(response.status, 404)

    async def test_patch_transfer_is_read_only(self):
        goal_id = db.create_goal(1, "Отпуск", 1000000)
        tx_id = db.contribute_to_goal(1, goal_id, 10000)
        response = await self.client.patch(
            f"/api/transactions/{tx_id}",
            json={"amount": "50"},
            headers=self.headers,
        )
        self.assertEqual(response.status, 409)


class MoneyAndTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tempdir.name) / "money.db")
        db.init_db()
        db.ensure_user(1, "owner")

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def test_tenge_to_tiyn_rounding_and_no_binary_float_error(self):
        from formatting import money
        from money import tenge_to_tiyn

        self.assertEqual(tenge_to_tiyn("0.01"), 1)
        self.assertEqual(tenge_to_tiyn("100.40"), 10040)
        self.assertEqual(tenge_to_tiyn("1.005"), 101)
        self.assertEqual(tenge_to_tiyn(0.1) + tenge_to_tiyn(0.2), tenge_to_tiyn("0.3"))
        self.assertEqual(tenge_to_tiyn("9999999.99"), 999999999)
        self.assertEqual(money(1), "0,01 ₸")
        self.assertEqual(money(10040), "100,40 ₸")
        self.assertEqual(money(10000), "100 ₸")
        self.assertEqual(money(125050), "1 250,50 ₸")
        from i18n import bot_commands, format_date, format_money, translation_gaps

        self.assertEqual(translation_gaps(), {})
        self.assertEqual(format_money(125050, "ru"), "1 250,50 ₸")
        self.assertEqual(format_money(125050, "en"), "1,250.50 ₸")
        self.assertEqual(format_date(date(2026, 9, 5), "ru"), "05.09.2026")
        self.assertEqual(format_date(date(2026, 9, 5), "en"), "5 Sep 2026")
        commands = {cmd.command for cmd in bot_commands("en")}
        self.assertIn("privacy", commands)
        self.assertIn("feedback", commands)
        self.assertTrue(all(cmd.description for cmd in bot_commands("kk")))

    def test_almaty_day_boundary_when_utc_is_previous_evening(self):
        from timeutil import today_in_tz

        frozen = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)
        self.assertEqual(today_in_tz("UTC", frozen), date(2026, 9, 4))
        self.assertEqual(today_in_tz("Asia/Almaty", frozen), date(2026, 9, 5))
        self.assertEqual(
            db.user_today(1, now=frozen).isoformat(),
            "2026-09-05",
        )
        db.set_user_timezone(1, "UTC")
        self.assertEqual(db.user_today(1, now=frozen).isoformat(), "2026-09-04")

    def test_recurring_on_first_of_month_uses_user_timezone(self):
        cat_id = db.get_category_id_by_name(1, "Продукты")
        rec_id = db.add_recurring(1, "expense", 15000, cat_id, None, "rent-1", 1)
        # 31 августа 21:00 UTC = 1 сентября 02:00 в Алматы.
        frozen = datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
        today = db.user_today(1, now=frozen)
        self.assertEqual(today.isoformat(), "2026-09-01")
        self.assertTrue(db.apply_due_recurring(rec_id, today.isoformat()))
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT amount, op_date FROM transactions WHERE description='rent-1'"
            ).fetchone()
        self.assertEqual(row["amount"], 15000)
        self.assertEqual(row["op_date"], "2026-09-01")

    def test_period_bounds_use_supplied_today(self):
        from bot import _period_bounds

        date_from, date_to, label = _period_bounds("day", date(2026, 9, 5))
        self.assertEqual((date_from, date_to, label), ("2026-09-05", "2026-09-05", "за сегодня"))
        date_from, date_to, _ = _period_bounds("month", date(2026, 9, 5))
        self.assertEqual((date_from, date_to), ("2026-09-01", "2026-09-05"))

    def test_new_database_stores_integer_tiyn_without_scaling_twice(self):
        db.add_transaction(1, "expense", 10040, db.get_category_id_by_name(1, "Продукты"), None, None, "x", "2026-09-05")
        db.init_db()
        with db.get_conn() as conn:
            amount_type = conn.execute("PRAGMA table_info(transactions)").fetchall()
            types = {row["name"]: row["type"].upper() for row in amount_type}
            stored = conn.execute("SELECT amount FROM transactions").fetchone()[0]
            marker = conn.execute(
                "SELECT 1 FROM app_meta WHERE key=?", (db.MONEY_TIYN_KEY,)
            ).fetchone()
        self.assertEqual(types["amount"], "INTEGER")
        self.assertEqual(stored, 10040)
        self.assertIsNotNone(marker)

    def test_real_tenge_rows_are_migrated_to_tiyn_with_matching_totals(self):
        live_path = Path(self.tempdir.name) / "legacy.db"
        db.DB_PATH = str(live_path)
        conn = __import__("sqlite3").connect(live_path)
        conn.executescript(
            """
            CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT NOT NULL);
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL
            );
            CREATE TABLE payment_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL
            );
            CREATE TABLE receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, store TEXT,
                receipt_date TEXT, receipt_time TEXT, payment_method_id INTEGER, raw_text TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                type TEXT NOT NULL, amount REAL NOT NULL, category_id INTEGER,
                payment_method_id INTEGER, store TEXT, description TEXT, receipt_id INTEGER,
                op_date TEXT NOT NULL, op_time TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE recurring_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                type TEXT NOT NULL, amount REAL NOT NULL, category_id INTEGER,
                payment_method_id INTEGER, description TEXT, day_of_month INTEGER NOT NULL,
                last_run_date TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE category_budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL, monthly_limit REAL NOT NULL,
                UNIQUE(user_id, category_id)
            );
            CREATE TABLE savings_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                name TEXT NOT NULL, target_amount REAL NOT NULL,
                current_amount REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE app_state (
                scope TEXT NOT NULL, key TEXT NOT NULL, user_id INTEGER,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (scope, key)
            );
            CREATE TABLE recurring_runs (
                recurring_id INTEGER NOT NULL, period TEXT NOT NULL,
                PRIMARY KEY (recurring_id, period)
            );
            CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO users(user_id, username, created_at) VALUES (1, 'legacy', '2026-01-01T00:00:00+00:00');
            INSERT INTO categories(user_id, name) VALUES (1, 'Продукты');
            INSERT INTO transactions(user_id, type, amount, category_id, description, op_date, created_at)
                VALUES (1, 'expense', 100.40, 1, 'a', '2026-09-01', '2026-09-01T00:00:00+00:00');
            INSERT INTO transactions(user_id, type, amount, category_id, description, op_date, created_at)
                VALUES (1, 'expense', 0.01, 1, 'b', '2026-09-01', '2026-09-01T00:00:00+00:00');
            INSERT INTO transactions(user_id, type, amount, category_id, description, op_date, created_at)
                VALUES (1, 'income', 50, 1, 'c', '2026-09-01', '2026-09-01T00:00:00+00:00');
            INSERT INTO transactions(user_id, type, amount, category_id, description, op_date, created_at)
                VALUES (1, 'expense', 1.005, 1, 'half-up', '2026-09-01', '2026-09-01T00:00:00+00:00');
            INSERT INTO app_state(scope, key, user_id, payload, updated_at)
                VALUES ('receipt_draft', 'old', 1, '{"total": 100}', '2026-09-01T00:00:00+00:00');
            """
        )
        before_n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        before_sum = conn.execute("SELECT SUM(amount) FROM transactions").fetchone()[0]
        conn.close()

        db.init_db()
        with db.get_conn() as conn:
            after_n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            after_sum = conn.execute("SELECT SUM(amount) FROM transactions").fetchone()[0]
            amount_type = [
                row["type"].upper()
                for row in conn.execute("PRAGMA table_info(transactions)")
                if row["name"] == "amount"
            ][0]
            amounts = [
                row[0]
                for row in conn.execute(
                    "SELECT amount FROM transactions ORDER BY id"
                )
            ]
            leftover_drafts = conn.execute(
                "SELECT COUNT(*) FROM app_state WHERE scope='receipt_draft'"
            ).fetchone()[0]
        self.assertEqual(after_n, before_n)
        self.assertEqual(amount_type, "INTEGER")
        self.assertEqual(amounts, [10040, 1, 5000, 101])
        self.assertEqual(after_sum, 15142)
        self.assertEqual(leftover_drafts, 0)


class ReceiptSafetyTests(unittest.TestCase):
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
                {"name": "good", "price": 10000, "category": "Продукты"},
                {"name": "bad", "price": 20000, "category": "Транспорт"},
            ],
            "total": 30000,
            "raw_text": "raw",
            "source": "test",
        }

    def test_receipt_preview_includes_payment_picker(self):
        text, keyboard = bot._receipt_draft_view(self._draft(), "draft")
        self.assertIn("Оплата:", text)
        callbacks = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("recv_paypick:draft", callbacks)
        self.assertIn("recv_save:draft", callbacks)

    def test_receipt_draft_payment_is_used_on_save(self):
        card_id = db.get_payment_method_id_by_name(1, "Карта (основная)")
        draft = self._draft()
        draft["payment_method_id"] = card_id
        db.save_state("receipt_draft", "pay", draft, user_id=1)
        parsed = db.update_receipt_draft_payment(1, "pay", card_id)
        self.assertEqual(parsed["payment_method_id"], card_id)
        self.assertEqual(parsed["payment_name"], "Карта (основная)")
        self.assertIsNone(
            db.update_receipt_draft_payment(
                1, "pay", db.get_payment_method_id_by_name(2, "Карта (основная)")
            )
        )
        db.save_receipt_draft(1, "pay", "2026-08-20")
        with db.get_conn() as conn:
            receipt_pm = conn.execute("SELECT payment_method_id FROM receipts").fetchone()[0]
            tx_pms = {
                row[0]
                for row in conn.execute("SELECT payment_method_id FROM transactions")
            }
        self.assertEqual(receipt_pm, card_id)
        self.assertEqual(tx_pms, {card_id})

    def test_gemini_quota_eleventh_is_denied(self):
        for expected in range(1, 11):
            ok, used = db.try_consume_gemini_quota(1, "2026-09-05")
            self.assertTrue(ok)
            self.assertEqual(used, expected)
        ok, used = db.try_consume_gemini_quota(1, "2026-09-05")
        self.assertFalse(ok)
        self.assertEqual(used, 10)
        self.assertEqual(db.get_gemini_quota_used(1, "2026-09-05"), 10)

    def test_gemini_quota_is_race_safe(self):
        results = []

        def consume():
            results.append(db.try_consume_gemini_quota(1, "2026-09-05", limit=1))

        threads = [threading.Thread(target=consume) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for ok, _ in results if ok), 1)
        self.assertEqual(db.get_gemini_quota_used(1, "2026-09-05"), 1)

    def test_manual_bank_and_import_do_not_consume_gemini_quota(self):
        from fingerprints import bank_fingerprint

        cat = db.get_category_id_by_name(1, "Продукты")
        db.add_transaction(1, "expense", 50000, cat, None, None, "taxi", "2026-09-05")

        fp = bank_fingerprint(30000, "Test Store", "2026-08-20", "Kaspi Gold: оплата 300 KZT")
        draft = self._draft()
        draft["source"] = "bank_notification"
        draft["fingerprints"] = [fp]
        db.save_state("receipt_draft", "bankq", draft, user_id=1)
        self.assertIsNotNone(db.save_receipt_draft(1, "bankq", "2026-08-20"))
        self.assertEqual(db.find_fingerprint(1, "bank", [fp]), "2026-08-20")

        rows = [{
            "amount": 1000,
            "type": "expense",
            "date": "2026-08-20",
            "description": "A",
            "category": "Продукты",
        }]
        db.save_state("import_draft", "1_q", rows, user_id=1)
        self.assertEqual(
            db.commit_import_draft(1, "1_q", db.get_default_payment_method_id(1), "2026-08-20"),
            1,
        )
        self.assertEqual(db.get_gemini_quota_used(1, "2026-09-05"), 0)
        self.assertEqual(db.get_gemini_quota_used(1, "2026-08-20"), 0)

    def test_duplicate_photo_fingerprint_warns_but_save_is_allowed(self):
        from fingerprints import photo_telegram_fingerprint

        fp = photo_telegram_fingerprint("uniq1")
        self.assertIsNone(db.find_fingerprint(1, "photo", [fp]))
        draft = self._draft()
        draft["fingerprints"] = [fp]
        db.save_state("receipt_draft", "p1", draft, user_id=1)
        self.assertIsNotNone(db.save_receipt_draft(1, "p1", "2026-08-20"))
        self.assertEqual(db.find_fingerprint(1, "photo", [fp]), "2026-08-20")

        draft2 = self._draft()
        draft2["fingerprints"] = [fp]
        db.save_state("receipt_draft", "p2", draft2, user_id=1)
        self.assertIsNotNone(db.save_receipt_draft(1, "p2", "2026-08-20"))
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        self.assertEqual(n, 2)

    def test_import_duplicate_count_does_not_block_second_import(self):
        rows = [{
            "amount": 1000,
            "type": "expense",
            "date": "2026-08-20",
            "description": "A",
            "category": "Продукты",
        }]
        self.assertEqual(db.count_import_duplicates(1, rows, "2026-08-20"), 0)
        db.save_state("import_draft", "1_d1", rows, user_id=1)
        self.assertEqual(
            db.commit_import_draft(1, "1_d1", db.get_default_payment_method_id(1), "2026-08-20"),
            1,
        )
        self.assertEqual(db.count_import_duplicates(1, rows, "2026-08-20"), 1)
        db.save_state("import_draft", "1_d2", rows, user_id=1)
        self.assertEqual(
            db.commit_import_draft(1, "1_d2", db.get_default_payment_method_id(1), "2026-08-20"),
            1,
        )
        with db.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        self.assertEqual(n, 2)

    def test_error_messages_are_distinct(self):
        rate = bot._receipt_error_text("rate_limit")
        empty = bot._receipt_error_text("empty")
        network = bot._receipt_error_text("network")
        bad_json = bot._receipt_error_text("bad_json")
        quota = bot._receipt_error_text("quota")
        self.assertIn("лимит запросов", rate)
        self.assertIn("товаров", empty)
        self.assertNotEqual(rate, empty)
        self.assertNotEqual(network, bad_json)
        self.assertIn("лимит распознавания", quota)


class ReceiptImageAndGeminiTests(unittest.TestCase):
    def test_prepare_rejects_bad_magic(self):
        from image_prep import ReceiptImageError, prepare_receipt_image

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
            fh.write(b"GIF89a" + b"\x00" * 40)
            path = fh.name
        try:
            with self.assertRaises(ReceiptImageError) as ctx:
                prepare_receipt_image(path)
            self.assertEqual(ctx.exception.code, "unsupported")
        finally:
            os.remove(path)

    def test_prepare_rejects_too_large(self):
        from image_prep import MAX_RECEIPT_BYTES, ReceiptImageError, prepare_receipt_image

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
            fh.write(b"\xff\xd8" + b"\x00" * 40)
            path = fh.name
        try:
            with patch("image_prep.os.path.getsize", return_value=MAX_RECEIPT_BYTES + 1):
                with self.assertRaises(ReceiptImageError) as ctx:
                    prepare_receipt_image(path)
            self.assertEqual(ctx.exception.code, "too_large")
        finally:
            os.remove(path)

    def test_prepare_resizes_long_side(self):
        from PIL import Image

        from image_prep import MAX_SIDE_PX, prepare_receipt_image

        src = Path(tempfile.mkdtemp()) / "wide.png"
        Image.new("RGB", (2000, 80), "white").save(src)
        dest = prepare_receipt_image(str(src))
        try:
            with Image.open(dest) as out:
                self.assertLessEqual(max(out.size), MAX_SIDE_PX)
                self.assertEqual(out.format, "JPEG")
                self.assertFalse(bool(out.getexif()))
        finally:
            os.remove(src)
            if os.path.exists(dest):
                os.remove(dest)
            os.rmdir(src.parent)

    def test_extract_receipt_maps_rate_limit_and_empty(self):
        from receipt_pipeline import extract_receipt

        with patch(
            "receipt_pipeline.get_receipt_from_gemini",
            return_value=(None, "rate_limit"),
        ):
            parsed, err = extract_receipt("unused.jpg", 1)
        self.assertEqual(err, "rate_limit")
        self.assertEqual(parsed["items"], [])

        with patch(
            "receipt_pipeline.get_receipt_from_gemini",
            return_value=(None, "empty"),
        ):
            parsed, err = extract_receipt("unused.jpg", 1)
        self.assertEqual(err, "empty")
        self.assertEqual(parsed["items"], [])

    def test_post_gemini_distinguishes_429_and_5xx(self):
        import gemini_engine

        class Resp:
            def __init__(self, status_code):
                self.status_code = status_code
                self.ok = False
                self.text = "err"

        with patch("gemini_engine.time.sleep"):
            with patch("gemini_engine.requests.post", return_value=Resp(429)):
                payload, err = gemini_engine._post_gemini({})
        self.assertIsNone(payload)
        self.assertEqual(err, "rate_limit")

        with patch("gemini_engine.time.sleep"):
            with patch("gemini_engine.requests.post", return_value=Resp(503)):
                payload, err = gemini_engine._post_gemini({})
        self.assertIsNone(payload)
        self.assertEqual(err, "network")


class GoalsBudgetsRecurringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tempdir.name) / "goals.db")
        db.init_db()
        db.ensure_user(1, "owner")
        db.ensure_user(2, "other")

    async def asyncTearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def test_contribute_creates_transfer_not_expense(self):
        cat_id = db.get_category_id_by_name(1, "Продукты")
        db.add_transaction(1, "expense", 50000, cat_id, None, None, "taxi", "2026-09-05")
        goal_id = db.create_goal(1, "Отпуск", 300000, deadline="2026-12-01")
        tx_id = db.contribute_to_goal(1, goal_id, 100000, op_date="2026-09-05")
        self.assertIsNotNone(tx_id)

        goal = db.get_goal_by_id(1, goal_id)
        self.assertEqual(goal["current_amount"], 100000)
        self.assertEqual(goal["deadline"], "2026-12-01")

        tx = db.get_transaction_by_id(1, tx_id)
        self.assertEqual(tx["type"], "transfer")
        self.assertEqual(tx["amount"], 100000)
        self.assertEqual(tx["goal_id"], goal_id)
        self.assertEqual(tx["goal_delta"], 100000)
        self.assertEqual(db.get_total_expense(1, "2026-09-01", "2026-09-30"), 50000)
        self.assertEqual(db.get_goal_transfers_total(1, "2026-09-01", "2026-09-30"), 100000)
        self.assertEqual(db.get_goals_reserved(1), 100000)
        self.assertEqual(db.get_category_name(1, tx["category_id"]), "Накопления")

    def test_withdraw_is_atomic_and_cannot_go_negative(self):
        goal_id = db.create_goal(1, "Подушка", 500000)
        db.contribute_to_goal(1, goal_id, 100000, op_date="2026-09-05")
        tx_id, reason = db.withdraw_from_goal(1, goal_id, 40000, op_date="2026-09-05")
        self.assertEqual(reason, "ok")
        self.assertIsNotNone(tx_id)
        self.assertEqual(db.get_goal_by_id(1, goal_id)["current_amount"], 60000)

        tx_id, reason = db.withdraw_from_goal(1, goal_id, 70000, op_date="2026-09-05")
        self.assertIsNone(tx_id)
        self.assertEqual(reason, "insufficient")
        self.assertEqual(db.get_goal_by_id(1, goal_id)["current_amount"], 60000)
        self.assertEqual(db.get_goal_transfers_total(1, "2026-09-01", "2026-09-30"), 60000)

        tx_id, reason = db.withdraw_from_goal(2, goal_id, 1000, op_date="2026-09-05")
        self.assertEqual(reason, "not_found")

    def test_goal_name_target_deadline_can_be_edited(self):
        goal_id = db.create_goal(1, "Старое", 100000)
        self.assertTrue(db.update_goal(1, goal_id, name="Новое", target_amount=250000, deadline="2027-01-15"))
        goal = db.get_goal_by_id(1, goal_id)
        self.assertEqual(goal["name"], "Новое")
        self.assertEqual(goal["target_amount"], 250000)
        self.assertEqual(goal["deadline"], "2027-01-15")
        self.assertTrue(db.update_goal(1, goal_id, clear_deadline=True))
        self.assertIsNone(db.get_goal_by_id(1, goal_id)["deadline"])
        self.assertFalse(db.update_goal(2, goal_id, name="Чужое"))
        self.assertEqual(db.get_goal_by_id(1, goal_id)["name"], "Новое")

    def test_delete_goal_returns_remaining_as_transfer(self):
        goal_id = db.create_goal(1, "Велосипед", 200000)
        db.contribute_to_goal(1, goal_id, 80000, op_date="2026-09-05")
        self.assertTrue(db.delete_goal(1, goal_id))
        self.assertIsNone(db.get_goal_by_id(1, goal_id))
        self.assertEqual(db.get_goals_reserved(1), 0)
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT type, goal_delta, goal_id FROM transactions WHERE user_id=1 ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["type"], "transfer")
        self.assertEqual(rows[0]["goal_delta"], 80000)
        self.assertIsNone(rows[0]["goal_id"])
        self.assertEqual(rows[1]["goal_delta"], -80000)
        self.assertEqual(db.get_total_expense(1, "2026-01-01", "2026-12-31"), 0)

    def test_transfer_amount_and_type_are_locked(self):
        goal_id = db.create_goal(1, "Отпуск", 100000)
        tx_id = db.contribute_to_goal(1, goal_id, 10000, op_date="2026-09-05")
        self.assertFalse(db.update_transaction_amount(1, tx_id, 5000))
        self.assertFalse(db.update_transaction_type(1, tx_id, "expense"))
        other_cat = db.get_category_id_by_name(1, "Продукты")
        self.assertFalse(db.update_transaction_category(1, tx_id, other_cat))
        tx = db.get_transaction_by_id(1, tx_id)
        self.assertEqual(tx["type"], "transfer")
        self.assertEqual(tx["amount"], 10000)
        text, keyboard = bot._tx_detail_view(1, tx_id)
        callbacks = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertNotIn(f"tx_amount:{tx_id}", callbacks)
        self.assertNotIn(f"tx_type:{tx_id}", callbacks)
        self.assertIn("Перевод", text)

    def test_update_recurring_keeps_runs_and_stays_idempotent(self):
        cat_id = db.get_category_id_by_name(1, "Коммунальные и связь")
        pay_id = db.get_default_payment_method_id(1)
        rec_id = db.add_recurring(1, "expense", 8000000, cat_id, pay_id, "Аренда", 1)
        today = "2026-09-05"
        self.assertTrue(db.apply_due_recurring(rec_id, today))
        food_id = db.get_category_id_by_name(1, "Продукты")
        self.assertTrue(
            db.update_recurring(
                1, rec_id, amount=9000000, day_of_month=5,
                category_id=food_id, description="Аренда квартиры",
            )
        )
        row = db.get_recurring_by_id(1, rec_id)
        self.assertEqual(row["amount"], 9000000)
        self.assertEqual(row["day_of_month"], 5)
        self.assertEqual(row["category_id"], food_id)
        self.assertEqual(row["description"], "Аренда квартиры")
        with db.get_conn() as conn:
            runs = conn.execute(
                "SELECT period FROM recurring_runs WHERE recurring_id=?", (rec_id,)
            ).fetchall()
        self.assertEqual([r[0] for r in runs], ["2026-09"])
        self.assertFalse(db.apply_due_recurring(rec_id, today))
        with db.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE description='Аренда'"
            ).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertFalse(db.update_recurring(2, rec_id, amount=1000))
        self.assertFalse(db.update_recurring(1, rec_id, day_of_month=31))

    def test_overall_budget_and_combined_warning(self):
        food = db.get_category_id_by_name(1, "Продукты")
        today = db.user_today(1).isoformat()
        db.set_budget(1, food, 100000)
        db.set_budget(1, None, 150000)
        db.add_transaction(1, "expense", 90000, food, None, None, "milk", today)
        db.add_transaction(
            1, "expense", 40000, db.get_category_id_by_name(1, "Транспорт"),
            None, None, "taxi", today,
        )
        warning = bot._budget_warnings_text(1, [food])
        self.assertIsNotNone(warning)
        self.assertIn("Продукты", warning)
        self.assertIn("Общий бюджет", warning)
        self.assertEqual(warning.count("\n") + 1, 2)

        text, keyboard = bot._budget_view(1)
        self.assertIn("Все расходы", text)
        callbacks = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("bud_overall", callbacks)

        overall = db.get_overall_budget(1)
        self.assertEqual(overall["monthly_limit"], 150000)
        db.set_budget(1, None, 200000)
        self.assertEqual(db.get_overall_budget(1)["monthly_limit"], 200000)
        with db.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM category_budgets WHERE user_id=1 AND category_id IS NULL"
            ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_backup_round_trip_keeps_goal_transfer_and_overall_budget(self):
        food = db.get_category_id_by_name(1, "Продукты")
        goal_id = db.create_goal(1, "Отпуск", 500000, deadline="2026-12-31")
        db.contribute_to_goal(1, goal_id, 120000, op_date="2026-09-05")
        db.set_budget(1, None, 400000)
        db.set_budget(1, food, 100000)
        data = db.get_all_user_rows(1)
        self.assertEqual(data["version"], 5)
        self.assertEqual(data["goals"][0]["deadline"], "2026-12-31")
        self.assertIsNotNone(data["goals"][0]["backup_id"])
        transfer = next(tx for tx in data["transactions"] if tx["type"] == "transfer")
        self.assertEqual(transfer["goal_backup_id"], data["goals"][0]["backup_id"])
        self.assertEqual(transfer["goal_delta"], 120000)
        self.assertTrue(any(b["category"] is None for b in data["budgets"]))

        db.restore_user_backup(1, data)
        restored_goal = db.get_goals(1)[0]
        self.assertEqual(restored_goal["current_amount"], 120000)
        self.assertEqual(restored_goal["deadline"], "2026-12-31")
        txs = db.get_transactions(1, "2026-09-01", "2026-09-30")
        transfer_row = next(r for r in txs if r["type"] == "transfer")
        self.assertEqual(transfer_row["goal_id"], restored_goal["id"])
        self.assertEqual(db.get_overall_budget(1)["monthly_limit"], 400000)
        self.assertEqual(db.get_total_expense(1, "2026-09-01", "2026-09-30"), 0)

    def test_legacy_schema_gains_transfer_and_nullable_budget(self):
        live_path = Path(self.tempdir.name) / "legacy_goals.db"
        db.DB_PATH = str(live_path)
        conn = __import__("sqlite3").connect(live_path)
        conn.executescript(
            """
            CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT NOT NULL);
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL
            );
            CREATE TABLE payment_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL
            );
            CREATE TABLE receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, store TEXT,
                receipt_date TEXT, receipt_time TEXT, payment_method_id INTEGER, raw_text TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('expense', 'income')), amount INTEGER NOT NULL,
                category_id INTEGER, payment_method_id INTEGER, store TEXT, description TEXT,
                receipt_id INTEGER, op_date TEXT NOT NULL, op_time TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE recurring_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                type TEXT NOT NULL, amount INTEGER NOT NULL, category_id INTEGER,
                payment_method_id INTEGER, description TEXT, day_of_month INTEGER NOT NULL,
                last_run_date TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE category_budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL, monthly_limit INTEGER NOT NULL,
                UNIQUE(user_id, category_id)
            );
            CREATE TABLE savings_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                name TEXT NOT NULL, target_amount INTEGER NOT NULL,
                current_amount INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE app_state (
                scope TEXT NOT NULL, key TEXT NOT NULL, user_id INTEGER,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (scope, key)
            );
            CREATE TABLE recurring_runs (
                recurring_id INTEGER NOT NULL, period TEXT NOT NULL,
                PRIMARY KEY (recurring_id, period)
            );
            CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO app_meta(key, value) VALUES ('money_stored_as_tiyn_v1', '{}');
            INSERT INTO users(user_id, username, created_at) VALUES (1, 'legacy', '2026-01-01T00:00:00+00:00');
            INSERT INTO categories(user_id, name) VALUES (1, 'Продукты');
            INSERT INTO savings_goals(user_id, name, target_amount, current_amount, created_at)
                VALUES (1, 'Старая', 100000, 25000, '2026-01-01T00:00:00+00:00');
            """
        )
        conn.close()
        db.init_db()
        db.ensure_user(1, "legacy")
        goal = db.get_goals(1)[0]
        tx_id = db.contribute_to_goal(1, goal["id"], 1000, op_date="2026-09-05")
        self.assertIsNotNone(tx_id)
        self.assertEqual(db.get_goal_by_id(1, goal["id"])["current_amount"], 26000)
        db.set_budget(1, None, 500000)
        self.assertEqual(db.get_overall_budget(1)["monthly_limit"], 500000)
        with db.get_conn() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
            notnull = [
                row["notnull"]
                for row in conn.execute("PRAGMA table_info(category_budgets)")
                if row["name"] == "category_id"
            ][0]
        self.assertIn("goal_id", cols)
        self.assertEqual(notnull, 0)


if __name__ == "__main__":
    unittest.main()
