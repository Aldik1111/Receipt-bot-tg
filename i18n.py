"""
Мультиязычность интерфейса. Три языка: ru (по умолчанию), kk (казахский), en.

t(key, lang, **kwargs) - достаёт строку по ключу и языку, подставляет
плейсхолдеры через .format(). Если для языка нет перевода конкретного ключа -
тихо откатывается на русский, а если и там нет - возвращает сам ключ (чтобы
в проде не падать, а просто было видно, что забыли перевести).

Это покрывает основные экраны (start/help, быстрый ввод, статистика,
категории/оплаты, бюджеты/цели/повторы, язык, дайджест). Более редкие
сообщения (отдельные ошибки валидации) пока остаются на русском - легко
дополнить, просто добавив ключ в словари ниже.
"""

LANGUAGES = {"ru": "Русский", "kk": "Қазақша", "en": "English"}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": {
        "start_greeting": "Привет! Я твой бюджетный менеджер.\n\n",
        "help_text": (
            "🤖 <b>Что я умею</b>\n\n"
            "⚡️ <b>Быстрый ввод</b> - напиши сообщением сумму и что купил:\n"
            "<code>500 такси</code> - расход, <code>+50000 зарплата</code> - доход.\n"
            "Категория подбирается автоматически, поправить можно кнопкой под сообщением.\n\n"
            "📸 <b>Фото чека</b> - пришли фото, я распознаю магазин, товары, цены и разложу "
            "по категориям.\n\n"
            "➕ /add - пошаговое добавление операции\n"
            "📋 /recent - список последних операций, можно править и удалять\n"
            "📊 /stats - статистика за период (в т.ч. свой)\n"
            "🔍 /category_stats - аналитика по одной категории\n"
            "🏷 /categories - свои категории\n"
            "💳 /payments - способы оплаты\n"
            "💰 /budget - лимиты по категориям\n"
            "🎯 /goals - накопительные цели\n"
            "🔁 /recurring - повторяющиеся платежи\n"
            "📤 /export - выгрузить операции в файл\n"
            "🔔 /digest - автосводка и AI-инсайты по расписанию\n"
            "⚙️ /settings - язык, автосводка, напоминания, импорт из банков, удаление данных\n"
            "📨 Перешли уведомление банка (после включения импорта в /settings) - "
            "распознаю платёж автоматически\n"
            "📎 Пришли CSV/Excel из другого приложения - предложу импортировать\n"
            "🌐 /language - сменить язык\n"
            "❓ /help - это сообщение"
        ),
        "recorded": "Записано: {amount} — {desc}",
        "no_description": "без описания",
        "category_label": "Категория",
        "edit_button": "✏️ Изменить",
        "back_button": "◀️ Назад",
        "cancel_button": "◀️ Отмена",
        "delete_button": "🗑",
        "confirm_delete": "✅ Да, удалить",
        "save_button": "✅ Сохранить",
        "add_button": "➕ Добавить",
        "period_day": "Сегодня",
        "period_week": "Неделя",
        "period_month": "Месяц",
        "period_3months": "3 месяца",
        "period_year": "Год",
        "period_custom": "📅 Свой период",
        "stats_title": "📊 Статистика {label}",
        "expenses": "💸 Расходы",
        "income": "💰 Доходы",
        "balance": "Баланс",
        "vs_prev": "к прошлому периоду",
        "categories_title": "🏷 Твои категории",
        "payments_title": "💳 Твои способы оплаты",
        "language_prompt": "Выбери язык интерфейса:",
        "language_set": "✅ Готово, теперь на русском.",
        "digest_prompt": "Как часто присылать автосводку со статистикой и AI-инсайтом?",
        "digest_off": "Выключить",
        "digest_day": "Каждый день",
        "digest_week": "Каждую неделю",
        "digest_month": "Каждый месяц",
        "digest_year": "Каждый год",
        "digest_set": "✅ Автосводка: {freq}",
        "export_prompt": "За какой период выгрузить операции?",
        "export_format_prompt": "В каком формате?",
    },
    "kk": {
        "start_greeting": "Сәлем! Мен сенің бюджет менеджеріңмін.\n\n",
        "help_text": (
            "🤖 <b>Мен не істей аламын</b>\n\n"
            "⚡️ <b>Жылдам енгізу</b> - сомасын және не сатып алғаныңды жаз:\n"
            "<code>500 такси</code> - шығын, <code>+50000 жалақы</code> - кіріс.\n"
            "Санат автоматты түрде таңдалады, хабарлама астындағы батырмамен түзетуге болады.\n\n"
            "📸 <b>Чек фотосы</b> - фото жібер, дүкенді, тауарларды, бағаларды танып, "
            "санаттарға бөлемін.\n\n"
            "➕ /add - операцияны қадам сайын қосу\n"
            "📋 /recent - соңғы операциялар тізімі, түзетуге/өшіруге болады\n"
            "📊 /stats - кезең бойынша статистика (өз кезеңің де болады)\n"
            "🔍 /category_stats - бір санат бойынша аналитика\n"
            "🏷 /categories - өз санаттарың\n"
            "💳 /payments - төлем әдістері\n"
            "💰 /budget - санаттар бойынша лимиттер\n"
            "🎯 /goals - жинақтау мақсаттары\n"
            "🔁 /recurring - қайталанатын төлемдер\n"
            "📤 /export - операцияларды файлға шығару\n"
            "🔔 /digest - кесте бойынша авто-жинақ және AI-түсінік\n"
            "⚙️ /settings - тіл, авто-жинақ, еске салулар, банктен импорт, деректерді өшіру\n"
            "📨 Банк хабарламасын қайта жіберші (алдымен /settings-те импортты қос) - "
            "төлемді автоматты танимын\n"
            "📎 Басқа қосымшадан CSV/Excel жібер - импорттауды ұсынамын\n"
            "🌐 /language - тілді ауыстыру\n"
            "❓ /help - осы хабарлама"
        ),
        "recorded": "Жазылды: {amount} — {desc}",
        "no_description": "сипаттамасыз",
        "category_label": "Санат",
        "edit_button": "✏️ Өзгерту",
        "back_button": "◀️ Артқа",
        "cancel_button": "◀️ Бас тарту",
        "delete_button": "🗑",
        "confirm_delete": "✅ Иә, өшіру",
        "save_button": "✅ Сақтау",
        "add_button": "➕ Қосу",
        "period_day": "Бүгін",
        "period_week": "Апта",
        "period_month": "Ай",
        "period_3months": "3 ай",
        "period_year": "Жыл",
        "period_custom": "📅 Өз кезеңім",
        "stats_title": "📊 Статистика ({label})",
        "expenses": "💸 Шығындар",
        "income": "💰 Кірістер",
        "balance": "Баланс",
        "vs_prev": "өткен кезеңмен салыстырғанда",
        "categories_title": "🏷 Сенің санаттарың",
        "payments_title": "💳 Сенің төлем әдістерің",
        "language_prompt": "Интерфейс тілін таңда:",
        "language_set": "✅ Дайын, енді қазақша.",
        "digest_prompt": "Статистика мен AI-түсінікті қаншалықты жиі жіберу керек?",
        "digest_off": "Өшіру",
        "digest_day": "Күн сайын",
        "digest_week": "Апта сайын",
        "digest_month": "Ай сайын",
        "digest_year": "Жыл сайын",
        "digest_set": "✅ Авто-жинақ: {freq}",
        "export_prompt": "Қай кезең үшін операцияларды шығару керек?",
        "export_format_prompt": "Қандай форматта?",
    },
    "en": {
        "start_greeting": "Hi! I'm your budget manager.\n\n",
        "help_text": (
            "🤖 <b>What I can do</b>\n\n"
            "⚡️ <b>Quick add</b> - just type the amount and what you bought:\n"
            "<code>500 taxi</code> - expense, <code>+50000 salary</code> - income.\n"
            "Category is picked automatically, tap the button under the message to fix it.\n\n"
            "📸 <b>Receipt photo</b> - send a photo, I'll recognize the store, items, prices "
            "and sort them into categories.\n\n"
            "➕ /add - add an entry step by step\n"
            "📋 /recent - recent entries, edit or delete any of them\n"
            "📊 /stats - stats for a period (including a custom range)\n"
            "🔍 /category_stats - detailed analytics for one category\n"
            "🏷 /categories - your categories\n"
            "💳 /payments - payment methods\n"
            "💰 /budget - monthly limits per category\n"
            "🎯 /goals - savings goals\n"
            "🔁 /recurring - recurring payments\n"
            "📤 /export - export entries to a file\n"
            "🔔 /digest - scheduled summary + AI insight\n"
            "⚙️ /settings - language, digest, reminders, bank import, delete all data\n"
            "📨 Forward a bank notification (after enabling import in /settings) - "
            "I'll try to recognize the payment\n"
            "📎 Send a CSV/Excel from another app - I'll offer to import it\n"
            "🌐 /language - change language\n"
            "❓ /help - this message"
        ),
        "recorded": "Recorded: {amount} — {desc}",
        "no_description": "no description",
        "category_label": "Category",
        "edit_button": "✏️ Edit",
        "back_button": "◀️ Back",
        "cancel_button": "◀️ Cancel",
        "delete_button": "🗑",
        "confirm_delete": "✅ Yes, delete",
        "save_button": "✅ Save",
        "add_button": "➕ Add",
        "period_day": "Today",
        "period_week": "Week",
        "period_month": "Month",
        "period_3months": "3 months",
        "period_year": "Year",
        "period_custom": "📅 Custom period",
        "stats_title": "📊 Stats — {label}",
        "expenses": "💸 Expenses",
        "income": "💰 Income",
        "balance": "Balance",
        "vs_prev": "vs previous period",
        "categories_title": "🏷 Your categories",
        "payments_title": "💳 Your payment methods",
        "language_prompt": "Choose interface language:",
        "language_set": "✅ Done, now in English.",
        "digest_prompt": "How often should I send a summary with stats and an AI insight?",
        "digest_off": "Turn off",
        "digest_day": "Every day",
        "digest_week": "Every week",
        "digest_month": "Every month",
        "digest_year": "Every year",
        "digest_set": "✅ Auto-summary: {freq}",
        "export_prompt": "Which period to export?",
        "export_format_prompt": "In which format?",
    },
}


def t(key: str, lang: str = "ru", **kwargs) -> str:
    text = TRANSLATIONS.get(lang, {}).get(key) or TRANSLATIONS["ru"].get(key) or key
    return text.format(**kwargs) if kwargs else text