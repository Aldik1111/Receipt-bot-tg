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


def categorize_many(user_id: int, item_names: list[str]) -> list[str]:
    """Категории для списка названий. Неизвестные товары — один вызов Gemini."""
    import db
    from gemini_engine import guess_categories_ai

    category_names = [c["name"] for c in db.get_categories(user_id)]
    results: list[str | None] = [None] * len(item_names)
    unknown: list[tuple[int, str]] = []

    for index, item_name in enumerate(item_names):
        normalized = (item_name or "").strip().lower()
        if not normalized:
            results[index] = "Прочее"
            continue
        learned = db.get_learned_category_name(user_id, normalized)
        if learned:
            results[index] = learned
            continue
        local = categorize(item_name)
        if local != "Прочее":
            results[index] = local
            continue
        unknown.append((index, item_name))

    if unknown:
        guesses = guess_categories_ai([name for _, name in unknown], category_names)
        for index, item_name in unknown:
            guess = guesses.get(item_name)
            if guess and guess in category_names:
                results[index] = guess
            else:
                results[index] = "Прочее"

    return [name or "Прочее" for name in results]


def categorize_smart(user_id: int, item_name: str) -> str:
    return categorize_many(user_id, [item_name])[0]


if __name__ == "__main__":
    tests = [
        "Молоко Простоквашино 3.2% 930мл",
        "Такси Яндекс Go поездка",
        "Кроссовки Nike Air",
        "Аптека Ригла - Парацетамол",
        "Что-то совсем непонятное xyz123",
    ]
    for t in tests:
        print(f"{t!r:55} -> {categorize(t)}")
