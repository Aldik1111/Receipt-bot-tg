"""Экспорт операций пользователя в CSV/Excel для скачивания из бота."""

import csv
import io
import math

from openpyxl import Workbook

HEADERS = ["Дата", "Время", "Тип", "Сумма", "Категория", "Способ оплаты", "Магазин", "Описание"]
MAX_IMPORT_ROWS = 5000


def _row_values(r: dict) -> list:
    return [
        r["op_date"],
        r["op_time"] or "",
        "Доход" if r["type"] == "income" else "Расход",
        r["amount"],
        r["category_name"] or "",
        r["payment_name"] or "",
        r["store"] or "",
        r["description"] or "",
    ]


def export_csv(rows: list[dict]) -> io.BytesIO:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADERS)
    for r in rows:
        writer.writerow(_row_values(r))
    result = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM - чтобы Excel не путал кодировку
    result.seek(0)
    return result


def export_xlsx(rows: list[dict]) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Операции"
    ws.append(HEADERS)
    for r in rows:
        ws.append(_row_values(r))
    for col_cells in ws.columns:
        length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Импорт: разбор чужого CSV/Excel (выписка из банка или другого приложения)
# ---------------------------------------------------------------------------

import re as _re

from openpyxl import load_workbook

# Возможные названия колонок в выписках - ищем без учёта регистра/пробелов
DATE_HEADERS = {"дата", "date", "operation date", "дата операции"}
AMOUNT_HEADERS = {"сумма", "amount", "sum", "sum, kzt", "сумма, kzt"}
DESC_HEADERS = {"описание", "назначение", "description", "detail", "merchant", "получатель"}
TYPE_HEADERS = {"тип", "type", "тип операции", "направление", "операция"}
CATEGORY_HEADERS = {"категория", "category"}
PAYMENT_HEADERS = {"способ оплаты", "оплата", "payment", "payment method", "счёт", "счет", "карта"}
STORE_HEADERS = {"магазин", "store", "место", "продавец"}

# Значения колонки "Тип". Своя выгрузка пишет "Доход"/"Расход", у банков и
# других приложений встречаются английские и «списание/зачисление».
INCOME_WORDS = {"доход", "income", "credit", "зачисление", "пополнение", "приход", "+"}
EXPENSE_WORDS = {"расход", "expense", "debit", "списание", "покупка", "оплата", "-"}

# Порядок важен: сначала более узкие наборы, иначе "оплата" из PAYMENT_HEADERS
# перехватит колонку "Способ оплаты" раньше, чем сработает STORE/TYPE.
_HEADER_GROUPS = (
    ("date", DATE_HEADERS),
    ("amount", AMOUNT_HEADERS),
    ("type", TYPE_HEADERS),
    ("category", CATEGORY_HEADERS),
    ("payment", PAYMENT_HEADERS),
    ("store", STORE_HEADERS),
    ("description", DESC_HEADERS),
)


def _guess_columns(header_row: list) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        if cell is None:
            continue
        norm = str(cell).strip().lower()
        for key, headers in _HEADER_GROUPS:
            if norm in headers and key not in mapping:
                mapping[key] = idx
                break
    return mapping


def _parse_type_cell(value) -> str | None:
    """expense/income, если колонка типа заполнена понятным словом, иначе None."""
    if value is None:
        return None
    norm = str(value).strip().lower()
    if not norm:
        return None
    if norm in INCOME_WORDS:
        return "income"
    if norm in EXPENSE_WORDS:
        return "expense"
    return None


def _cell(row: list, mapping: dict[str, int], key: str):
    idx = mapping.get(key)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _text_cell(row: list, mapping: dict[str, int], key: str) -> str | None:
    value = _cell(row, mapping, key)
    if value is None:
        return None
    text = str(value).strip()
    return text[:200] or None


def _parse_date_cell(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _decode_csv_bytes(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("cp1251", errors="replace")


def parse_import_file(file_bytes: bytes, filename: str) -> list[dict]:
    """Пытается вытащить дата/сумма/описание из чужого CSV/Excel по заголовкам
    колонок. Возвращает список найденных строк - вызывающий код обязательно
    должен показать превью перед сохранением, точность не гарантируется."""
    rows_raw: list[list] = []

    if filename.lower().endswith(".xlsx"):
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            rows_raw.append(list(row))
            if len(rows_raw) > MAX_IMPORT_ROWS + 1:
                raise ValueError("too many rows")
    else:
        text = _decode_csv_bytes(file_bytes)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        for row in reader:
            rows_raw.append(row)
            if len(rows_raw) > MAX_IMPORT_ROWS + 1:
                raise ValueError("too many rows")

    if not rows_raw:
        return []

    header = rows_raw[0]
    mapping = _guess_columns(header)
    if "amount" not in mapping:
        return []  # без колонки суммы разбирать нечего

    results = []
    # Строки, где тип пришлось выводить из знака суммы. Только к ним применима
    # эвристика «в файле нет минусов - значит это всё траты»: если в файле есть
    # явная колонка типа, её значения важнее любых догадок по знаку.
    guessed_indexes: list[int] = []

    for row in rows_raw[1:]:
        amount_raw = _cell(row, mapping, "amount")
        if amount_raw is None or str(amount_raw).strip() == "":
            continue
        try:
            amount = float(str(amount_raw).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        except ValueError:
            continue
        if not math.isfinite(amount) or amount == 0:
            continue

        tx_type = _parse_type_cell(_cell(row, mapping, "type"))
        if tx_type is None:
            tx_type = "expense" if amount < 0 else "income"
            guessed_indexes.append(len(results))

        results.append({
            "amount": abs(amount),
            "type": tx_type,
            "date": _parse_date_cell(_cell(row, mapping, "date")),
            "description": _text_cell(row, mapping, "description"),
            "category": _text_cell(row, mapping, "category"),
            "payment": _text_cell(row, mapping, "payment"),
            "store": _text_cell(row, mapping, "store"),
        })

    guessed = [results[i] for i in guessed_indexes]
    if guessed and not any(r["type"] == "expense" for r in guessed):
        # Ни одной отрицательной суммы среди строк без явного типа - похоже,
        # знак направления тут не используется. Безопаснее считать их расходом
        # (это обычные траты), чем ошибочно записать все как доходы.
        for r in guessed:
            r["type"] = "expense"

    return results