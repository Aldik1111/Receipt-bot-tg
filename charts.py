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

from money import tiyn_to_tenge


def _new_figure(figsize: tuple[float, float]) -> Figure:
    fig = Figure(figsize=figsize)
    FigureCanvasAgg(fig)  # без канвы savefig не сможет отрисовать PNG
    return fig


def top_categories_with_other(
    transactions: list[dict],
    limit: int = 7,
    other_label: str = "Прочее",
    empty_label: str = "Без категории",
) -> list[tuple[str, int]]:
    totals: dict[str, int] = defaultdict(int)
    for transaction in transactions:
        totals[transaction["category_name"] or empty_label] += int(
            transaction["amount"]
        )
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    visible = ranked[:limit]
    if len(ranked) > limit:
        visible.append(
            (other_label, sum(amount for _, amount in ranked[limit:]))
        )
    return visible


def pie_chart_by_category(
    transactions: list[dict],
    title: str,
    other_label: str = "Прочее",
    empty_label: str = "Без категории",
    empty_text: str = "Нет данных за период",
) -> io.BytesIO:
    """transactions: список dict с ключами 'category_name' и 'amount' (только expense)."""
    visible = top_categories_with_other(
        transactions, other_label=other_label, empty_label=empty_label
    )

    buf = io.BytesIO()
    if not visible:
        fig = _new_figure((6, 4))
        ax = fig.subplots()
        ax.text(0.5, 0.5, empty_text, ha="center", va="center")
        ax.axis("off")
        fig.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        return buf

    labels = [name for name, _ in visible]
    values = [float(tiyn_to_tenge(amount)) for _, amount in visible]

    fig = _new_figure((7, 6))
    ax = fig.subplots()
    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.axis("equal")
    ax.set_title(title)
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    return buf


def bar_chart_by_period(
    period_totals: dict[str, int],
    title: str,
    ylabel: str = "Сумма",
) -> io.BytesIO:
    """period_totals: {label_периода: сумма в тиынах}, например {'Янв': 100000}."""
    buf = io.BytesIO()
    fig = _new_figure((8, 4.5))
    ax = fig.subplots()
    labels = list(period_totals.keys())
    values = [float(tiyn_to_tenge(int(v))) for v in period_totals.values()]
    ax.bar(labels, values, color="#4C72B0")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    return buf
