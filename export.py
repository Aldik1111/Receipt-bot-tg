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
