"""Экспорт операций пользователя в CSV/Excel для скачивания из бота."""

import csv
import io

from openpyxl import Workbook

HEADERS = ["Дата", "Время", "Тип", "Сумма", "Категория", "Способ оплаты", "Магазин", "Описание"]


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


def _guess_columns(header_row: list) -> dict[str, int]:
    mapping = {}
    for idx, cell in enumerate(header_row):
        if cell is None:
            continue
        norm = str(cell).strip().lower()
        if norm in DATE_HEADERS and "date" not in mapping:
            mapping["date"] = idx
        elif norm in AMOUNT_HEADERS and "amount" not in mapping:
            mapping["amount"] = idx
        elif norm in DESC_HEADERS and "description" not in mapping:
            mapping["description"] = idx
    return mapping


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


def parse_import_file(file_bytes: bytes, filename: str) -> list[dict]:
    """Пытается вытащить дата/сумма/описание из чужого CSV/Excel по заголовкам
    колонок. Возвращает список найденных строк - вызывающий код обязательно
    должен показать превью перед сохранением, точность не гарантируется."""
    rows_raw: list[list] = []

    if filename.lower().endswith((".xlsx", ".xls")):
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            rows_raw.append(list(row))
    else:
        text = file_bytes.decode("utf-8-sig", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        rows_raw = [row for row in reader]

    if not rows_raw:
        return []

    header = rows_raw[0]
    mapping = _guess_columns(header)
    if "amount" not in mapping:
        return []  # без колонки суммы разбирать нечего

    results = []
    for row in rows_raw[1:]:
        if mapping["amount"] >= len(row):
            continue
        amount_raw = row[mapping["amount"]]
        if amount_raw is None or str(amount_raw).strip() == "":
            continue
        try:
            amount = float(str(amount_raw).replace(" ", "").replace(",", "."))
        except ValueError:
            continue

        date_val = row[mapping["date"]] if "date" in mapping and mapping["date"] < len(row) else None
        desc_val = row[mapping["description"]] if "description" in mapping and mapping["description"] < len(row) else None

        results.append({
            "amount": abs(amount),
            "type": "expense" if amount < 0 else "income",
            "date": _parse_date_cell(date_val),
            "description": str(desc_val).strip() if desc_val else None,
        })

    if results and not any(r["type"] == "expense" for r in results):
        # Ни одной отрицательной суммы в файле - похоже, знак направления тут
        # не используется. Безопаснее по умолчанию считать всё расходом
        # (это обычные траты), чем ошибочно записать все как доходы.
        for r in results:
            r["type"] = "expense"

    return results