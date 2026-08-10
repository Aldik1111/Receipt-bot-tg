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


def categorize_smart(user_id: int, item_name: str) -> str:
    """Три уровня, по порядку:
    1) Слово уже было один раз исправлено пользователем вручную - используем
       запомненную категорию (быстро, бесплатно, 100% предсказуемо).
    2) Локальный словарь ключевых слов (быстро, бесплатно).
    3) Только если словарь не справился (результат - "Прочее") - спрашиваем
       ИИ (Gemini), и если он дал уверенный ответ, ЗАПОМИНАЕМ его как выученное
       слово, чтобы в следующий раз не тратить время/запрос на тот же товар.
    """
    import db
    from gemini_engine import guess_category_ai

    normalized = item_name.strip().lower()

    learned = db.get_learned_category_name(user_id, normalized)
    if learned:
        return learned

    result = categorize(item_name)
    if result != "Прочее":
        return result

    category_names = [c["name"] for c in db.get_categories(user_id)]
    ai_guess = guess_category_ai(item_name, category_names)
    if ai_guess and ai_guess in category_names:
        cat_id = db.get_category_id_by_name(user_id, ai_guess)
        if cat_id:
            db.learn_category(user_id, normalized, cat_id)
        return ai_guess

    return "Прочее"


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