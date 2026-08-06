"""
Простая, но эффективная категоризация "продукт -> категория" без всякого ИИ:
сравниваем название товара со списком ключевых слов для каждой категории.

Это работает бесплатно, быстро и достаточно точно для типичных чеков
(товары в чеках почти всегда называются стандартно: "Молоко 3.2%",
"Хлеб бородинский", "Такси поездка" и т.п.)
"""

from config import DEFAULT_CATEGORIES


def categorize(item_name: str, categories_keywords: dict[str, list[str]] | None = None) -> str:
    """
    Возвращает наиболее вероятную категорию для названия товара.
    categories_keywords: словарь {категория: [ключевые слова]}; если не передан,
    используется DEFAULT_CATEGORIES из config.py.
    """
    if categories_keywords is None:
        categories_keywords = DEFAULT_CATEGORIES

    name = item_name.lower()

    best_category = "Прочее"
    best_score = 0

    for category, keywords in categories_keywords.items():
        score = sum(1 for kw in keywords if kw in name)
        if score > best_score:
            best_score = score
            best_category = category

    return best_category


if __name__ == "__main__":
    # Быстрый самотест
    tests = [
        "Молоко Простоквашино 3.2% 930мл",
        "Такси Яндекс Go поездка",
        "Кроссовки Nike Air",
        "Аптека Ригла - Парацетамол",
        "Что-то совсем непонятное xyz123",
    ]
    for t in tests:
        print(f"{t!r:55} -> {categorize(t)}")