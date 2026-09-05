"""One-off helper: dump remaining locale files from the RU dictionary."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RU = json.loads((ROOT / "locales" / "ru.json").read_text(encoding="utf-8"))

KK_EXTRA = {
    "enter_new_description": "Жаңа сипаттама (немесе өшіру үшін '-'):",
    "description_updated": "✅ Сипаттама жаңартылды.\n\n{text}",
    "choose_new_category": "Жаңа санатты таңда:",
    "category_updated_learned": "✅ Санат жаңартылды (келесі жолы есте сақтаймын).\n\n{text}",
    "choose_new_payment": "Жаңа төлем әдісін таңда:",
    "payment_updated": "✅ Төлем әдісі жаңартылды.\n\n{text}",
    "delete_tx_confirm": "Осы операцияны қалпына келтірусіз өшіреміз бе?",
    "delete_tx_goal_block": "Өшіруге болмайды: мақсат минусқа кетеді. /goals арқылы өзгерт.",
    "deleted": "✅ Өшірілді.\n\n{text}",
    "transfer_to_goal": "Мақсатқа аударым «{name}»",
    "transfer_from_goal": "Мақсаттан аударым «{name}»",
    "goal_fallback": "мақсат",
    "catalog_categories_hint": (
        "🏷 <b>Сенің санаттарың</b>\n\n"
        "🎨 эмодзи, ✏️ атау, 🗑 өшіру."
    ),
    "protected_category": "Бұл негізгі санат — оны өзгертуге немесе өшіруге болмайды",
    "protected_rename": "Бұл негізгі санат — атауын өзгертуге болмайды",
    "enter_category_name": "Жаңа санаттың атауын енгіз:",
    "category_added": "✅ «{name}» санаты қосылды.\n\n{text}",
    "enter_category_rename": "Осы санаттың жаңа атауын енгіз:",
    "enter_category_emoji": "Осы санатқа бір эмодзи жібер:",
    "emoji_bad": "Бір эмодзи жібер, мысалы 👨‍👩‍👧‍👦.",
    "emoji_updated": "✅ Эмодзи жаңартылды.\n\n{text}",
    "renamed_to": "✅ «{name}» деп өзгертілді.\n\n{text}",
    "category_exists": "⚠️ «{name}» санаты бар — басқа атау таңда.\n\n{text}",
    "delete_category_q": "Осы санатты өшіреміз бе?\n{note}",
    "category_has_tx": "Онда {count} операция бар — олар «{fallback}» санатына көшеді.",
    "category_no_tx": "Онда әлі операция жоқ.",
    "delete_failed": "⚠️ Өшіру мүмкін болмады.\n\n{text}",
    "btn_add_category": "➕ Санат қосу",
    "catalog_payments_hint": (
        "💳 <b>Сенің төлем әдістерің</b>\n\n"
        "✏️ атауын өзгерту, 🗑 өшіру."
    ),
    "btn_add_payment": "➕ Төлем әдісін қосу",
    "enter_payment_name": "Төлем әдісінің атауын енгіз (мысалы: Kaspi Gold):",
    "payment_added": "✅ «{name}» төлем әдісі қосылды.\n\n{text}",
    "enter_payment_rename": "Осы төлем әдісінің жаңа атауын енгіз:",
    "payment_exists": "⚠️ «{name}» төлем әдісі бар — басқа атау таңда.\n\n{text}",
    "delete_payment_q": "Осы төлем әдісін өшіреміз бе?\n{note}",
    "payment_has_tx": "Онда {count} операция бар — олардың төлем әдісі бос болады.",
    "payment_no_tx": "Онда әлі операция жоқ.",
    "last_payment_block": "⚠️ Соңғы төлем әдісін өшіруге болмайды.\n\n{text}",
    "budgets_title": "💰 <b>Бюджеттер</b>",
    "no_budgets": "Әлі бірде-бір лимит жоқ.",
    "overall_expenses": "Барлық шығындар",
    "btn_cat_limit": "➕ Санат лимиті",
    "btn_overall_budget": "🌐 Жалпы айлық бюджет",
    "budget_pick_cat": "Қай санатқа лимит қоямыз?",
    "budget_enter_overall": "Барлық шығынға айлық лимитті енгіз (бос орынсыз сан):",
    "budget_enter_cat": "Осы санаттың айлық лимитін енгіз (бос орынсыз сан):",
    "amount_positive_big": "Нөлден үлкен сан жібер, мысалы: 100000",
    "budget_set": "✅ Лимит орнатылды.\n\n{text}",
    "removed": "Өшірілді",
    "goals_title": "🎯 <b>Жинақтау мақсаттары</b>",
    "no_goals": "Әлі бірде-бір мақсат жоқ.",
    "deadline_none": "жоқ",
    "btn_new_goal": "➕ Жаңа мақсат",
    "goal_saved": "Жиналды: {current} / {target}",
    "goal_deadline": "Мерзім: {value}",
    "goal_note": "Толықтыру аударым жасайды, шығын емес — /stats балансы дұрыс қалады.",
    "btn_contribute": "➕ Толықтыру",
    "btn_withdraw": "➖ Алу",
    "btn_name": "✏️ Атауы",
    "btn_target": "🎯 Сома",
    "btn_deadline": "📅 Мерзім",
    "goal_not_found": "Мақсат табылмады",
    "goal_not_found_long": "Мақсат табылмады (мүмкін өшірілген).",
    "enter_goal_name": "Мақсат атауы (мысалы: Демалыс):",
    "enter_goal_target": "Қанша жинау керек (сан)?",
    "amount_positive_goal": "Нөлден үлкен сан жібер, мысалы: 300000",
    "enter_goal_deadline": "Мерзім КК.АА.ЖЖЖЖ немесе «-», егер мерзімсіз:",
    "bad_date_or_dash": "Күнді тани алмадым. КК.АА.ЖЖЖЖ немесе «-» жаз.",
    "goal_created": "✅ Мақсат құрылды.\n\n{text}",
    "enter_contribute": "Мақсатты қаншаға толықтырамыз?",
    "amount_positive_short": "Нөлден үлкен сан жібер.",
    "goal_reached": "🎉 Мақсат орындалды!\n\n",
    "goal_contributed": "✅ Аударыммен толықтырылды.\n\n",
    "goal_empty": "Мақсатта әзірге алатын ештеңе жоқ",
    "enter_withdraw": "Қанша аламыз? Қазір мақсатта {amount}.",
    "withdraw_too_much": "Жиналғаннан көп алуға болмайды. Мақсат минусқа кетпейді.",
    "goal_withdrawn": "✅ Мақсаттан алынды.\n\n",
    "enter_goal_rename": "Мақсаттың жаңа атауы:",
    "name_updated": "✅ Атауы жаңартылды.\n\n{text}",
    "enter_goal_new_target": "Жаңа мақсат сомасы:",
    "target_updated": "✅ Мақсат сомасы жаңартылды.\n\n{text}",
    "enter_goal_new_deadline": "Жаңа мерзім КК.АА.ЖЖЖЖ немесе өшіру үшін «-»:",
    "deadline_updated": "✅ Мерзім жаңартылды.\n\n{text}",
    "delete_goal_confirm": (
        "Мақсатты өшіреміз бе? Қалдық қолжетімді ақшаға аударылады, аударым тарихы сақталады."
    ),
    "recurring_title": "🔁 <b>Қайталанатын төлемдер</b>",
    "no_recurring": "Әлі бірде-біреуі қосылмаған.",
    "recurring_line": "{status} {emoji} {amount} — {label}, айдың {day}-і",
    "payment_fallback": "төлем",
    "rec_status_on": "белсенді",
    "rec_status_off": "кідіртілген",
    "rec_status_label": "Күйі: {status}",
    "rec_day_label": "Ай күні: {day}",
    "rec_amount_label": "Сома: {amount}",
    "btn_pause": "⏸ Кідірту",
    "btn_resume": "▶️ Қосу",
    "btn_day": "📅 Күн",
    "rec_not_found": "Төлем табылмады",
    "rec_not_found_long": "Төлем табылмады (мүмкін өшірілген).",
    "rec_what": "Нені қайталаймыз?",
    "enter_day_of_month": "Айдың қай күні (1-28)?",
    "day_range": "1-ден 28-ге дейінгі сан жібер (барлық айға жарайды).",
    "day_range_short": "1-ден 28-ге дейінгі сан жібер.",
    "enter_rec_name": "Төлем атауы (мысалы: Пәтер жалдау):",
    "rec_added": "✅ Қайталанатын төлем қосылды.\n\n{text}",
    "enter_rec_amount": "Қайталаудың жаңа сомасы:",
    "rec_amount_updated": "✅ Сома жаңартылды. Өткен іске қосулар өзгермеді.\n\n{text}",
    "day_updated": "✅ Күн жаңартылды.\n\n{text}",
    "enter_rec_desc": "Төлемнің жаңа атауы:",
    "rec_cat_fail": "Санатты өзгерту мүмкін болмады",
    "rec_pay_fail": "Төлем әдісін өзгерту мүмкін болмады",
    "category_updated": "✅ Санат жаңартылды.\n\n{text}",
    "export_empty": "Осы кезеңде операция жоқ — шығаратын ештеңе жоқ.",
    "export_large": "⏳ Үлкен экспорт: {count} операция. Файл дайындау біраз уақыт алуы мүмкін.",
    "export_ready": "Дайын, {count} операция 👇",
    "restore_prompt": (
        "📥 Бұрын жіберген бэкап файлын (.json) жібер.\n\n"
        "⚠️ Қазіргі операциялар, чектер, қайталаулар, бюджеттер мен мақсаттар "
        "файл мазмұнымен <b>толық ауыстырылады</b>. Кері қайтару болмайды.\n\n"
        "/cancel — бас тарту."
    ),
    "file_too_big_20": "Файл тым үлкен (ең көбі 20 МБ).",
    "restore_not_json": "❌ Бұл JSON-бэкапқа ұқсамайды. Өзгертілмеген файл жібер немесе /cancel.",
    "restore_bad_format": "❌ Бэкап форматы қате. Өзгертілмеген файл жібер немесе /cancel.",
    "restore_preview": (
        "🧾 Чектер: {receipts}\n"
        "💰 Операциялар: {transactions}\n"
        "🔁 Қайталаулар: {recurring}\n"
        "🎯 Мақсаттар: {goals}\n"
        "🏷 Санаттар: {categories}"
    ),
    "restore_confirm_q": "Файлда таптым:\n{counts}\n\n⚠️ Қазіргі деректер осымен ауыстырылады. Растайсың ба?",
    "btn_replace_data": "✅ Деректерді ауыстыру",
    "btn_no": "❌ Бас тарту",
    "restore_need_file": "Бэкап файлын (.json) күтемін. /cancel — бас тарту.",
    "not_your_draft": "Бұл сенің жобаң емес.",
    "draft_stale_file": "Жоба ескірген, файлды қайта жібер.",
    "restore_failed": (
        "❌ Бэкапты қалпына келтіру мүмкін болмады — файл бүлінген немесе үйлесімсіз. "
        "Қазіргі деректерің өзгермеді."
    ),
    "restore_done": (
        "✅ Деректер қалпына келтірілді:\n"
        "💰 Операциялар: {transactions}\n"
        "🧾 Чектер: {receipts}\n"
        "🔁 Қайталаулар: {recurring}\n"
        "🎯 Мақсаттар: {goals}"
    ),
    "restore_cancelled": "Қалпына келтіру тоқтатылды, деректер өзгермеді.",
    "import_too_big": "⚠️ Файл тым үлкен. Ең көбі 2 МБ.",
    "import_too_many": "⚠️ Файлда жол тым көп (лимит 5000).",
    "import_unreadable": "⚠️ Файлды оқи алмадым. Күні/сомасы/сипаттамасы бар CSV пен Excel қолданылады.",
    "import_no_amount": (
        "⚠️ Файлда сома бағанын таппадым. Бірінші жолда "
        "«Сумма» / «Amount» сияқты тақырып бар екенін тексер."
    ),
    "import_header": "📤 {count} операция таптым:\n💸 Шығындар: {expense}",
    "import_income_line": "\n💰 Кірістер: {amount}",
    "import_dups": (
        "\n\n⚠️ {count} жол бұрын импортталғанға ұқсайды. "
        "Басқа файл болса, қайта жүктеуге болады."
    ),
    "import_ask": "\n\nИмпорттаймыз ба?",
    "btn_import_all": "✅ Барлығын импорттау ({count})",
    "draft_stale": "Жоба ескірген",
    "imported": "✅ {count} операция импортталды.",
    "import_cancelled": "❌ Импорт тоқтатылды.",
    "receipt_err_too_large": "⚠️ Фото 8 МБ-тан үлкен. Кішірейт немесе фото ретінде жібер, үлкен файл емес.",
    "receipt_err_unsupported": "⚠️ Бұл формат қолдаусыз. JPEG, PNG немесе WebP керек.",
    "receipt_err_corrupt": "⚠️ Файл бүлінген суретке ұқсайды. Басқа фото жібер.",
    "receipt_err_rate_limit": (
        "⚠️ Gemini қазір шектеуде. Біраздан кейін көр "
        "немесе мәтінмен жаз: <code>500 такси</code>"
    ),
    "receipt_err_network": (
        "⚠️ Gemini-ге қосыла алмадым. Кейінірек көр немесе мәтінмен жаз: "
        "<code>500 такси</code>"
    ),
    "receipt_err_bad_json": "⚠️ Gemini түсініксіз жауап берді. Басқа фото жібер немесе мәтінмен жаз.",
    "receipt_err_no_key": "⚠️ Чек тану бапталмаған. Шығынды мәтінмен жаз: <code>500 такси</code>",
    "receipt_err_unavailable": (
        "⚠️ Gemini қазір қолжетімсіз. Кейінірек көр немесе мәтінмен жаз: "
        "<code>500 такси</code>"
    ),
    "receipt_err_quota": (
        "⚠️ Бүгінгі чек тану лимиті бітті ({limit}). "
        "Ертең фотомен болады, қазір мәтінмен: <code>500 такси</code>"
    ),
    "receipt_err_empty": (
        "⚠️ Чекте тауар таппадым (Gemini тани алмады).\n"
        "Кеңес: чекті ФАЙЛ ретінде жібер (📎 → Файл) — "
        "Telegram суретті сықпайды.\n"
        "Немесе жай жаз: 500 такси"
    ),
    "receipt_source_bank": "🏦 Банк",
    "receipt_recognizing": "🔍 Чекті танып жатырмын, сәл күт...",
    "receipt_dup_prompt": "⚠️ Бұл чек бұрын сақталған сияқты ({seen}). Қайта танып жазамыз ба?",
    "btn_save_again": "✅ Тағы сақтау",
    "btn_no_need": "❌ Керек емес",
    "receipt_title": "🧾 <b>Танылған чек</b>",
    "receipt_source": "<i>Көзі: {name}</i>",
    "receipt_dup_inline": (
        "⚠️ Бұл бұрын сақталған сияқты ({seen}). "
        "Басқа сатып алу болса, қайта жазуға болады."
    ),
    "receipt_store": "Дүкен: {name}",
    "receipt_datetime": "Күні: {date}   Уақыты: {time}",
    "receipt_type": "Түрі: {name}",
    "receipt_choose_type": "⚠️ Сақтаудан бұрын операция түрін таңда.",
    "receipt_pay": "Төлем: {name}",
    "receipt_items_total": "Тауарлар қосындысы: {amount}",
    "receipt_total": "Чек қорытындысы: {amount}",
    "receipt_mismatch": (
        "\n⚠️ Қорытындылар {amount} айырмашылықта. Қай сомаға сенеміз?"
    ),
    "receipt_check_items": "\nЖолдарды тексер: сақтаудан бұрын түзетуге немесе өшіруге болады.",
    "receipt_truncated": "\n… кейбір жолдар көрсетілмеді, бірақ төмендегі батырмаларда бар.",
    "receipt_item_fallback": "Жол {n}",
    "btn_trust_items": "🛒 Тауарларға сену",
    "btn_trust_total": "🧾 Қорытындыға сену",
    "btn_save_all": "✅ Барлығын сақтау",
    "draft_stale_item": "Жоба немесе жол ескірген",
    "btn_fix": "✏️ Түзету",
    "btn_to_receipt": "◀️ Чекке",
    "receipt_edit_prompt": (
        "Атау мен бағаны <code>|</code> арқылы жаз:\n"
        "<code>{sample}</code>\n\n"
        "Мысалы: <code>Сүт 2,5% | 450</code>\n"
        "/cancel — бас тарту."
    ),
    "receipt_edit_format": "Формат: атауы | бағасы. Мысалы: Сүт | 450",
    "receipt_edit_need": "Бос емес атау және нөлден үлкен баға керек.",
    "draft_stale_resend": "Жоба немесе жол ескірген. Фотоны қайта жібер.",
    "item_updated": "✅ Жол жаңартылды.\n\n{text}",
    "last_item_block": "Соңғы жолды өшіруге болмайды — бүкіл чекті тоқтат.",
    "item_deleted": "Жол өшірілді",
    "draft_update_fail": "Жобаны жаңарту мүмкін болмады",
    "used_items_total": "Тауарлар қосындысын қолданамын",
    "receipt_total_bad": "Чек қорытындысы қате",
    "used_receipt_total": "Жолдар чек қорытындысына қарай қайта есептелді",
    "choose_type_first": "Алдымен кіріс немесе шығынды таңда",
    "choose_total_first": "Алдымен қай қорытындыны қолданатыныңды таңда",
    "save_retry": "Сақтау мүмкін болмады. Жоба жоғалған жоқ — қайта көр.",
    "draft_stale_photo": "Жоба ескірген, фотоны қайта жібер",
    "receipt_saved": "\n\n✅ Базаға сақталды!",
    "btn_receipt_items": "📝 Чек жолдары (түзету)",
    "saved": "Сақталды",
    "receipt_items_title": "🧾 <b>Чек жолдары</b>\n",
    "items_not_found": "Жолдар табылмады",
    "receipt_cancelled": "❌ Бас тартылды, ештеңе сақталмады.",
    "choose_how_paid": "Қалай төледің?",
    "draft_or_pay_bad": "Жоба ескірген немесе төлем әдісі бөтен",
    "request_stale": "Сұрау ескірген",
    "recognize_again": "Жарайды, қайта танимын.",
    "dup_skipped": "Жарайды, ештеңе сақтамаймын. Басқа сатып алу болса — фотоны қайта жібер.",
    "sched_recurring": "🔁 Автоматты қосылды: {emoji} {amount} — {desc}",
    "sched_idle": (
        "👋 Жаңа жазбалар көптен бері жоқ. Бәрі жақсы ма? Қажет болса — "
        "сома мен не алғаныңды жаз, бір секунд алады."
    ),
    "sched_backup": "📦 Деректеріңнің апталық автобэкабы",
    "app_title": "📊 Менің бюджетім",
    "tab_overview": "Шолу және операциялар",
    "tab_budgets": "Бюджеттер",
    "tab_goals": "Мақсаттар",
    "custom_apply": "Көрсету",
    "all_categories": "Барлық санаттар",
    "by_categories": "Санаттар бойынша",
    "transactions_title": "Операциялар",
    "no_transactions": "Операция табылмады",
    "load_error": "Деректерді жүктеу мүмкін болмады. Mini App-ті ботта /start-тан кейін аш.",
    "form_amount": "Сома",
    "form_type": "Түрі",
    "form_category": "Санат",
    "form_date": "Күні",
    "form_description": "Сипаттама",
    "form_store": "Дүкен",
    "form_payment": "Төлем",
    "created_ok": "Операция сақталды",
    "updated_ok": "Операция жаңартылды",
    "save_error": "Сақтау мүмкін болмады",
    "validation_error": "Соманы, күнді және санатты тексер",
}

EN = {
    "start_greeting": "Hi! I'm your budget manager.\n\n",
    "onboarding_text": (
        "Hi! I'll help you track spending. Three short steps:\n\n"
        "1. Type <code>500 taxi</code> — I'll save an expense.\n"
        "2. Send a receipt photo — I'll read items and prices.\n"
        "3. Read the policy: receipt photos go to Gemini.\n\n"
        "Tapping Accept means you agree to /privacy."
    ),
    "onboarding_accept": "✅ Accept the policy",
    "onboarding_done": "Done. Type <code>500 taxi</code> or send a receipt photo.",
    "help_text": (
        "🤖 <b>What I can do</b>\n\n"
        "⚡️ <b>Quick add</b> - just type the amount and what you bought:\n"
        "<code>500 taxi</code> - expense, <code>+50000 salary</code> - income.\n"
        "Category is picked automatically, tap the button under the message to fix it.\n\n"
        "📸 <b>Receipt photo</b> - send a photo, I'll recognize the store, items, prices "
        "and sort them into categories.\n\n"
        "➕ /add - add an entry step by step\n"
        "📅 /today - today's entries\n"
        "📋 /recent - recent entries, edit or delete any of them\n"
        "🔎 /search - search descriptions and stores\n"
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
        "🔒 /privacy - privacy policy\n"
        "💬 /feedback - send feedback to the developer\n"
        "❓ /help - this message"
    ),
    "privacy_text": (
        "🔒 <b>Privacy policy</b> (version {version})\n\n"
        "I store your Telegram id, transactions, categories and settings on the bot server.\n"
        "Receipt photos and short digest summaries go to Google Gemini and are not kept by me.\n"
        "Raw bank notifications are not stored after parsing.\n"
        "You can delete everything in /settings. Details are in PRIVACY_POLICY.md.\n\n"
        "Accepted version: {accepted}."
    ),
    "privacy_not_accepted": "not accepted yet",
    "privacy_accept": "✅ Accept this version",
    "privacy_accepted": "✅ Policy accepted. Version: {version}.",
    "feedback_prompt": "Write a message — a bug, an idea, anything. I'll forward it to the developer.",
    "feedback_disabled": "Feedback is not configured by the developer yet.",
    "feedback_sent": "✅ Sent, thank you!",
    "feedback_failed": "⚠️ Couldn't send it, try again later.",
    "cancel_cleared": "Ok, the flow is cleared. You can start again.",
    "cancel_idle": "There's no active flow right now.",
    "text_hint": "Send it as text. /cancel — reset the flow.",
    "outdated_button": "This button is outdated. Open the section again.",
    "recorded": "Recorded: {amount} — {desc}",
    "no_description": "no description",
    "uncategorized": "Uncategorized",
    "unspecified": "Unspecified",
    "category_label": "Category",
    "edit_button": "✏️ Edit",
    "back_button": "◀️ Back",
    "cancel_button": "◀️ Cancel",
    "delete_button": "🗑",
    "confirm_delete": "✅ Yes, delete",
    "save_button": "✅ Save",
    "add_button": "➕ Add",
    "more_button": "More",
    "dash": "—",
    "period_day": "Today",
    "period_week": "Week",
    "period_month": "Month",
    "period_3months": "3 months",
    "period_year": "Year",
    "period_all": "All time",
    "period_custom": "📅 Custom period",
    "period_label_day": "today",
    "period_label_week": "this week",
    "period_label_month": "this month",
    "period_label_3months": "last 3 months",
    "period_label_year": "this year",
    "period_label_custom": "custom period",
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
    "cmd_start": "start and short help",
    "cmd_help": "what the bot can do",
    "cmd_add": "add an entry step by step",
    "cmd_today": "today's entries",
    "cmd_recent": "recent entries",
    "cmd_search": "search entries",
    "cmd_stats": "stats",
    "cmd_category_stats": "stats for one category",
    "cmd_categories": "categories",
    "cmd_payments": "payment methods",
    "cmd_budget": "budgets",
    "cmd_goals": "goals",
    "cmd_recurring": "recurring payments",
    "cmd_export": "export",
    "cmd_digest": "auto-summary",
    "cmd_settings": "settings",
    "cmd_language": "language",
    "cmd_privacy": "privacy policy",
    "cmd_feedback": "message the developer",
    "cmd_cancel": "reset the current flow",
    "webapp_button": "Budget",
    "settings_title": "⚙️ <b>Settings</b>",
    "settings_language": "🌐 Language: {name}",
    "settings_timezone": "🕒 Time zone: {name}",
    "settings_digest": "🔔 Auto-summary: {name}",
    "settings_idle": "📉 Idle reminder: {state}",
    "settings_backup": "📦 Weekly auto-backup: {state}",
    "settings_bank": "🏦 Bank notification import: {state}",
    "state_on": "on",
    "state_off": "off",
    "btn_change_language": "🌐 Change language",
    "btn_timezone": "🕒 Time zone",
    "btn_digest_freq": "🔔 Auto-summary frequency",
    "btn_idle_toggle": "📉 Reminder: {action}",
    "btn_backup_toggle": "📦 Auto-backup: {action}",
    "btn_bank_toggle": "🏦 Bank import: {action}",
    "action_enable": "enable",
    "action_disable": "disable",
    "btn_backup_now": "📦 Download backup now",
    "btn_restore": "📥 Restore from file",
    "btn_danger": "⚠️ Delete all my data",
    "tz_prompt": "Choose a time zone. It controls today, stats, recurring payments and the digest.",
    "tz_invalid": "This time zone is not available",
    "tz_updated": "Time zone updated",
    "backup_too_big": "The backup is too large for Telegram, download it from the server",
    "backup_send_fail": "Couldn't send the file",
    "backup_sent": "Backup sent",
    "backup_note_on": "\n\n📦 Once a week I'll send a JSON backup to this chat.",
    "bank_note_on": (
        "\n\n📨 Now just forward me a payment notification from your bank app — "
        "I'll try to recognize the amount and store."
    ),
    "danger_intro": (
        "⚠️ <b>This will permanently delete:</b>\n"
        "— {count} transactions\n"
        "— all categories, payment methods, budgets and goals\n\n"
        "This cannot be undone (unless you made a /settings backup first).\n\n"
        "Continue?"
    ),
    "danger_confirm": "⚠️ Yes, I understand",
    "danger_phrase_prompt": (
        "Last step. To confirm, type exactly:\n\n"
        "<code>{phrase}</code>"
    ),
    "danger_phrase": "DELETE EVERYTHING",
    "danger_mismatch": "The phrase didn't match — nothing was deleted. Changing your mind is fine.",
    "danger_done": "Done, all data is deleted. /start — to begin again.",
    "cancelled": "Cancelled",
    "add_what": "What are we adding?",
    "type_expense": "💸 Expense",
    "type_income": "💰 Income",
    "type_transfer": "🔄 Transfer",
    "enter_amount": "Enter the amount:",
    "amount_example": "Send a number, for example: 350 or 350.50",
    "choose_category": "Choose a category:",
    "choose_payment": "Payment method:",
    "enter_description": "Short description (or send '-' to skip):",
    "quick_add_hint": (
        "I didn't get that 🤔 Format: <b>amount</b> and what you bought, for example:\n"
        "<code>500 taxi</code>, <code>yesterday 500 taxi</code> or "
        "<code>500 taxi 03.09.2026</code>\n"
        "Or use /add for the step-by-step flow."
    ),
    "quick_add_bad_amount": "Couldn't parse the amount, try again.",
    "bank_unparsed": "📨 This looks like a bank notification, but I couldn't read the amount.",
    "bank_ambiguous": "⚠️ Not sure if this is income or expense. Check the type before saving.\n\n",
    "ask_period": "For which period?",
    "pct_na": "n/a",
    "pct_new": "new",
    "budget_overall_name": "all expenses",
    "budget_overall_label": "Overall budget",
    "budget_category_label": "Budget for “{name}”",
    "budget_exceeded": " exceeded",
    "budget_warning": "{icon} {label}{suffix}: {spent} of {limit} ({pct:.0f}%)",
    "stats_period": "Period: {start} — {end}",
    "stats_forecast": "\n📈 Forecast by period end: ~{amount}",
    "stats_goals_period": "\n🎯 To goals this period: {amount}",
    "stats_goals_now": "\n🏦 Saved in goals: {amount}",
    "stats_expense_line": "💸 Expenses: {amount}  ({change} {vs})",
    "stats_income_line": "💰 Income: {amount}",
    "stats_balance_line": "Balance: {amount}",
    "show_operations": "📋 Show transactions",
    "no_expenses": "No expenses in this period.",
    "chart_expenses": "Expenses {label}",
    "chart_trend": "Spending trend {label}",
    "chart_no_data": "No data for this period",
    "chart_axis": "Amount",
    "chart_other": "Other",
    "top_stores": "📍 <b>Top places</b>",
    "top_items": "🛒 <b>Top items</b>",
    "top_item_line": "• {name} — {count} pcs, {amount}",
    "by_payments": "💳 <b>By payment method</b>",
    "custom_period_prompt": (
        "Enter a period as <code>DD.MM.YYYY DD.MM.YYYY</code>\n"
        "For example: <code>01.03.2026 15.03.2026</code>"
    ),
    "custom_period_need_two": "Need two dates separated by a space, for example: 01.03.2026 15.03.2026",
    "custom_period_bad": "Couldn't parse the dates. Format: DD.MM.YYYY DD.MM.YYYY",
    "category_stats_pick": "Which category should I show stats for?",
    "category_stats_body": (
        "🔍 <b>{name}</b> — {label}\n"
        "Period: {start} — {end}\n\n"
        "Spent: {spent}\n"
        "Share of all expenses: {pct:.0f}%\n"
        "Vs previous period: {change} (was {prev})"
    ),
    "recent_period_title": "📋 <b>Transactions for the period</b>\n{start} — {end}",
    "recent_title": "📋 <b>Recent transactions</b>",
    "filter_search": "search “{query}”",
    "filter_category": "category “{name}”",
    "filter_type": "type “{name}”",
    "filters_line": "Filters: {filters}",
    "btn_search": "🔎 Search",
    "btn_filters": "⚙️ Filters",
    "btn_clear_filters": "✖️ Clear filters",
    "nothing_found": "Nothing found.",
    "page_of": "Page {page} of {total}",
    "nav_earlier": "◀️ Earlier",
    "nav_later": "Later ▶️",
    "search_prompt": "What should I find in descriptions or store names?",
    "search_empty": "Send a non-empty query or /cancel.",
    "choose_filter": "Choose a filter:",
    "btn_category": "🏷 Category",
    "btn_period": "📅 Period",
    "ask_period_show": "Which period should I show?",
    "filters_cleared": "Filters cleared",
    "tx_not_found": "Transaction not found (it may already be deleted)",
    "tx_type_label": "Type: {name}",
    "tx_date_label": "Date: {date}",
    "tx_time_label": "  Time: {time}",
    "tx_category_label": "Category: {name}",
    "tx_payment_label": "Payment method: {name}",
    "tx_store_label": "Store: {name}",
    "tx_desc_label": "Description: {name}",
    "btn_payment": "💳 Payment",
    "btn_description": "📝 Description",
    "btn_date": "📅 Date",
    "btn_delete": "🗑 Delete",
    "btn_to_list": "◀️ Back to list",
    "btn_amount": "✏️ Amount",
    "btn_store": "🏪 Store",
    "btn_tx_type": "🔄 Transaction type",
    "enter_new_amount": "Enter the new amount:",
    "amount_positive": "Send a number greater than zero, for example: 350",
    "transfer_amount_via_goals": "Change a goal transfer amount via /goals.",
    "amount_updated": "✅ Amount updated.\n\n{text}",
    "enter_new_date": (
        "Enter the new date as <code>DD.MM.YYYY</code>, for example "
        "<code>04.09.2026</code>."
    ),
    "bad_date": "Couldn't parse the date. Use DD.MM.YYYY, for example 04.09.2026.",
    "date_updated": "✅ Date updated.\n\n{text}",
    "enter_store": "Enter the new store (or '-' to clear):",
    "store_too_long": "Store name is too long. Maximum 128 characters.",
    "store_updated": "✅ Store updated.\n\n{text}",
    "choose_tx_type": "Choose the new transaction type:",
    "tx_type_bad": "Transaction not found or type is invalid",
    "tx_type_updated": "✅ Transaction type updated.\n\n{text}",
    "enter_new_description": "New description (or '-' to clear):",
    "description_updated": "✅ Description updated.\n\n{text}",
    "choose_new_category": "Choose a new category:",
    "category_updated_learned": "✅ Category updated (I'll remember it next time).\n\n{text}",
    "choose_new_payment": "Choose a new payment method:",
    "payment_updated": "✅ Payment method updated.\n\n{text}",
    "delete_tx_confirm": "Delete this transaction with no way to restore it?",
    "delete_tx_goal_block": "Can't delete: the goal would go negative. Change it via /goals.",
    "deleted": "✅ Deleted.\n\n{text}",
    "transfer_to_goal": "Transfer to goal “{name}”",
    "transfer_from_goal": "Transfer from goal “{name}”",
    "goal_fallback": "goal",
    "catalog_categories_hint": (
        "🏷 <b>Your categories</b>\n\n"
        "Tap 🎨 to change the emoji, ✏️ to rename, 🗑 to delete."
    ),
    "protected_category": "This is a base category — it can't be renamed or deleted",
    "protected_rename": "This is a base category — it can't be renamed",
    "enter_category_name": "Enter the new category name:",
    "category_added": "✅ Category “{name}” added.\n\n{text}",
    "enter_category_rename": "Enter a new name for this category:",
    "enter_category_emoji": "Send one emoji for this category:",
    "emoji_bad": "Send one emoji, for example 👨‍👩‍👧‍👦.",
    "emoji_updated": "✅ Emoji updated.\n\n{text}",
    "renamed_to": "✅ Renamed to “{name}”.\n\n{text}",
    "category_exists": "⚠️ Category “{name}” already exists — pick another name.\n\n{text}",
    "delete_category_q": "Delete this category?\n{note}",
    "category_has_tx": "It has {count} transactions — they will move to “{fallback}”.",
    "category_no_tx": "It has no transactions yet.",
    "delete_failed": "⚠️ Couldn't delete it.\n\n{text}",
    "btn_add_category": "➕ Add category",
    "catalog_payments_hint": (
        "💳 <b>Your payment methods</b>\n\n"
        "Tap ✏️ to rename, 🗑 to delete."
    ),
    "btn_add_payment": "➕ Add payment method",
    "enter_payment_name": "Enter a payment method name (for example: Kaspi Gold):",
    "payment_added": "✅ Payment method “{name}” added.\n\n{text}",
    "enter_payment_rename": "Enter a new name for this payment method:",
    "payment_exists": "⚠️ Payment method “{name}” already exists — pick another name.\n\n{text}",
    "delete_payment_q": "Delete this payment method?\n{note}",
    "payment_has_tx": "It has {count} transactions — their payment method will become empty.",
    "payment_no_tx": "It has no transactions yet.",
    "last_payment_block": "⚠️ You can't delete the last remaining payment method.\n\n{text}",
    "budgets_title": "💰 <b>Budgets</b>",
    "no_budgets": "No limits set yet.",
    "overall_expenses": "All expenses",
    "btn_cat_limit": "➕ Category limit",
    "btn_overall_budget": "🌐 Overall monthly budget",
    "budget_pick_cat": "Which category should get a limit?",
    "budget_enter_overall": "Enter the overall monthly limit for all expenses (number, no spaces):",
    "budget_enter_cat": "Enter the monthly limit for this category (number, no spaces):",
    "amount_positive_big": "Send a number greater than zero, for example: 100000",
    "budget_set": "✅ Limit saved.\n\n{text}",
    "removed": "Deleted",
    "goals_title": "🎯 <b>Savings goals</b>",
    "no_goals": "No goals yet.",
    "deadline_none": "none",
    "btn_new_goal": "➕ New goal",
    "goal_saved": "Saved: {current} / {target}",
    "goal_deadline": "Deadline: {value}",
    "goal_note": "A contribution creates a transfer, not an expense — /stats stays honest.",
    "btn_contribute": "➕ Contribute",
    "btn_withdraw": "➖ Withdraw",
    "btn_name": "✏️ Name",
    "btn_target": "🎯 Amount",
    "btn_deadline": "📅 Deadline",
    "goal_not_found": "Goal not found",
    "goal_not_found_long": "Goal not found (it may already be deleted).",
    "enter_goal_name": "Goal name (for example: Vacation):",
    "enter_goal_target": "How much do you want to save (number)?",
    "amount_positive_goal": "Send a number greater than zero, for example: 300000",
    "enter_goal_deadline": "Deadline as DD.MM.YYYY or '-' if there is none:",
    "bad_date_or_dash": "Couldn't parse the date. Use DD.MM.YYYY or '-'.",
    "goal_created": "✅ Goal created.\n\n{text}",
    "enter_contribute": "How much should I add to the goal?",
    "amount_positive_short": "Send a number greater than zero.",
    "goal_reached": "🎉 Goal reached!\n\n",
    "goal_contributed": "✅ Added as a transfer.\n\n",
    "goal_empty": "There's nothing to withdraw from this goal yet",
    "enter_withdraw": "How much to withdraw? The goal currently has {amount}.",
    "withdraw_too_much": "You can't withdraw more than is saved. The goal won't go negative.",
    "goal_withdrawn": "✅ Withdrawn from the goal.\n\n",
    "enter_goal_rename": "New goal name:",
    "name_updated": "✅ Name updated.\n\n{text}",
    "enter_goal_new_target": "New target amount:",
    "target_updated": "✅ Target amount updated.\n\n{text}",
    "enter_goal_new_deadline": "New deadline as DD.MM.YYYY, or '-' to clear:",
    "deadline_updated": "✅ Deadline updated.\n\n{text}",
    "delete_goal_confirm": (
        "Delete this goal? The remaining amount will return as a transfer, and transfer history will stay."
    ),
    "recurring_title": "🔁 <b>Recurring payments</b>",
    "no_recurring": "None added yet.",
    "recurring_line": "{status} {emoji} {amount} — {label}, on day {day} every month",
    "payment_fallback": "payment",
    "rec_status_on": "active",
    "rec_status_off": "paused",
    "rec_status_label": "Status: {status}",
    "rec_day_label": "Day of month: {day}",
    "rec_amount_label": "Amount: {amount}",
    "btn_pause": "⏸ Pause",
    "btn_resume": "▶️ Enable",
    "btn_day": "📅 Day",
    "rec_not_found": "Payment not found",
    "rec_not_found_long": "Payment not found (it may already be deleted).",
    "rec_what": "What should repeat?",
    "enter_day_of_month": "Which day of the month (1-28)?",
    "day_range": "Send a number from 1 to 28 (so it works in every month).",
    "day_range_short": "Send a number from 1 to 28.",
    "enter_rec_name": "Payment name (for example: Rent):",
    "rec_added": "✅ Recurring payment added.\n\n{text}",
    "enter_rec_amount": "New recurring amount:",
    "rec_amount_updated": "✅ Amount updated. Past runs were not changed.\n\n{text}",
    "day_updated": "✅ Day updated.\n\n{text}",
    "enter_rec_desc": "New payment name:",
    "rec_cat_fail": "Couldn't change the category",
    "rec_pay_fail": "Couldn't change the payment method",
    "category_updated": "✅ Category updated.\n\n{text}",
    "export_empty": "No transactions in this period — nothing to export.",
    "export_large": "⏳ Large export: {count} transactions. Preparing the file may take a while.",
    "export_ready": "Done, {count} transactions 👇",
    "restore_prompt": (
        "📥 Send the backup file (.json) I sent you earlier.\n\n"
        "⚠️ All current transactions, receipts, recurring payments, budgets and goals will be "
        "<b>fully replaced</b> by the file. This cannot be undone.\n\n"
        "/cancel — cancel."
    ),
    "file_too_big_20": "The file is too large (20 MB max).",
    "restore_not_json": "❌ This doesn't look like a JSON backup. Send the file unchanged or /cancel.",
    "restore_bad_format": "❌ Invalid backup format. Send the file unchanged or /cancel.",
    "restore_preview": (
        "🧾 Receipts: {receipts}\n"
        "💰 Transactions: {transactions}\n"
        "🔁 Recurring: {recurring}\n"
        "🎯 Goals: {goals}\n"
        "🏷 Categories: {categories}"
    ),
    "restore_confirm_q": "Found in the file:\n{counts}\n\n⚠️ All current data will be replaced. Confirm?",
    "btn_replace_data": "✅ Replace data",
    "btn_no": "❌ Cancel",
    "restore_need_file": "Waiting for a backup file (.json). /cancel — cancel.",
    "not_your_draft": "This is not your draft.",
    "draft_stale_file": "The draft is stale, send the file again.",
    "restore_failed": (
        "❌ Couldn't restore the backup — the file is damaged or incompatible. "
        "Your current data was not changed."
    ),
    "restore_done": (
        "✅ Data restored:\n"
        "💰 Transactions: {transactions}\n"
        "🧾 Receipts: {receipts}\n"
        "🔁 Recurring: {recurring}\n"
        "🎯 Goals: {goals}"
    ),
    "restore_cancelled": "Restore cancelled, data was not changed.",
    "import_too_big": "⚠️ The file is too large. 2 MB max.",
    "import_too_many": "⚠️ Too many rows in the file (limit 5000).",
    "import_unreadable": "⚠️ Couldn't read the file. CSV and Excel with date/amount/description columns are supported.",
    "import_no_amount": (
        "⚠️ I couldn't find an amount column. Make sure the first row "
        "has a header like “Amount” / “Сумма”."
    ),
    "import_header": "📤 Found {count} transactions:\n💸 Expenses: {expense}",
    "import_income_line": "\n💰 Income: {amount}",
    "import_dups": (
        "\n\n⚠️ {count} rows look like ones already imported. "
        "You can import again if this is a different file."
    ),
    "import_ask": "\n\nImport them?",
    "btn_import_all": "✅ Import all ({count})",
    "draft_stale": "The draft is stale",
    "imported": "✅ Imported {count} transactions.",
    "import_cancelled": "❌ Import cancelled.",
    "receipt_err_too_large": "⚠️ The photo is larger than 8 MB. Compress it or send it as a photo, not a huge file.",
    "receipt_err_unsupported": "⚠️ This format is not supported. I need JPEG, PNG or WebP.",
    "receipt_err_corrupt": "⚠️ The file looks like a damaged image. Send another photo.",
    "receipt_err_rate_limit": (
        "⚠️ Gemini is rate-limited right now. Try again in a few minutes "
        "or type the expense, for example: <code>500 taxi</code>"
    ),
    "receipt_err_network": (
        "⚠️ Couldn't reach Gemini. Try later or type it: "
        "<code>500 taxi</code>"
    ),
    "receipt_err_bad_json": "⚠️ Gemini returned an unclear response. Try another photo or type it.",
    "receipt_err_no_key": "⚠️ Receipt recognition is not configured. Type the expense: <code>500 taxi</code>",
    "receipt_err_unavailable": (
        "⚠️ Gemini is unavailable right now. Try later or type it: "
        "<code>500 taxi</code>"
    ),
    "receipt_err_quota": (
        "⚠️ Today's receipt recognition limit is used up ({limit}). "
        "Tomorrow you can use photos again; for now type: <code>500 taxi</code>"
    ),
    "receipt_err_empty": (
        "⚠️ I found no items on the receipt (Gemini couldn't handle it).\n"
        "Tip: send the receipt as a FILE (📎 → File, not as a photo) — "
        "Telegram won't compress it.\n"
        "Or just type: 500 taxi"
    ),
    "receipt_source_bank": "🏦 Bank",
    "receipt_recognizing": "🔍 Reading the receipt, one moment...",
    "receipt_dup_prompt": "⚠️ This receipt looks already saved ({seen}). Recognize and save it again?",
    "btn_save_again": "✅ Save again",
    "btn_no_need": "❌ No need",
    "receipt_title": "🧾 <b>Recognized receipt</b>",
    "receipt_source": "<i>Source: {name}</i>",
    "receipt_dup_inline": (
        "⚠️ This looks already saved ({seen}). "
        "You can save it again if this is another purchase."
    ),
    "receipt_store": "Store: {name}",
    "receipt_datetime": "Date: {date}   Time: {time}",
    "receipt_type": "Type: {name}",
    "receipt_choose_type": "⚠️ Choose the transaction type before saving.",
    "receipt_pay": "Payment: {name}",
    "receipt_items_total": "Items total: {amount}",
    "receipt_total": "Receipt total: {amount}",
    "receipt_mismatch": (
        "\n⚠️ The totals differ by {amount}. Choose which amount to trust."
    ),
    "receipt_check_items": "\nCheck the items: you can edit or delete them before saving.",
    "receipt_truncated": "\n… some items are hidden here but still available via the buttons below.",
    "receipt_item_fallback": "Item {n}",
    "btn_trust_items": "🛒 Trust items",
    "btn_trust_total": "🧾 Trust receipt total",
    "btn_save_all": "✅ Save all",
    "draft_stale_item": "The draft or item is stale",
    "btn_fix": "✏️ Edit",
    "btn_to_receipt": "◀️ Back to receipt",
    "receipt_edit_prompt": (
        "Enter the name and price separated by <code>|</code>:\n"
        "<code>{sample}</code>\n\n"
        "For example: <code>Milk 2.5% | 450</code>\n"
        "/cancel — cancel."
    ),
    "receipt_edit_format": "Format: name | price. For example: Milk | 450",
    "receipt_edit_need": "I need a non-empty name and a price greater than zero.",
    "draft_stale_resend": "The draft or item is stale. Send the photo again.",
    "item_updated": "✅ Item updated.\n\n{text}",
    "last_item_block": "You can't delete the last item — cancel the whole receipt.",
    "item_deleted": "Item deleted",
    "draft_update_fail": "Couldn't update the draft",
    "used_items_total": "Using the items total",
    "receipt_total_bad": "The receipt total is invalid",
    "used_receipt_total": "Items were recalculated to match the receipt total",
    "choose_type_first": "Choose income or expense first",
    "choose_total_first": "Choose which total to use first",
    "save_retry": "Couldn't save. The draft is still here — try again.",
    "draft_stale_photo": "The draft is stale, send the photo again",
    "receipt_saved": "\n\n✅ Saved!",
    "btn_receipt_items": "📝 Receipt items (edit)",
    "saved": "Saved",
    "receipt_items_title": "🧾 <b>Receipt items</b>\n",
    "items_not_found": "Items not found",
    "receipt_cancelled": "❌ Cancelled, nothing was saved.",
    "choose_how_paid": "How did you pay?",
    "draft_or_pay_bad": "The draft is stale or the payment method is not yours",
    "request_stale": "The request is stale",
    "recognize_again": "Ok, I'll recognize it again.",
    "dup_skipped": "Ok, I won't save anything. If this is another purchase — send the photo again.",
    "sched_recurring": "🔁 Added automatically: {emoji} {amount} — {desc}",
    "sched_idle": (
        "👋 No new entries for a while. Everything ok? If not — "
        "just type an amount and what you bought, it takes a second."
    ),
    "sched_backup": "📦 Weekly auto-backup of your data",
    "app_title": "📊 My budget",
    "tab_overview": "Overview and transactions",
    "tab_budgets": "Budgets",
    "tab_goals": "Goals",
    "custom_apply": "Show",
    "all_categories": "All categories",
    "by_categories": "By category",
    "transactions_title": "Transactions",
    "no_transactions": "No transactions found",
    "load_error": "Couldn't load data. Open the Mini App from the bot after /start.",
    "form_amount": "Amount",
    "form_type": "Type",
    "form_category": "Category",
    "form_date": "Date",
    "form_description": "Description",
    "form_store": "Store",
    "form_payment": "Payment",
    "created_ok": "Transaction saved",
    "updated_ok": "Transaction updated",
    "save_error": "Couldn't save",
    "validation_error": "Check the amount, date and category",
}


def _load_partial_kk() -> dict[str, str]:
    src = (ROOT / "i18n.py").read_text(encoding="utf-8")
    start = src.index("_KK = {")
    ns: dict = {}
    try:
        exec(src[start:], ns)
    except SyntaxError:
        # Truncated file: close the last string and the dict.
        closed = src[start:].rstrip()
        if not closed.endswith('"'):
            closed += '"'
        closed += "\n}\n"
        exec(closed, ns)
    return dict(ns["_KK"])


def main() -> None:
    kk = _load_partial_kk()
    kk.update(KK_EXTRA)
    missing_kk = sorted(set(RU) - set(kk))
    extra_kk = sorted(set(kk) - set(RU))
    missing_en = sorted(set(RU) - set(EN))
    extra_en = sorted(set(EN) - set(RU))
    if missing_kk or extra_kk or missing_en or extra_en:
        raise SystemExit(
            f"kk missing={missing_kk} extra={extra_kk}; "
            f"en missing={missing_en} extra={extra_en}"
        )
    (ROOT / "locales" / "kk.json").write_text(
        json.dumps({key: kk[key] for key in RU}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (ROOT / "locales" / "en.json").write_text(
        json.dumps({key: EN[key] for key in RU}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote kk/en with {len(RU)} keys")


if __name__ == "__main__":
    main()
