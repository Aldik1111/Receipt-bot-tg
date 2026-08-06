"""Генерация графиков статистики (matplotlib) для отправки в Telegram картинкой."""

import io
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")  # без GUI, для сервера
import matplotlib.pyplot as plt


def pie_chart_by_category(transactions: list[dict], title: str) -> io.BytesIO:
    """transactions: список dict с ключами 'category_name' и 'amount' (только expense)."""
    totals = defaultdict(float)
    for t in transactions:
        totals[t["category_name"] or "Без категории"] += t["amount"]

    buf = io.BytesIO()
    if not totals:
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.text(0.5, 0.5, "Нет данных за период", ha="center", va="center")
        ax.axis("off")
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf

    labels = list(totals.keys())
    values = list(totals.values())

    fig, ax = plt.subplots(figsize=(7, 6))
    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.axis("equal")
    ax.set_title(title)
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def bar_chart_by_period(period_totals: dict[str, float], title: str) -> io.BytesIO:
    """period_totals: {label_периода: сумма}, например {'Янв': 1000, 'Фев': 1200}."""
    buf = io.BytesIO()
    fig, ax = plt.subplots(figsize=(8, 4.5))
    labels = list(period_totals.keys())
    values = list(period_totals.values())
    ax.bar(labels, values, color="#4C72B0")
    ax.set_title(title)
    ax.set_ylabel("Сумма")
    plt.xticks(rotation=45, ha="right")
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf