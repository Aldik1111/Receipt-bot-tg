"""Генерация графиков статистики (matplotlib) для отправки в Telegram картинкой.

Рисуем через объектный API (Figure + Agg-канва), а не через pyplot: у pyplot
есть глобальный реестр фигур, который не потокобезопасен, а bot.py вызывает
эти функции из asyncio.to_thread (несколько пользователей могут строить
графики одновременно). Побочный бонус - не нужен plt.close(), фигура нигде
не регистрируется и собирается сборщиком мусора сама.
"""

import io
from collections import defaultdict

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure


def _new_figure(figsize: tuple[float, float]) -> Figure:
    fig = Figure(figsize=figsize)
    FigureCanvasAgg(fig)  # без канвы savefig не сможет отрисовать PNG
    return fig


def pie_chart_by_category(transactions: list[dict], title: str) -> io.BytesIO:
    """transactions: список dict с ключами 'category_name' и 'amount' (только expense)."""
    totals = defaultdict(float)
    for t in transactions:
        totals[t["category_name"] or "Без категории"] += t["amount"]

    buf = io.BytesIO()
    if not totals:
        fig = _new_figure((6, 4))
        ax = fig.subplots()
        ax.text(0.5, 0.5, "Нет данных за период", ha="center", va="center")
        ax.axis("off")
        fig.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        return buf

    labels = list(totals.keys())
    values = list(totals.values())

    fig = _new_figure((7, 6))
    ax = fig.subplots()
    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.axis("equal")
    ax.set_title(title)
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    return buf


def bar_chart_by_period(period_totals: dict[str, float], title: str) -> io.BytesIO:
    """period_totals: {label_периода: сумма}, например {'Янв': 1000, 'Фев': 1200}."""
    buf = io.BytesIO()
    fig = _new_figure((8, 4.5))
    ax = fig.subplots()
    labels = list(period_totals.keys())
    values = list(period_totals.values())
    ax.bar(labels, values, color="#4C72B0")
    ax.set_title(title)
    ax.set_ylabel("Сумма")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    return buf
