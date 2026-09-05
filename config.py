import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # подхватывает переменные из файла .env, если он есть
except ImportError:
    pass  # python-dotenv не обязателен - можно просто экспортировать переменные в shell

# Токен бота берём из переменной окружения (получить у @BotFather в Telegram)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Публичный HTTPS-адрес Mini App (без завершающего слэша).
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")

# Версия политики, которую пользователь принимает в /start и /privacy.
PRIVACY_POLICY_VERSION = os.getenv("PRIVACY_POLICY_VERSION", "2026-08-20")

# Путь к файлу базы данных SQLite
DB_PATH = os.getenv("DB_PATH", "budget.db")

# Категории по умолчанию, которые создаются каждому новому пользователю.
# "keywords" используются модулем categorizer.py для автоматического
# определения категории товара по его названию из чека.
DEFAULT_CATEGORIES = {
    "Продукты": [
        "хлеб", "молоко", "масло", "сыр", "яйц", "мяс", "курин", "рыба",
        "овощ", "фрукт", "картоф", "капуст", "морков", "лук", "яблок",
        "банан", "крупа", "рис", "макарон", "сахар", "соль", "чай", "кофе",
        "вода", "сок", "йогурт", "творог", "колбас", "сосиск", "конфет",
        "шоколад", "печенье", "супермаркет", "магнит", "пятерочка", "перекресток",
        "ашан", "лента", "дикси", "магазин продукт",
    ],
    "Кафе и рестораны": [
        "кафе", "ресторан", "кофейн", "бар", "фастфуд", "пицц", "суши",
        "бургер", "kfc", "макдональдс", "mcdonald", "starbucks", "старбакс",
        "coffee", "restaurant", "cafe", "шаурма", "донер",
    ],
    "Транспорт": [
        "такси", "yandex go", "uber", "бензин", "заправк", "азс", "метро",
        "автобус", "проезд", "парковк", "каршеринг", "билет", "ж/д", "жд билет",
    ],
    "Одежда и обувь": [
        "одежд", "обувь", "футболк", "джинс", "куртк", "платье", "рубашк",
        "кроссовк", "ботинк", "h&m", "zara", "спортмастер",
    ],
    "Здоровье и аптека": [
        "аптек", "лекарств", "таблетк", "витамин", "клиник", "больниц",
        "врач", "стоматолог", "анализ", "медицин",
    ],
    "Развлечения": [
        "кино", "театр", "концерт", "боулинг", "бильярд", "квест", "клуб",
        "netflix", "spotify", "подписк", "игр", "steam", "playstation",
    ],
    "Коммунальные и связь": [
        "коммунальн", "электричеств", "квартплат", "интернет", "мобильн",
        "связь", "теле2", "билайн", "мтс", "мегафон", "жкх", "аренда",
    ],
    "Дом и быт": [
        "бытова", "хозтовар", "мебель", "посуда", "ремонт", "стройматериал",
        "икеа", "ikea", "leroy", "леруа",
    ],
    "Прочее": [],  # категория по умолчанию, если ничего не подошло
}

# Способы оплаты по умолчанию для нового пользователя
DEFAULT_CATEGORY_EMOJIS = {
    "Продукты": "🛒",
    "Кафе и рестораны": "☕",
    "Транспорт": "🚕",
    "Одежда и обувь": "👕",
    "Здоровье и аптека": "💊",
    "Развлечения": "🎬",
    "Коммунальные и связь": "💡",
    "Дом и быт": "🏠",
    "Прочее": "🏷",
}

DEFAULT_PAYMENT_METHODS = ["Наличные", "Карта (основная)"]

# Валюта фиксирована - тенге. Символ используется во всех местах, где
# показывается сумма (см. formatting.py)
CURRENCY_SYMBOL = "₸"

# Google Gemini - единственный способ распознавания чека.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Название модели-алиаса - Google сам направляет его на актуальную Flash-модель,
# поэтому имя не "протухнет" при выходе новых версий (в отличие от жёстко
# зашитого "gemini-2.5-flash", который в какой-то момент может быть снят с
# поддержки). Полный список: https://ai.google.dev/gemini-api/docs/models
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

# Твой Telegram user_id - сюда будет приходить фидбэк от /feedback.
# Узнать свой id можно у бота @userinfobot.
try:
    _admin_raw = os.getenv("ADMIN_USER_ID", "0")
    ADMIN_USER_ID = int(_admin_raw) or None
except ValueError:
    ADMIN_USER_ID = None

try:
    GEMINI_DAILY_LIMIT = max(1, int(os.getenv("GEMINI_DAILY_LIMIT", "10")))
except ValueError:
    GEMINI_DAILY_LIMIT = 10

try:
    GEMINI_PRO_LIMIT = max(GEMINI_DAILY_LIMIT, int(os.getenv("GEMINI_PRO_LIMIT", "50")))
except ValueError:
    GEMINI_PRO_LIMIT = 50

try:
    STARS_PRO_PRICE = max(1, int(os.getenv("STARS_PRO_PRICE", "150")))
except ValueError:
    STARS_PRO_PRICE = 150

try:
    PRO_DURATION_DAYS = max(1, int(os.getenv("PRO_DURATION_DAYS", "30")))
except ValueError:
    PRO_DURATION_DAYS = 30

try:
    BACKUP_RETAIN_DAYS = max(1, int(os.getenv("BACKUP_RETAIN_DAYS", "14")))
except ValueError:
    BACKUP_RETAIN_DAYS = 14

try:
    BOOK_INVITE_HOURS = max(1, int(os.getenv("BOOK_INVITE_HOURS", "48")))
except ValueError:
    BOOK_INVITE_HOURS = 48

BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "127.0.0.1")
try:
    WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8000"))
except ValueError:
    WEBAPP_PORT = 8000
PLAN_FREE = "free"
PLAN_PRO = "pro"